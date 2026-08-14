# S3-compatible storage driver

CogniStore's `s3` driver provides the same object operations as the POSIX
driver against AWS S3 and S3-compatible services such as MinIO. A tier does
not fix a single bucket: the bucket passed to each driver or CLI operation is
the S3 bucket used for that request.

## Configuration

Add an S3 tier under `tiers` in `drivers.yaml`. The loader accepts the
following fields:

| Field | Purpose |
| --- | --- |
| `driver` | Must be `s3`. |
| `endpoint` or `endpoint_url` | Optional S3 API URL. Set it for MinIO; omit it for AWS S3. |
| `region` or `region_name` | Optional AWS region, for example `us-west-2`. |
| `access_key`, `secret_key`, `session_token` | Literal credentials. Supported for controlled use, but do not commit them. |
| `access_key_env`, `secret_key_env`, `session_token_env` | Names of environment variables whose values contain credentials. |
| `profile` or `profile_name` | A profile from the AWS shared configuration and credentials files. |
| `addressing_style` | Botocore bucket addressing mode: `auto`, `virtual`, or `path`. Path style is commonly needed for local MinIO. |
| `auto_create_bucket` | When `true`, create a missing bucket before the first write. Defaults to `false`. |
| `chunk_size` | Streaming buffer and multipart part size in bytes. Defaults to 8388608 (8 MiB) and must be from 5242880 (5 MiB) through 5368709120 (5 GiB). |
| `list_page_size` | Number of keys requested per `ListObjectsV2` page. Listing still yields every matching key. |
| `multipart_threshold` | Positive object size in bytes at which multipart upload begins. Defaults to 8388608 (8 MiB); the boundary is inclusive. |

Use only one spelling of an aliased field in a tier. Use one credential
strategy per tier as well; combining a profile with explicit keys makes
credential selection harder to audit.

### MinIO

Export credentials in the process that launches CogniStore:

```bash
export COGNISTORE_MINIO_ACCESS_KEY='<MinIO access key>'
export COGNISTORE_MINIO_SECRET_KEY='<MinIO secret key>'
```

Then configure a tier without putting the values themselves in YAML:

```yaml
tiers:
  object:
    driver: s3
    endpoint_url: http://127.0.0.1:9000
    region_name: us-east-1
    access_key_env: COGNISTORE_MINIO_ACCESS_KEY
    secret_key_env: COGNISTORE_MINIO_SECRET_KEY
    addressing_style: path
    auto_create_bucket: true
    chunk_size: 8388608
    list_page_size: 1000
    multipart_threshold: 8388608
```

`auto_create_bucket: true` is convenient for a disposable local MinIO
instance. The endpoint must be the S3 API endpoint, not the MinIO console.

### AWS S3

On AWS, prefer an IAM role or another short-lived identity available through
the standard boto3 credential chain. In that case, omit every credential field:

```yaml
tiers:
  object:
    driver: s3
    region: us-west-2
    addressing_style: auto
    auto_create_bucket: false
    chunk_size: 8388608
    list_page_size: 1000
    multipart_threshold: 8388608
```

For local development, select a configured AWS profile without copying its
keys into the repository:

```yaml
tiers:
  object:
    driver: s3
    region_name: us-west-2
    profile_name: cognistore-dev
    auto_create_bucket: false
```

If no explicit keys, `*_env` references, or profile is configured, boto3 uses
its normal credential provider chain. That includes standard AWS environment
variables, shared AWS config/credential files, container credentials, and
instance roles. CogniStore does not print resolved credential values.

For production AWS buckets, keep `auto_create_bucket` disabled and provision
buckets separately with the intended ownership, region, encryption, versioning,
and access controls. Enabling it requires bucket-creation permission and does
not replace infrastructure policy.

## Supported semantics

The driver advertises `range_reads=True`, `range_writes=False`, and
`atomic_no_overwrite=True` through its capability declaration.

