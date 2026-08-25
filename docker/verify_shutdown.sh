#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd -- "${script_directory}/.." && pwd)"
cd "${repository_root}"

project_name="${COGNISTORE_SHUTDOWN_PROJECT:-cognistore-shutdown-test}"
export COGNISTORE_NATS_PORT="${COGNISTORE_NATS_PORT:-24222}"
export COGNISTORE_NATS_MONITOR_PORT="${COGNISTORE_NATS_MONITOR_PORT:-28222}"
export COGNISTORE_MINIO_PORT="${COGNISTORE_MINIO_PORT:-29000}"
export COGNISTORE_MINIO_CONSOLE_PORT="${COGNISTORE_MINIO_CONSOLE_PORT:-29001}"
export COGNISTORE_HEALTH_PORT="${COGNISTORE_HEALTH_PORT:-28081}"
export COGNISTORE_TIER_LIMITS_FILE="${COGNISTORE_TIER_LIMITS_FILE:-./docker/tier-limits.shutdown.yaml}"

compose() {
  docker compose --project-name "${project_name}" "$@"
}

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    compose logs --no-color || true
  fi
  compose stop --timeout 2 cognistore >/dev/null 2>&1 || true
  compose --profile integration --profile development down \
    --volumes --remove-orphans >/dev/null 2>&1 || true
  exit "${status}"
}
trap cleanup EXIT

compose up --build --wait --wait-timeout 90

compose exec -T cognistore python -c '
from cognistore.drivers.posix_driver import PosixDriver

driver = PosixDriver("/var/lib/cognistore/tiers/hot")
driver.put_object(
    "shutdown-diagnostics",
    "in-flight.bin",
    b"x" * (16 * 1024 * 1024),
)
'

compose exec -T cognistore cognistore \
  --no-config \
  --drivers /etc/cognistore/drivers.yaml \
  --catalog-db /var/lib/cognistore/catalog.sqlite3 \
  catalog-scan hot shutdown-diagnostics --sync --json >/dev/null

compose exec -T cognistore cognistore \
  --no-config \
  --drivers /etc/cognistore/drivers.yaml \
  --nats-url nats://nats:4222 \
  policy-run shutdown-diagnostics \
  --threshold 1 \
  --allowed-tiers hot,warm \
  --json >/dev/null

journal_ready=false
for _attempt in {1..100}; do
  if compose exec -T cognistore python -c '
from cognistore.core.sqlite_catalog import SQLiteCatalog

catalog = SQLiteCatalog("/var/lib/cognistore/catalog.sqlite3", read_only=True)
try:
    jobs = catalog.list_move_jobs()
finally:
    catalog.close()
raise SystemExit(0 if any(not job.state.terminal for job in jobs) else 1)
'; then
    journal_ready=true
    break
  fi
  sleep 0.1
done

if [[ "${journal_ready}" != true ]]; then
  echo "The worker did not create a non-terminal move journal in time." >&2
  exit 1
fi

# Override the normal 45-second Compose grace to model daemon or machine loss
# while the rate-limited storage call is still in flight.
compose stop --timeout 2 cognistore

compose run --rm --no-deps --entrypoint python cognistore -c '
import json

from cognistore.core.sqlite_catalog import SQLiteCatalog

catalog = SQLiteCatalog("/var/lib/cognistore/catalog.sqlite3", read_only=True)
try:
    jobs = catalog.list_move_jobs()
    assert jobs, "interrupted move has no durable job record"
    diagnostics = []
    for job in jobs:
        transitions = catalog.list_move_job_transitions(job.idempotency_key)
        assert transitions, f"move {job.idempotency_key!r} has no phase history"
        diagnostics.append(
            {
                "idempotency_key": job.idempotency_key,
                "state": job.state.value,
                "transitions": [item.to_state.value for item in transitions],
            }
        )
    assert any(not job.state.terminal for job in jobs), (
        "rate-limited move unexpectedly completed before forced shutdown"
    )
    print(json.dumps(diagnostics, sort_keys=True))
finally:
    catalog.close()
'

# A restarted worker must become healthy with the same catalog and JetStream
# volumes still attached. The interrupted delivery can then be redelivered and
# resume from the journaled phase after its leases expire.
compose up --wait --wait-timeout 90 cognistore

compose run --rm --no-deps --entrypoint python cognistore -c '
from cognistore.core.sqlite_catalog import SQLiteCatalog

catalog = SQLiteCatalog("/var/lib/cognistore/catalog.sqlite3", read_only=True)
try:
    assert catalog.list_move_jobs(), "move journal disappeared after restart"
finally:
    catalog.close()
'

echo "Interrupted move journal survived worker shutdown and restart."
