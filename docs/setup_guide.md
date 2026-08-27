# Docker development and integration environment

The repository contains a repeatable local stack for CogniStore, its durable
NATS JetStream queue, and an S3-compatible MinIO tier. Docker builds a small
runtime image for the worker and a separate development image containing the
test and quality tooling. Every image in the stack runs as an unprivileged
user, and credentials are injected only when containers start.

Base images are digest-pinned. Python dependency versions are resolved from
the bounds in `pyproject.toml` at build time, so identical source is not yet a
byte-for-byte reproducible dependency build. Rebuild and rerun the integration
suite when evaluating a later dependency resolution.

## Prerequisites

- Docker Engine 24 or Docker Desktop with Docker Compose v2.17 or newer.
- Free host ports 4222, 8081, 8222, 9000, and 9001. Each port can be overridden
  with the environment variables listed below.

No host Python, NATS, MinIO, or package installation is required.

## Start the stack

From a clean checkout, build the runtime image and wait for every default
service to become healthy:

```bash
docker compose up --build --wait
```

This starts:

| Service | Host endpoint | Purpose |
| --- | --- | --- |
| CogniStore | `http://127.0.0.1:8081/readyz` | Worker readiness and JetStream probe |
| NATS | `nats://127.0.0.1:4222` | Durable job transport |
| NATS monitor | `http://127.0.0.1:8222` | Broker health and diagnostics |
| MinIO | `http://127.0.0.1:9000` | S3-compatible API |
| MinIO console | `http://127.0.0.1:9001` | Local object-store console |

The default MinIO identity is `cognistore` with password
`cognistore-development-only`. These are public, disposable development
defaults, not production credentials. Override them at container runtime when
the stack is shared:

```bash
export COGNISTORE_MINIO_ACCESS_KEY='<local access key>'
export COGNISTORE_MINIO_SECRET_KEY='<local secret of at least eight characters>'
docker compose up --build --wait
```

Compose also accepts `COGNISTORE_NATS_PORT`,
`COGNISTORE_NATS_MONITOR_PORT`, `COGNISTORE_MINIO_PORT`,
`COGNISTORE_MINIO_CONSOLE_PORT`, and `COGNISTORE_HEALTH_PORT` to change the
published host ports. Container-to-container traffic always uses the internal
service names and is unaffected by those overrides.

## Run the integration suite

The following single command builds the development/test image, starts the
health-gated stack, and runs `tests/integration` inside it:

```bash
docker compose --profile integration up --build \
  --abort-on-container-exit \
  --exit-code-from integration-tests
```

The command returns pytest's exit status and stops the other containers after
the test runner exits. NATS and MinIO tests receive their service URLs and
disposable credentials from Compose, so they do not silently fall back to host
services or the ambient AWS credential chain. A JUnit report remains in the
`test-results` named volume. To copy it into the ignored local
`test-results/` directory:

```bash
mkdir -p test-results
docker compose --profile integration run --rm --no-deps \
  --entrypoint cat integration-tests /test-results/integration.xml \
  > test-results/integration.xml
```

## Use the editable development image

Open a shell with the checkout mounted at `/workspace` and the development
dependencies already installed:

```bash
docker compose --profile development run --rm development
```

The shell runs as UID 10001, not root. It shares the service network, the
CogniStore data volume, the Compose driver configuration, and these internal
endpoints:

```bash
export COGNISTORE_NATS_URL=nats://nats:4222
export COGNISTORE_MINIO_ENDPOINT_URL=http://minio:9000
```

For example, run the environment-independent suite with:

```bash
python -m pytest tests/unit tests/conformance
```

Rebuild the development image after changing project dependency metadata.

## Shutdown and diagnostics

Use a normal stop or teardown so Compose sends `SIGTERM` to the worker:

```bash
docker compose stop
```

The worker stops claiming deliveries, drains current work, and records its
last safe movement phase before exiting. Compose allows 45 seconds for the
configured 30-second drain plus settlement. The SQLite catalog and POSIX tiers
remain in `cognistore-data`; queued and claimed jobs remain in `nats-data`; and
MinIO objects remain in `minio-data`. Restart with `docker compose up --wait`.

Inspect durable movement state without starting the dependencies:

```bash
docker compose run --rm --no-deps cognistore \
  --no-config \
  --drivers /etc/cognistore/drivers.yaml \
  --catalog-db /var/lib/cognistore/catalog.sqlite3 \
  move-list --json

docker compose logs cognistore nats
```

Inspect the logs after `stop` and before removing containers. Once the evidence
has been captured, remove the stopped containers and network while retaining
all named-volume data with `docker compose down`.

`SIGKILL`, a Docker daemon crash, or machine loss cannot run graceful cleanup.
Even then, the named volumes retain the NATS delivery and the catalog-backed
move journal. On restart, at-least-once redelivery reuses the stable job ID and
resumes from the recorded checkpoint after its lease expires. Do not use
`docker compose down --volumes` when preserving evidence for diagnosis.

The CI container job exercises the same boundary with a real rate-limited
background move. Run that focused probe locally with:

```bash
bash docker/verify_shutdown.sh
```

It uses a separate Compose project and high-numbered host ports, forces an
interruption only after a non-terminal move journal exists, verifies its phase
history from the stopped worker's volume, restarts the worker, and deletes its
isolated test volumes on exit.

## Reset all local data

Ordinary `stop` and `down` commands preserve the named volumes. To explicitly
delete all local objects, queue state, catalog history, and test reports:

```bash
docker compose --profile integration --profile development down \
  --volumes --remove-orphans
```

This reset is destructive and the removed local state cannot be recovered.

## Image and configuration details

- `Dockerfile` pins its Python, NATS, and MinIO base image digests. The
  `runtime` target contains only CogniStore and runtime dependencies; the
  `development` target adds `.[dev]` tooling and the checkout.
- `.dockerignore` excludes Git history, local environments, credentials,
  caches, reports, build artifacts, logs, and local SQLite state.
- `docker/drivers.yaml` contains only paths, the internal MinIO endpoint, and
  environment-variable names. It contains no literal credential value.
- `docker/tier-limits.yaml` keeps the normal stack unthrottled while declaring
  bounded concurrency. A separate rate-limit file is selected only by
  `docker/verify_shutdown.sh`.
- All persistent write paths are created with the unprivileged UID during the
  image build. Application root filesystems are read-only in the normal worker
  and integration-test services, with bounded temporary filesystems for
  streaming and test scratch data.
