# Ticket #155: local gate repairs under the no-spend constraint

This campaign repairs local validation gates. **M5 remains unqualified and open.**
The owner stated on 2026-09-29 Pacific / 2026-09-30 UTC: “I do not want to spend
any money.” The proposed AWS specification was not accepted. No AWS account
was inspected, cloud resource provisioned, paid service purchased, customer
traffic admitted, or hosted workflow dispatched, watched or rerun.

The full run is bound to `a03477cf53761b2b87a4043124ca49bed0d5f47b` (following
`950ba37f9fcb0c9afeea463c0b128f8ffa762d52`) on a branch based on main
`a48d7d7ce00bb8a9e4a55531b723fa899953410d`. The source inventory and validation
record identify the exact tested bytes; later evidence-only commits do not
change that implementation. The subsequent test-only extraction fixture correction
is commit `65fc9bb`; full identities are in `validation.json`. These are development
checks, not a frozen release.

## Repairs

- Seventeen database typing failures under SQLAlchemy 2.1.1 / mypy 2.3.1 are
  resolved with schema-derived scalar/result annotations. SQL, data validation,
  migrations' operations and schema heads are unchanged.
- The OpenTelemetry 1.45.0 redirect regression now uses the public exporter
  `session=` argument and exports an actual synthetic serialized span. Both
  307 and 308 responses must fail with exactly one HTTPS request and no
  plaintext forwarding. A local mutation permitting a redirect fails both
  cases. Ambient exporter compression is isolated explicitly.
- Semantic extraction tests use the unchanged 30-second production deadline;
  five explicit 50-millisecond deadline tests retain their original bounds.
- Release assembly upgrades the available bookworm `openssl`, `libssl3` and
  `tzdata` packages after a signed, fail-on-incomplete APT metadata refresh.
  A no-cache native-stage build and offline SSL/libmagic/timezone checks pass.
  This stage is not the complete application image or a release candidate.
- Current guidance links the implemented load adapter and newer historical
  evidence, and records the no-spend constraint. Sealed prior campaign records
  and their checksum manifests are preserved.

## Validation and diagnostic history

Full local suite at `a03477c`: **6,797 passed, 3 failed, 0 errors,
33 skipped and 1 expected failure**, **88.64% coverage**
(required >=80%). See [validation.json](validation.json), [JUnit](full-suite.xml.gz)
and [text log](full-suite.log.gz) for commands, scope and exact outcomes.
Ruff, mypy (165 source files), Bandit, deterministic OpenAPI, declared runtime
`pip_audit`, package build and Twine checks all pass.

The full run retains three extraction failures: two explicitly reported timeout;
the streaming-result failure did not expose its failure code. Those cases took
2.074–2.319 seconds with a two-second synthetic fixture limit. The later test-only
correction at `65fc9bb` aligns that
fixture with the unchanged 30-second production default. The complete extraction
module then passes **34/34 under coverage**, including all five unchanged
50-millisecond timeout tests. The unchanged narrow baseline also passed, so
host load is not claimed as a deterministically reproduced cause. No second
clean full-suite run is claimed. Application/package source is identical across
these revisions; the later edits affect regression-test isolation only.

The first full-suite attempt used obsolete fake-gcs-server 1.54.0 and was
intentionally stopped after reproducing two emulator generation-precondition
failures: 592 passed, 2 failed, 32 skipped and 1 expected failure. This is retained
under `initial-obsolete-gcs/` and is not described as passing. The repository's
pinned emulator source `3c29d20789f6475f65d2554ad6898c4afd7124bb` passes the same
GCS module: 20 passed, 2 skipped and 1 expected failure. No product assertion or
skip was weakened. The exact source was built and labeled before rerunning.

The native smoke harness initially assumed the wrong OpenSSL version-tuple
layout; the initial diagnostic and corrected version-string check are both
retained. The same built native image passed the corrected check. The image
configuration digest is
`sha256:b83c2ab06b815fb8f868f5b86fafe605ebaa9d9c847edd7af1886fedd581f632`.
It installs OpenSSL/libssl3 `3.0.22-1~deb12u1`, tzdata `2026c-0+deb12u1` and
libmagic1 `1:5.44-3`. This is package-remediation evidence, not a new full-image
scan. The retained prior scanner's seven available-version native remediation
leads need rechecking in a newly assembled candidate. Pip-vendored components
and other critical/high findings remain unresolved; no exception is granted.

All services were disposable Docker containers bound to localhost, using
synthetic fixtures: PostgreSQL 16/pgvector, NATS, MinIO, Azurite and the pinned
GCS emulator. No cloud account credentials were used. Public emulator fixture
credentials in the reproduction settings are test-only. Service identities,
ports, dependency versions, exact commands, logs and JUnit/coverage reports are
retained. All six task-owned containers were stopped and removed after testing;
[cleanup.json](cleanup.json) records zero remaining task containers. The native
image remains local for review; no new candidate binary bundle was uploaded.
Local paths/JWT-shaped fixture strings are sanitized; XML values are
sanitized through parsing and serialization so reports remain valid XML.

## Remaining requirements

The original milestone still requires accepted scope/owners, a clean-main
candidate including subsequent fixes, exact-image security disposition, a
verified registry identity, and selected-environment staging/audit/UAT/recovery
qualification. Its 72-hour load campaign, real operator alert receipts and
14-day/80-staffed-hour pilot have not run. Those outcomes cannot be inferred
from these local tests or administrative ticket closures.

The current AWS deployment proposal incurs charges and cannot proceed under
the no-spend instruction. Replacing it with local-only qualification requires
an explicit scope/specification decision; this campaign does not silently
weaken those gates. Hosted CI remains
**skipped: user instruction; known GitHub billing/spending restriction**.
Its explicit qualification criterion remains unmet. No M5 ticket is closed by
this evidence, and no production-readiness or pilot-entry approval is claimed.

The [current M5 index](../README.md) and [staging guide](../../../staging.md)
record the dependency chain. `SHA256SUMS` covers every retained file in this
folder except itself; test-run counts overlap and are not added together.
