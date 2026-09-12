# Google Cloud Storage driver

CogniStore's `gcs` driver implements the shared storage contract using the
Google Cloud Storage JSON API. The bucket passed to each operation selects
the remote bucket; a tier does not bind one bucket. The default installation
includes the driver and its `google-auth` authentication dependency.

## Configuration and credentials

Configure a tier in `drivers.yaml`:

```yaml
tiers:
  cloud:
    driver: gcs
    project: example-project
    auto_create_bucket: false
    chunk_size: 8388608
    list_page_size: 1000
    max_retries: 3
    timeout: 60
```

| Field | Purpose |
| --- | --- |
| `driver` | Must be `gcs`. |
| `project` | Optional Google Cloud project ID; required for bucket creation if credentials do not supply one. It does not restrict access to buckets in other projects. |
| `credentials_file` | Optional path to a trusted Google authentication JSON file. The field contains a path, never inline JSON or a token. |
| `credentials_file_env` | Name of an environment variable containing the authentication file path. Use either this field or `credentials_file`. |
| `emulator_endpoint` | Explicit emulator base URL, for example `http://127.0.0.1:4443`. Enables anonymous requests and cannot be combined with explicit credential configuration. Omit for Google Cloud. |
| `auto_create_bucket` | Create a missing bucket before a write when `true`; defaults to `false`. Read-only operations and deletion never create buckets. |
| `chunk_size` | Transfer buffer size in bytes. Defaults to 8388608 (8 MiB); must be a positive multiple of 262144 (256 KiB). |
| `list_page_size` | Optional number of keys requested per page, from 1 through 1000. All matching pages are consumed. |
| `max_retries` | Bounded retry count for transient operations and upload recovery; defaults to `3`. Set `0` to disable retries. |
| `timeout` | Positive HTTP timeout in seconds; defaults to `60`. This applies to network operations, not the total object transfer duration. |

Unknown GCS fields are rejected. Credentials and HTTP clients supplied directly
to the Python constructor cannot be supplied through YAML.

