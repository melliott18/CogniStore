# Runtime secrets and key providers

Storage credentials can be resolved from Vault KV v2 or AWS Secrets Manager
when an operation needs them. The driver configuration contains references,
while a process-local cache limits provider calls. AWS KMS and Vault Transit
adapters also unwrap encrypted key material through the Python API. These
interfaces do not add storage encryption policy or change TLS enforcement.

The YAML integration covers storage-driver credentials. Database, NATS,
embedding-provider, and authentication configuration keep their existing
workload identity, environment, or mounted credential mechanisms; their
configuration fields do not accept these references.

Prefer each cloud SDK's native workload identity: the AWS credential chain,
Azure `DefaultAzureCredential`, or Google Application Default Credentials.
Use external secret references when a storage service requires a shared key,
connection string, or service-account material. The
[production example](https://github.com/melliott18/CogniStore/blob/main/examples/drivers.production.yaml)
contains only references and resource identifiers.

## Driver configuration

The `secrets` block belongs in the drivers YAML selected by `--drivers`, next
to `tiers`. It is not a new key in the separate CLI profiles configuration.

```yaml
secrets:
  cache_ttl_seconds: 300
  providers:
    vault:
      type: vault_kv
      url: https://vault.example.com
      token_file: /run/secrets/vault-token
      mount: secret
    aws:
      type: aws_secrets_manager
      region_name: us-east-1

tiers:
  object:
    driver: s3
    endpoint_url: https://objects.example.com
    region_name: us-east-1
    access_key_ref:
      provider: vault
      name: cognistore/storage
      field: access_key
    secret_key_ref:
      provider: vault
      name: cognistore/storage
      field: secret_key
    auto_create_bucket: false
```

Supply the Vault token in a runtime-mounted file readable only by the service
identity. A Vault agent or deployment identity component should authenticate,
renew its lease, and replace that file as needed; CogniStore does not implement
a Vault authentication service. The token file is reread on provider requests.
AWS providers use the standard SDK credential chain, so assign a workload role
instead of embedding AWS access keys in provider configuration.

Each reference has a configured `provider` name and a remote `name`. Optional
`field` selects a top-level field in JSON secret material. Optional `version`
pins a provider version. Omit `version` to follow the latest Vault KV version
or AWS Secrets Manager's `AWSCURRENT` version after cache expiry. Use the same
provider, name, and version for the fields of one credential set: CogniStore
fetches that secret once and extracts its fields from one returned version.
Separate remote secrets cannot be rotated atomically as one credential set.
Vault pins are version numbers expressed as strings, such as `version: "12"`;
AWS pins are exact `VersionId` strings, not staging labels such as `AWSPREVIOUS`.

| Driver | Reference fields | Secret material |
| --- | --- | --- |
| S3 | `access_key_ref`, `secret_key_ref`, `session_token_ref` | String fields from the same JSON secret for a matched credential set. |
| Azure Blob | `credential_ref` | Shared credential string, with `account_url`. |
| Azure Blob | `connection_string_ref` | Complete connection string; choose this instead of the account URL/credential form. |
| Google Cloud Storage | `credentials_ref` | Trusted service-account JSON material, not a path to a file. |

Use one source per credential: a reference cannot be combined with its literal
or environment-variable form. For GCS, choose `credentials_ref`, a credential
file, or ADC; emulator mode cannot use credential material. For Azure, install
the optional dependencies with `python -m pip install -e '.[azure]'`.

For example, a GCS service account can be stored as the entire JSON value in
AWS Secrets Manager and referenced without `field`:

```yaml
tiers:
  cloud:
    driver: gcs
    project: example-project
    credentials_ref:
      provider: aws
      name: cognistore/gcs-service-account
    auto_create_bucket: false
```

This fragment uses the `aws` provider block above. Do not copy the private key
or the resolved JSON into YAML, an image, a job payload, or a command argument.

## Caching, rotation, and outages

The cache is local to each resolver/process, keyed by the reference. Its
lifetime is bounded by `cache_ttl_seconds` and any shorter expiry reported by
the provider. Provider expiry is a ceiling, not a request to extend the local
TTL. A cached value never becomes usable again merely because a refresh fails.
Expiry does not provide proactive revocation: a valid cached credential can
still be used until that deadline unless the storage service rejects it.
Choose a positive TTL shorter than the validity window of stored temporary
credentials; Secrets Manager does not supply a credential-expiry lease from
arbitrary fields in its stored JSON. Caches are bounded (256 entries by default
in the Python resolver); an evicted entry must be fetched again.
The cache is never persisted. A fresh process must reach its provider to load
referenced credentials; an outage at startup fails driver initialization and
requires the deployment's normal startup retry/restart policy.

New operations resolve credentials, reuse a valid cached value, and construct
a replacement backend when the resolved credentials change. A streaming read
or write pins its backend for that operation, allowing an active transfer to
finish with its original credential while later operations use the new one.
Retain the old credential's permissions for at least the cache TTL plus the
longest expected transfer and retry grace window. Immediate revocation can
still interrupt transfers; pinning does not keep a revoked credential valid.

Rotate a deployment in this order:

1. Create and authorize a replacement credential at the storage service.
2. Publish the complete credential set as one new secret version. Leave the
   previous credential valid during the overlap window.
3. For unpinned references, wait for cache expiry and verify that a new storage
   operation succeeds in every worker/API process. No image rebuild is needed.
4. Revoke the old storage credential only after the overlap window and active
   transfers have completed. Retain historical key versions while ciphertext
   still needs them.

A pinned `version` intentionally does not follow rotation. Changing a pinned
reference requires reloading/restarting the application with updated drivers
configuration; rotating the remote secret alone does not move that pin. Keep
catalog and broker storage durable across any deployment restart.

| State | Behavior |
| --- | --- |
| Valid cache entry; provider unavailable | Operations can use the cached value until its deadline. |
| Expired or empty cache; provider unavailable | Resolution fails; the operation does not fall back to expired credentials or another authentication source. |
| Missing secret, missing JSON field, or malformed provider response | Resolution fails with a generic error that excludes secret material. |
| Active stream when cache expires | The stream retains its existing backend; later operations must resolve under the normal expiry rules. |

Provider timeouts, transport failures, throttling, and server errors raise
`SecretUnavailableError` and receive bounded retries during running jobs. Denied access, missing
secrets/versions, or unusable responses raise terminal `SecretAccessError`;
invalid configuration raises terminal `SecretConfigurationError`. They follow
the worker's existing
[retry and dead-letter policy](background_workers.md#retry-and-terminal-error-policy).
Job identity, catalog move phases, and broker delivery state remain in their
existing durable stores; credentials are not checkpointed into job state.
Restore provider access and use the normal retry/redrive or move-recovery flow.
A provider outage does not grant unlimited retries, reset attempt counters, or
make object transfers resumable beyond each driver's existing guarantees.

## Least privilege

Separate the identity that rotates secrets from the service identity that reads
them. The reader does not need to create, update, enumerate, or delete secrets.
Restrict the storage credential itself to the buckets/containers and object
operations used by its tier; disable automatic bucket/container creation in
production. See the [S3](s3_driver.md), [Azure](azure_blob_driver.md), and
[GCS](gcs_driver.md) guides for storage operation requirements.

For the Vault example, grant only the KV v2 data path:

```hcl
path "secret/data/cognistore/storage" {
  capabilities = ["read"]
}
```

The API path includes `data/`, although the configured secret `name` does not.
No metadata listing capability is needed to read a known version. A Transit
key reader needs only the specific decrypt endpoint:

```hcl
path "transit/decrypt/cognistore-data" {
  capabilities = ["update"]
}
```

Keep key creation, rotation, export, and destruction permissions on a separate
administrative identity. See the official [Vault KV v2 API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2)
and [Transit API](https://developer.hashicorp.com/vault/api-docs/secret/transit).

For AWS Secrets Manager, allow `secretsmanager:GetSecretValue` on the exact
secret ARN, including its generated suffix. If a customer-managed KMS key
protects the secret, allow `kms:Decrypt` on that key ARN and authorize the
workload in its key policy. Scope that permission to Secrets Manager with
`kms:ViaService` and to the expected `kms:EncryptionContext:SecretARN`. The
adapter does not require `ListSecrets`, `PutSecretValue`, or rotation rights.
See [GetSecretValue permissions](https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html)
and [Secrets Manager encryption context](https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html).

For direct KMS key unwrapping, allow only `kms:Decrypt` on the intended KMS key
ARN and constrain the expected application encryption context. Use separate
permissions from Secrets Manager-mediated decrypt: direct calls must not be
restricted to `kms:ViaService` for Secrets Manager. Decrypt requires the same
case-sensitive context used for encryption. Context is metadata that AWS can
record in audit logs, so it must not contain secrets. See the official
[KMS Decrypt API](https://docs.aws.amazon.com/kms/latest/APIReference/API_Decrypt.html).

## Key-provider Python API

`SecretProvider` and `KeyProvider` separate retrieval from caching and callers.
The built-in implementations are `VaultKVProvider`,
`AWSSecretsManagerProvider`, `VaultTransitKeyProvider`, and `AWSKMSKeyProvider`,
exported from `cognistore.secrets`. Key providers decrypt externally produced
wrapped key material. They do not export KMS/Vault master keys or select a data
encryption policy for storage drivers.

Custom providers implement `fetch(SecretReference) -> SecretValue` or
`unwrap(KeyReference) -> SecretValue`. A `SecretValue` contains string/byte
material, optional version metadata, and an optional positive `ttl_seconds`
that limits its cache lifetime. Its string representation is redacted; only
call `reveal()` or `text()` at the consuming SDK/cryptographic boundary.

```python
from pathlib import Path

from cognistore.secrets import AWSKMSKeyProvider, KeyReference, SecretResolver

resolver = SecretResolver(
    {},
    key_providers={"kms": AWSKMSKeyProvider(region_name="us-east-1")},
    cache_ttl_seconds=300,
)
reference = KeyReference(
    provider="kms",
    name="arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    ciphertext=Path("/run/cognistore/wrapped-data-key.bin").read_bytes(),
    context=(("application", "cognistore"),),
)
key_material = resolver.resolve_key(reference)
# Pass key_material.reveal() directly to the cryptographic consumer.
# Never print, serialize, or include it in diagnostic metadata.
```

The ciphertext file is the encrypted key blob, not a plaintext key file. Use a
Vault Transit provider with `address`, `token_file`, and `mount="transit"` for
an externally wrapped Vault key. Its `ciphertext` is the ASCII `vault:vN:...`
value encoded as bytes. For a derived Transit key, the context is serialized
as compact, key-sorted JSON and base64-encoded; use that same byte sequence
when creating the ciphertext. Keep the ciphertext's key version and context
compatible with its provider; rotation does not rewrite historical ciphertext.
Cache expiry and provider failure behavior also apply to unwrapped keys. Python
callers can explicitly `invalidate(reference)` or `invalidate()` for a refresh
on the next lookup, and call `close()` when the resolver is no longer needed.
Invalidation does not revoke material already held by an active operation.

## Validation and diagnostics

Start with a dedicated test secret and a storage identity restricted to a
disposable bucket/container. Validate configuration without printing its
resolved values:

```bash
python - <<'PY'
from cognistore.drivers.driver_loader import load_drivers

load_drivers("examples/drivers.production.yaml")
print("Driver configuration loaded")
PY

# Replace the bucket with a pre-provisioned validation bucket.
# This read-only operation also exercises provider access and storage permissions.
cognistore --drivers examples/drivers.production.yaml \
  ls-tier object cognistore-validation --prefix rotation-check/
```

Loading referenced tiers performs initial secret resolution; it does not
establish that a storage request has sufficient permissions. Test an operation
before and after cache expiry,
then repeat with provider connectivity blocked: a still-valid cache can serve
the operation; an expired cache must fail. Exercise a long-running transfer
across rotation with both credential versions valid, and check recovery using
the same job ID after a bounded outage.

Resolved values are registered with the shared redactor before use, including
JSON fields and key material. Provider errors omit remote exception bodies,
request payloads, token values, and credential-bearing details. CogniStore's
structured telemetry uses allowlisted fields, and CLI diagnostics and audit
payloads use the shared redactor. Check logs, traces, errors, audit events, and
dead-letter records using synthetic sentinel credentials during validation.
The redactor retains known values in process memory for the process lifetime
so old versions remain protected after rotation; cache eviction does not zero
that memory. Binary diagnostic values are redacted rather than serialized.
Do not enable SDK HTTP body/header debug logging or add application logging of
resolved material; use provider-side status/audit tools for remote diagnostics.
