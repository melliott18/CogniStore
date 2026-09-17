#!/bin/sh
# Use the Compose-pinned Prometheus version and its real mounted configuration.
set -eu
repository=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repository"
docker compose --profile observability config --quiet
docker compose --profile observability run --rm --no-deps \
  --entrypoint /bin/sh --volume "$repository:/workspace:ro" \
  --workdir /workspace prometheus -ec '
    promtool check config /etc/prometheus/prometheus.yml
    promtool test rules tests/observability/slo_rules.test.yml
  '
