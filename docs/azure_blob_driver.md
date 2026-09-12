# Azure Blob storage driver

The `azure_blob` driver stores CogniStore objects as Azure block blobs. The
`bucket` argument selects a container and the `key` argument selects a blob
within it. A tier addresses one storage account and may use multiple
containers. Azure Files, page/append blob writes, and Data Lake-specific APIs
are outside this driver's contract.

## Installation and configuration

Azure dependencies are optional. Install the extra before selecting this driver:

```bash
python -m pip install -e '.[azure]'
```

For tests and development tooling, install `'.[dev,azure]'` instead. POSIX and
S3 configurations continue to work without the Azure packages.

Add a tier to `drivers.yaml`, or adapt
[`examples/drivers.azure.yaml`](../examples/drivers.azure.yaml):

| Field | Purpose |
| --- | --- |
| `driver` | Must be `azure_blob`. |
| `account_url` | Account Blob service endpoint, for example `https://example.blob.core.windows.net`. |
| `connection_string` or `connection_string_env` | Literal connection string or the name of its environment variable. Use this instead of `account_url` and `credential`. |
| `credential` or `credential_env` | Literal account key/SAS token or the name of its environment variable, used with `account_url`. Omit for `DefaultAzureCredential` unless the URL already contains a SAS. |
| `auto_create_container` | Create a missing container before a write when `true`. Defaults to `false`. Read, stat, list, and delete never create containers. |
| `chunk_size` | Maximum transfer block size in bytes, from 1 through 4194304000 (4,000 MiB); defaults to 8388608 (8 MiB). |
| `list_page_size` | Requested number of blobs per page, from 1 through 5000; does not limit the complete listing. |

Use only one of a secret's literal and `_env` fields. An `_env` value must be
an environment-variable name, and that variable must contain a non-empty
value. Connection strings, keys, and SAS tokens belong in process secrets;
do not commit them to YAML. CogniStore configuration and provider errors omit
resolved credentials and endpoint query strings.

### Azure identity

An account URL without an explicit credential or embedded SAS uses
`DefaultAzureCredential`:

```yaml
tiers:
  object:
    driver: azure_blob
    account_url: https://example.blob.core.windows.net
    auto_create_container: false
    chunk_size: 8388608
    list_page_size: 1000
```

Microsoft recommends Microsoft Entra identity for Blob access. Locally,
`DefaultAzureCredential` can use a signed-in Azure CLI identity; Azure-hosted
applications can use managed identity. Assign the identity the required Blob
data permissions, such as Storage Blob Data Contributor for read/write use.
See Microsoft's [Python Blob quickstart](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-quickstart-blobs-python).

Keep container auto-creation disabled for pre-provisioned production storage.
The storage account and its encryption, retention, network, and access
policies are provisioned separately.

### Connection strings and Azurite

Inject a connection string into the process that runs CogniStore:

```bash
export COGNISTORE_AZURE_CONNECTION_STRING='<connection string from your secret store>'
```

```yaml
tiers:
  object:
    driver: azure_blob
    connection_string_env: COGNISTORE_AZURE_CONNECTION_STRING
    auto_create_container: true
    chunk_size: 8388608
    list_page_size: 1000
```

Auto-creation is convenient for a disposable Azurite instance. Its connection
string must include the Blob endpoint and account path, for example
`BlobEndpoint=http://127.0.0.1:10000/cognistore`. Use the container's network
hostname instead of `127.0.0.1` when connecting from another container.

## Object and streaming semantics

The driver advertises `range_reads=True`, `range_writes=False`,
`atomic_no_overwrite=True`, and `conditional_delete=True`. It also implements
streaming reads/writes, generation-bound reads, normalized errors, and listing
pagination. Ranged writes are unsupported and
raise `NotImplementedError` before a storage mutation.

- `put_object` writes the complete value and overwrites by default.
  `overwrite=False` applies an atomic absence condition to the final block
  commit, raising `FileExistsError` on a collision, including concurrent ones.
- `get_object` supports single byte ranges (`bytes=0-99`, `bytes=100-`, and
  `bytes=-100`). Missing blobs raise `FileNotFoundError` for reads and stat.
  Deleting a missing blob or a blob in a missing container is idempotent.
