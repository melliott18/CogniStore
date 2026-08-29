# M1 closeout evidence

This directory retains the canonical full-profile qualification artifact used
to close CogniStore M1 and ticket
[#29](https://github.com/melliott18/CogniStore/issues/29). The JSON report is
preserved byte-for-byte from the isolated qualification project.

## Provenance

- Run ID: `full-20260827-205845`
- Qualified source revision:
  [`7961c82b560cc661587d20a925a63266d870b4e1`](https://github.com/melliott18/CogniStore/commit/7961c82b560cc661587d20a925a63266d870b4e1)
- Source checkout dirty: `false`
- Harness start: `2026-08-27T20:59:02.967789Z`
- Harness finish: `2026-08-29T11:31:29.371232Z`
- Report: [`full-20260827-205845.json`](full-20260827-205845.json)
- SHA-256: `0647793f7546a4996a9f5ae36d8d6c9e7310f209e2751bea8434552a42950f39`
- Schema: `cognistore.move-qualification` version `1`
- Result: `status: passed`; `acceptance_status: full_scale_passed`

The campaign ran under CPython 3.12.11 on the Linux/aarch64 Docker Desktop VM
with 10 CPUs and 16,756,506,624 bytes of memory. It used four workers, the
canonical mixed-size distribution, three bounded attempts, and the `standard`
fault profile.

## Accepted result

| Path | Backends | Seeded | Forward | Reverse | Result |
| --- | --- | ---: | ---: | ---: | --- |
| `posix` | `PosixDriver` → `PosixDriver` | 1,000,000 | 1,000,000 | 1,000,000 | passed |
| `s3` | `PosixDriver` → `S3Driver` | 1,000,000 | 1,000,000 | 1,000,000 | passed |

- 4,000,000 logical moves and 7,475,200,000 logical payload bytes moved.
- Every forward and reverse audit verified exactly 1,000,000 objects, catalog
  placements, and completed move jobs with zero objects left on the opposite
  tier.
- All eight timeout, throttling, backend-unavailability, and forced-SIGKILL
  worker-termination scenarios recovered with their sources retained.
- The bounded idempotency replay sample added zero move-journal transitions.
- Ambient failure objects, silent loss, and corruption were all zero.
- All report acceptance criteria passed.

The report proves one clean manual full-profile execution. Repeatability at
reduced scale remains covered by repeated CI artifacts. It does not claim
physical power-loss, JetStream/DLQ/redrive, multi-region cloud, or production
SLO qualification; those concerns are outside this artifact's declared scope.

## Verify

From this directory:

```bash
shasum -a 256 -c SHA256SUMS
python -m json.tool full-20260827-205845.json >/dev/null
```