Without an explicit credentials file, the driver uses Application Default
Credentials (ADC). ADC checks `GOOGLE_APPLICATION_CREDENTIALS`, local
application-default credentials, and an attached service account. For local
development, `gcloud auth application-default login` establishes ADC. On
Google Cloud, prefer an attached workload identity with the required bucket
permissions. See Google's
[ADC guidance](https://cloud.google.com/docs/authentication/application-default-credentials).

If a deployment supplies an authentication file outside the repository, use
path indirection:

```bash
export COGNISTORE_GCS_CREDENTIALS_FILE=/run/secrets/google-credentials.json
```

```yaml
tiers:
  cloud:
    driver: gcs
    project: example-project
    credentials_file_env: COGNISTORE_GCS_CREDENTIALS_FILE
```

The environment variable contains a **file path**, not credential JSON.
Provision and validate authentication files through the deployment's trusted
credential mechanism. Do not commit these files or put tokens, private keys,
or credential JSON in YAML. Driver errors omit authentication contents and
resumable session URLs; avoid enabling HTTP wire logging that would expose
authorization headers or session URLs.

Provision production buckets separately with their intended location,
encryption, access controls, retention, and lifecycle configuration. Keep
`auto_create_bucket` disabled for these buckets. The driver does not provision
cloud infrastructure or apply bucket policy. Uploads require
`storage.objects.create`; replacing an object also requires
`storage.objects.delete`. See Google's
[upload permissions](https://cloud.google.com/storage/docs/performing-resumable-uploads#required_roles).
The identity also needs the object read, list, and delete permissions for the
operations it uses. Automatic bucket creation additionally needs bucket
creation permission in the selected project.

## Object operations and metadata

The driver advertises `range_reads=True`, `range_writes=False`,
`atomic_no_overwrite=True`, and `conditional_delete=True`.

- Complete puts overwrite by default. `overwrite=False` uses the server's
  atomic `ifGenerationMatch=0` precondition and raises `FileExistsError` for an
  existing destination, including a collision during resumable upload.
- Reads support a single HTTP byte range, including `bytes=0-99`, `bytes=100-`,
  and `bytes=-100`. Ranged writes raise `NotImplementedError`.
- `open_object_reader` yields a context-managed HTTP stream. Bounded reads
  consume bounded buffers. `get_object`, like the other drivers, returns the
  requested bytes in memory. Reads preserve stored bytes without transparent
  HTTP content decompression.
- Generation-bound readers and conditional deletes use `ifGenerationMatch`
  on the actual request. A different generation raises
  `ObjectGenerationMismatchError`; the replacement remains untouched.
- Missing gets and stats raise `FileNotFoundError`. Deleting a missing object
  succeeds; conditional deletion returns `False` when it no longer exists.
- Listing follows every `nextPageToken` and yields keys matching the requested
  prefix. `list_page_size` controls each request, not the total result count.
- `stat_object` returns integer `size`, POSIX timestamp `mtime`, and an opaque
  string `generation`, plus `content_type`, `etag`, string-valued user
  `metadata`, and available `md5_hash` and `crc32c` checksums.

Set content type and string-valued user metadata with a streaming put:

```python
from io import BytesIO

from cognistore.drivers import GCSDriver

driver = GCSDriver(project="example-project")
data = b"hello from CogniStore\n"
driver.put_object_stream(
    "example-bucket",
    "notes/hello.txt",
    BytesIO(data),
    size=len(data),
    overwrite=False,
    metadata={
        "content_type": "text/plain",
        "metadata": {"source": "example"},
    },
)
```

## Resumable uploads, memory, and failure recovery

Every upload uses a resumable JSON API session. Before creating the session,
the driver validates the entire source against its declared size, staging it
in a spooled temporary file. The spool spills to disk above `chunk_size`, so
memory use is bounded by the configured transfer buffers; temporary disk
usage can approach the object's full size. A short, overlong, or failing
source cannot publish a partial object or replace an existing object.

The driver sends one chunk at a time. Following a connection interruption,
HTTP 429, or a retryable server error, it queries the session's acknowledged
offset before resending data. Recovery remains within the current call and
the configured retry budget. A successfully committed upload whose response
was lost is recognized by the status query.

Catchable failures and cancellation trigger a best-effort session DELETE.
If the process is killed, the machine fails, or the provider is unavailable,
that cleanup may not complete. Session state is not persisted for
cross-process resumption; a subsequent attempt starts a new upload. Google
Cloud expires unfinished resumable sessions after one week. Session URLs
grant upload access and must remain secret. The protocol, chunk alignment,
status queries, cancellation, and expiry are documented in Google's
[resumable upload guide](https://cloud.google.com/storage/docs/performing-resumable-uploads).

Authentication and permission failures raise `PermissionError`. Transient
provider and transport failures raise `GCSTransientError` (a `ConnectionError`
and `OSError` subclass) after retry exhaustion; invalid inputs raise
`ValueError`. If upload cancellation cannot be confirmed,
`GCSUploadCleanupError` reports that uncertainty. These driver-specific
exceptions are available from `cognistore.drivers.gcs_driver`. Error messages
contain safe operation and status context without provider response bodies or
credential-bearing URLs.

Session initiation, bucket creation, and unconditional object deletion are
not retried after an ambiguous response: repeating these mutations could
create another session or affect a replacement object. Streaming reads that
fail after yielding data raise a transient error to the caller instead of
silently reopening a possibly changed key.

## Emulator and live-cloud validation

The default unit and shared conformance suites use a deterministic in-process
GCS JSON API emulator. They run offline and cover partial uploads, interrupted
responses, throttling, missing objects, ranges, pagination, metadata, and
generation conditions:

```bash
python -m pytest tests/unit/test_gcs_driver.py tests/unit/test_gcs_driver_loader.py \
  tests/conformance
```

For external emulator integration, start the supplied disposable
`fake-gcs-server` service and opt in explicitly:

```bash
docker compose -f docker-compose.gcs.yml up -d --build
COGNISTORE_GCS_EMULATOR_ENDPOINT=http://127.0.0.1:4443 \
  python -m pytest tests/integration/test_gcs.py
docker compose -f docker-compose.gcs.yml down
```

The compose file builds a pinned upstream commit containing conditional DELETE
generation checks that are missing from the latest release, v1.56.1. CI runs
this external emulator as a separate job. The tests create and remove unique
test buckets. Use the following configuration
to connect a local CogniStore process to the same emulator:

```yaml
tiers:
  local-gcs:
    driver: gcs
    emulator_endpoint: http://127.0.0.1:4443
    project: local-test-project
    auto_create_bucket: true
    chunk_size: 8388608
```

Emulator access is anonymous and requires `emulator_endpoint` explicitly.
Setting `STORAGE_EMULATOR_HOST` alone does not change the backend. Explicit
emulator mode bypasses ADC, including `GOOGLE_APPLICATION_CREDENTIALS`, so
production credentials are not sent to a local test service. Emulator
behavior is only an approximation of Google Cloud; use live validation for
the provider contract.

The pinned emulator still ignores `ifGenerationMatch` on media GET requests.
That exact assertion has a strict expected failure in external conformance;
other failures remain failures, and a future emulator fix requires removing
the expectation. The complete shared suite passes against the deterministic
protocol emulator, and live validation checks the media GET condition directly.
The external emulator also does not faithfully model interrupted-upload status
queries or session cancellation, which the deterministic tests cover. These
limitations are visible in the pinned upstream
[object request handlers](https://github.com/fsouza/fake-gcs-server/blob/3c29d20789f6475f65d2554ad6898c4afd7124bb/fakestorage/object.go)
and [upload handlers](https://github.com/fsouza/fake-gcs-server/blob/3c29d20789f6475f65d2554ad6898c4afd7124bb/fakestorage/upload.go).

For opt-in live validation, select an existing disposable bucket and arrange
ADC through the normal credential mechanism:

```bash
export COGNISTORE_GCS_LIVE_BUCKET=your-disposable-test-bucket
# Optional project override:
export COGNISTORE_GCS_LIVE_PROJECT=your-test-project
python -m pytest -m integration tests/integration/test_gcs.py
```

The live path does not create buckets. It uses a unique object prefix and
deletes only objects created by the test. With neither integration endpoint
nor live bucket configured, external integration tests skip. Live tests can
incur ordinary storage and request charges; run them against the intended
test project and bucket.
