# PII detection and policy hooks

PII detection is an opt-in scan stage for supported extracted PDF and DOCX text.
It runs after document extraction and before the generation check and fenced
catalog write. It does not inspect arbitrary binary formats or certify regulatory
compliance. The built-in `regex` detector version `1` reports potential ASCII
email addresses (`EMAIL_ADDRESS`, confidence 0.9) and hyphenated US Social
Security numbers (`US_SSN`, confidence 0.85). These are heuristic scores, not
calibrated probabilities; a successful scan without findings does not prove
that the document contains no sensitive information.

## Configure detection

Add a `pii` block to the driver YAML used by CLI scans and workers:

```yaml
tiers:
  hot:
    driver: posix
    path: /data/hot
  warm:
    driver: posix
    path: /data/warm
pii:
  detectors: []
  limits:
    max_text_bytes: 4194304
    timeout_seconds: 10
    max_findings: 1000
  tenants:
    finance:
      detectors: [regex]
    default:
      detectors: [regex]
```

Omitting `pii` disables detection. Global settings apply to tenants without an
override. Tenant overrides inherit global limits; `detectors: []` explicitly
disables detection. Tenant selection uses the trusted catalog/job owner, never
an object field or caller-selected scan payload. Configured detector names must
exist in the application registry. YAML cannot import plugin code.
Workers load this configuration at startup; restart them after configuration
changes before rescanning affected objects.

All detectors for one document share the time and finding limits. Input limits
apply to the complete UTF-8 extracted text; text is never silently truncated.
The finding limit counts every emitted match before aggregation. Each attempt
uses a disposable subprocess with bounded input and result buffers and a
deadline; timed-out processes are terminated. The existing document extraction
limits apply separately before detection.

## Stored classification and privacy

The catalog's `pii_detection` metadata has a versioned, backend-independent
schema, so replacing a detector requires no database migration:

```json
{
  "schema_version": 1,
  "status": "succeeded",
  "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "detectors": [{"name": "regex", "version": "1"}],
  "findings": [{
    "type": "EMAIL_ADDRESS",
    "confidence": 0.9,
    "provenance": "regex",
    "detector": "regex",
    "detector_version": "1"
  }],
  "failure_code": null
}
```

Findings aggregate by category, detector/version, and provenance, retaining the
maximum confidence. They contain no match text, offsets, snippets, document
properties, or exception messages. Types and provenance labels are validated
against the normalized contract. Parser and detector output to Python logging,
stdout, and stderr is suppressed inside their subprocesses, including during
adapter unpickling.
Plugins are trusted application code; process isolation is a resource boundary,
not a sandbox against malicious plugins.

For every PII-enabled scan, extracted text and document properties are withheld
from catalog persistence after detection, including on failure. This also means
these scans do not supply document text to content search, embeddings, or Ask.
Original storage objects remain intact and follow existing authorization. PII
detection does not rewrite files, purge backups, or retrospectively erase old
search indexes. Rescan existing objects and rebuild dependent indexes when
enabling detection for a previously indexed deployment. Disabling detection
restores the existing extraction persistence behavior on subsequent scans.

Classification is attached to the exact full source digest and stored in the
tenant's catalog partition. A scan that changes content or extraction invalidates
omitted PII evidence. Replacing a detector or changing its configuration requires
a rescan to generate new evidence; persisted detector/version fields identify
which implementation produced each result.

## Placement policy behavior

Content policies accept ordered `pii_rules` before filename, MIME, embedding,
and size rules. For example, the CLI can retain potential email-bearing
documents in the hot tier. This local development example runs the scan inline
and previews placement:

```bash
export COGNISTORE_SECURITY_PROFILE=development
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  catalog-scan hot documents --sync
cognistore --drivers drivers.yaml --catalog-db catalog.sqlite3 \
  policy-run documents --dry-run --policy content \
  --pii-rule '{"finding_type":"EMAIL_ADDRESS","destination_tier":"hot","minimum_confidence":0.8}'
```

To submit queued policy work, omit `--dry-run` and `--catalog-db`; the worker
uses its configured catalog. API and Python SDK policy
configurations use the corresponding `pii_rules` array. Destinations must be in
the policy's allowed tiers. PII rules apply only to `content` policies.

When PII rules are configured, missing, malformed, stale, disabled, or unknown
classification causes `stay` with `required_features_unavailable` before any
other rule can move the object. A finding meeting its rule's confidence threshold
selects the destination with reason code `pii_rule`; the first matching rule
wins. Complete successful detection without a matching rule continues through
the other rules. Policies without PII rules preserve their existing behavior.
These are policy hooks, not an unconditional restriction on every manual move.

Unsupported, corrupt, encrypted, or otherwise unsuccessfully extracted documents
produce `unknown` when detection is enabled. Detector errors, invalid output,
timeouts, and limit exhaustion also produce `unknown` with a fixed failure code
and no findings. A partial result is never treated as a successful negative.

Policy previews, audit evidence, snapshots, and dataset exports carry normalized
PII features and support replay. PII policy jobs require envelope schema version
4 so an older worker rejects the job instead of ignoring its PII rules. Upgrade
workers before submitting PII policy jobs; other job configurations retain
their existing envelope versions.

## Replace a detector

Implement the pickle-compatible `PIIDetector` protocol with a `DetectorIdentity`
and `detect(text)` yielding `PIIFinding` instances. Supported categories are
available as `SUPPORTED_PII_TYPES`; provenance is one of `regex`, `rule`, `ner`,
or `classifier`. Identity names are bounded lowercase symbols and versions are
bounded numeric versions. Arbitrary extra response fields are rejected.

```python
from cognistore.core.pii import DetectorIdentity, PIIFinding
from cognistore.pii_runtime import load_pii_config

class OrganizationDetector:
    identity = DetectorIdentity("organization", "2.1")

    def detect(self, text):
        # Run the organization's classifier locally. Do not log input text.
        if "classified-account-marker" in text:
            yield PIIFinding("ACCOUNT_NUMBER", 0.95, "classifier")

config = load_pii_config(
    "drivers.yaml", detector_factories={"organization": OrganizationDetector}
)
pipeline = config.pipeline_for_tenant("finance")
# Pass pipeline as scan_catalog(..., pii_pipeline=pipeline), or config to
# build_handlers(..., pii_config=config). YAML selects [organization].
```

Define detectors in importable modules and use the usual `if __name__ ==
"__main__"` guard for standalone multiprocessing entrypoints. Factories create
fresh tenant-specific instances; configuration errors stop initialization with a
content-free error. Detector runtime failures become explicit unknown results.