- `list_objects` follows continuation pages and yields every key matching
  the prefix. Missing containers produce an empty listing.
- `stat_object` maps the blob's size, modification time, and ETag into the
  shared object metadata. The ETag is an opaque generation identifier, not a
  content checksum.
- Reads bind to an observed ETag. Every bounded download uses the same
  generation condition; a concurrent replacement fails instead of mixing
  bytes from different generations. Conditional deletes enforce the observed
  generation at the provider, allowing safe move cleanup.
  Use `open_object_reader().read(n)` to consume bounded chunks;
  `get_object()` and an unbounded `read()` materialize the requested content.
- Authentication failures become `PermissionError`. Throttling and temporary
  provider failures become `AzureBlobError` after SDK retries. Its safe
  `status_code` and `error_code` fields support caller retry decisions; public
  messages do not contain provider responses or credentials. Generation
  condition failures raise `ObjectGenerationMismatchError`.

Streaming writes stage one block at a time with unique IDs for each upload,
then commit exactly that upload's block list after validating its declared
size. They do not publish partial content. Memory scales with the configured
chunk buffers and a bounded block-ID list, rather than the complete object.
The service permits at most 50,000 committed blocks and, for supported modern
API versions, up to 4,000 MiB per block. See Microsoft's
[block commit specification](https://learn.microsoft.com/en-us/rest/api/storageservices/put-block-list).

An interrupted or failed upload leaves its blocks uncommitted and preserves
the existing committed object. Azure has no per-upload block abort API.
CogniStore does not clear the target's block list or delete the target as
cleanup, because those actions could overwrite or remove another writer's
object. Azure garbage-collects uncommitted blocks after a week without a
successful block upload or block-list commit for that blob; ongoing writes can
extend this interval. See the [provider's cleanup rules](https://learn.microsoft.com/en-us/rest/api/storageservices/put-block-list#remarks).

Other tools can leave pending blocks with a different block-ID length on the
same blob. Azure can reject subsequent staging with `InvalidBlobOrBlock` until
that upload finishes or its pending blocks expire. The existing committed
object remains intact; CogniStore does not discard another tool's pending work.

## Emulator and live validation

The default environment-independent suite uses an in-process storage double
for Azure contract coverage. Integration tests exercise the same contract
against an explicitly configured Azurite or Azure account, plus pagination,
block visibility, concurrent writes, and failed-upload preservation.

To start the repository-managed Azurite service and run its integration tests:

```bash
python -m pip install -e '.[dev,azure]'
docker compose --profile azure up -d --wait azurite
# Public disposable emulator credentials, not a production storage key.
export COGNISTORE_AZURITE_CONNECTION_STRING='DefaultEndpointsProtocol=http;AccountName=cognistore;AccountKey=Y29nbmlzdG9yZS1henVyaXRlLWRldmVsb3BtZW50LW9ubHk=;BlobEndpoint=http://127.0.0.1:10000/cognistore;'
python -m pytest -q -m integration tests/integration/test_azure_blob.py -k azurite
```

The complete [Docker integration stack](setup_guide.md#run-the-integration-suite)
also configures Azurite for its test runner. Without an emulator connection
string, emulator tests skip. Azure SDK packages are required for Azure tests.

Live tests require an explicit opt-in and a dedicated account that permits
container creation and deletion. Choose exactly one authentication path:

```bash
# Identity path: configure DefaultAzureCredential for the dedicated test account.
export COGNISTORE_AZURE_LIVE=1
export COGNISTORE_AZURE_ACCOUNT_URL=https://example.blob.core.windows.net
unset COGNISTORE_AZURE_CONNECTION_STRING
python -m pytest -q -m integration tests/integration/test_azure_blob.py -k live
```

For connection-string authentication, unset `COGNISTORE_AZURE_ACCOUNT_URL` and
set `COGNISTORE_AZURE_CONNECTION_STRING` instead, retaining
`COGNISTORE_AZURE_LIVE=1`. Tests create unique
`cognistore-azure-test-<uuid>` containers and delete only their own containers.
Azure live access is never inferred from ambient credentials without that
opt-in. Azurite is a development emulator; use the live path to validate the
configured Azure account's actual permissions and service behavior.