- `put_object` writes a complete object. It overwrites by default;
  `overwrite=False` uses an atomic conditional put and raises `FileExistsError`
  if the key already exists.
- Ranged reads accept an HTTP byte range such as `bytes=0-99` and return those
  bytes. Ranged writes are not supported and raise `NotImplementedError`.
- `get_object` and `stat_object` raise `FileNotFoundError` when the object does
  not exist. They do not silently return empty content or metadata.
- `delete_object` is idempotent: deleting a missing object or an object in a
  missing bucket succeeds without an error.
- `list_objects` uses paginated `ListObjectsV2` requests and yields all keys
  matching the prefix. `list_page_size` controls each request, not the total
  number of results.
- With `auto_create_bucket: true`, a missing bucket is created for a write.
  Read, stat, list, and delete operations do not create buckets.
- Moves read and write sequentially. Each source read is bounded by
  `chunk_size`, and an S3 destination uploads one multipart part at a time, so
  memory use scales with the configured buffers rather than total object size.
- Objects smaller than `multipart_threshold` and within S3's single-put limit
  use a single conditional put.
  Their validated content is staged in a spooled temporary file, which spills
  to disk above `chunk_size` so memory remains chunk-bounded; local temporary
  disk usage can approach the object's size. Objects at or above the threshold
  use multipart upload. Objects above S3's 5 GiB
  single-put limit use multipart regardless of the configured threshold. The
  driver rejects transfers that would require more than S3's 10,000-part
  limit. S3 requires every non-final part to be at least 5 MiB; the last part
  may be smaller.
- Multipart no-overwrite writes apply `If-None-Match: *` when completing the
  upload, preserving the destination-collision guard across the final commit.
- A catchable failure or cancellation before completion aborts the multipart
  upload. The configured identity needs `s3:AbortMultipartUpload` in addition
  to its write permissions.

A process crash, `SIGKILL`, or machine loss cannot execute application cleanup.
For production buckets, configure an S3 lifecycle rule with the
`AbortIncompleteMultipartUpload` action as a defense-in-depth backstop for
uploads that outlive the process. A repository-managed Docker Compose
environment, including its MinIO service and health checks, belongs to ticket
[#28](https://github.com/melliott18/CogniStore/issues/28).

## Opt-in MinIO integration tests

The S3 integration tests are skipped by default so the normal test suite does
not require network access or credentials. Start a MinIO instance outside this
repository, then provide its API endpoint and disposable test credentials.
All three variables shown below are required; the module skips when any one is
absent:

```bash
export COGNISTORE_MINIO_ENDPOINT_URL=http://127.0.0.1:9000
export COGNISTORE_MINIO_ACCESS_KEY='<MinIO access key>'
export COGNISTORE_MINIO_SECRET_KEY='<MinIO secret key>'
export COGNISTORE_MINIO_REGION=us-east-1
# Optional when the test identity uses temporary credentials:
# export COGNISTORE_MINIO_SESSION_TOKEN='<MinIO session token>'

python -m pytest -q -m integration tests/integration/test_s3_minio.py
```

Use a dedicated MinIO instance or credentials restricted to disposable test
buckets. The tests create and remove their own uniquely named buckets and
objects. Do not point them at a production AWS account. The one-command
Compose-based test environment will be delivered under ticket #28; ticket #17
intentionally requires a separately managed MinIO endpoint.

## Secret safety

- Never commit access keys, secret keys, session tokens, `.env` files, or AWS
  credential files. The repository's example `drivers.yaml` contains only
  environment-variable names.
- Prefer short-lived credentials and least-privilege policies. On AWS, prefer
  workload roles; on developer machines, prefer a named profile or SSO-backed
  credentials.
- Treat configuration files and diagnostic output as potentially shareable.
  Use `*_env` indirection when a nonstandard environment variable must supply a
  credential, and avoid commands that echo its value.
- Rotate a credential immediately if it appears in Git history, logs, test
  output, or a pasted configuration. Removing it from the latest file is not
  sufficient.
