# Ticket #163 upload failure follow-up

`LOCAL-163-001` is fixed in `cbefab12a273d3a5cd7345f1d87c84afe044cdbe`,
based on `04ee37a499d58695645d739971fadc72ab5af459`. This is local development
verification of the upload failure, not staging or release qualification.
The earlier [September 21 acceptance record](../ticket-163/README.md) is
preserved unchanged, including its failed observations and other unperformed
acceptance cases. GitHub #163 was already closed when this follow-up began.

## Cause and fix

The API rejected an oversized `Content-Length` without consuming the request
body and returned `Connection: close`. Uvicorn completed the response and closed
the transport while the eager HTTPX client was still writing, causing a TCP
reset before the client could inspect the error. In-process `TestClient` cases
had used an empty body with a fabricated large length and did not cover this
transport behavior.

The ASGI middleware sends the 413 status, headers and error bytes immediately,
then discards unread request data before sending the final ASGI body message.
This preserves Uvicorn's read flow control until cleanup completes (see
[Uvicorn server behavior](https://uvicorn.dev/server-behavior/)). Cleanup is
bounded to one second and 17 MiB, allowing a final transport chunk to cross the
byte budget. It stores no accumulated body, yields for cancellation even with
synchronously returned empty chunks, and serializes access to the receiver.
Other status codes do not cause cleanup reads. Authentication, permissions,
upload limits, storage and catalog mutation behavior are unchanged.

The full error is sent before cleanup reads, so `Expect: 100-continue` clients
receive the final rejection without uploading their body. Arbitrarily large or
slow senders may still see a transport close after the cleanup budget expires.
The [request-limit documentation](../../../rest_api.md#request-limits-and-metadata)
records this boundary.

## Reproduction and rerun

The unchanged local helper was run against separate fresh loopback-only
SQLite/POSIX fixtures with synthetic JWT identities and two tenants. Optional
model providers were absent. No account, billing credentials, cloud resources
or external test targets were used.

- [Baseline rehearsal](rehearsal-baseline.json.gz): 114 observations, 112 passed
  and two failed. Both tenants reproduced `ReadError: [Errno 54] Connection
  reset by peer` on an eager 16 MiB + 1-byte upload.
- [Fixed rehearsal](rehearsal-fixed.json.gz): all 114 observations passed,
  including HTTP 413 for both formerly failing uploads. These are local
  observations, not completion of the full manual acceptance matrix.
- [Socket baseline](socket-baseline.log.gz): four paced eager sender cases
  exposed premature connection closure. On this timing, HTTPX suppressed the
  write failure and could read the 413, so the regression also requires the
  bounded sender to finish. The helper above separately reproduces the original
  visible connection reset.
- [Socket rerun](socket-fixed.log.gz): all 20 tests passed against h11 and
  httptools. Coverage includes eager, paced and chunked sends, no new object on
  rejection, unchanged bytes/generation/catalog on rejected overwrite, valid
  retry, exact 16 MiB acceptance, `Expect` without an interim 100, and JSON
  request limits.

Both helper servers were stopped after collecting evidence. Socket tests start
and stop their own temporary loopback servers. Private tokens, keys, databases
and object roots are excluded from this directory.

## Validation

See [validation.json](validation.json) for exact commands, source hashes,
runtime identity, test totals, coverage, reports and limitations. The tested
application and regression-test contents match the fix commit above; evidence
documentation was added afterward.

The broad local campaign recorded **6,325 passed, one pre-existing failure,
36 skipped and 280 deselected**, with **86.86% coverage** (80% required).
All 35 new cleanup/socket regression cases passed within that campaign.
[Full log](local-suite.log.gz), [JUnit](local-suite.xml.gz) and
[coverage](coverage.xml.gz) retain the exact outcomes.

Ruff, Bandit, package build and Twine checks passed. Mypy reports the same 17
pre-existing diagnostics in six database files on both unchanged `main` and
the fix; [baseline](mypy-baseline.txt.gz) and [fixed](mypy-fixed.txt.gz) logs retain
the comparison. The additional middleware introduces no mypy diagnostic.

The broader test campaign also exposes a pre-existing telemetry redirect test
failure: OpenTelemetry exporter 1.45.0 removed the private `_session` attribute
that the test inspects. The targeted test fails identically on unchanged main
and on the upload fix ([baseline](telemetry-baseline.log.gz),
[fixed](telemetry-fixed.log.gz)). Production passes its protected session through
the supported constructor argument. This follow-up does not modify that test
or claim the complete local suite passes.

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
External-service integration tests, production configuration, production
providers, browser acceptance and release qualification were not rerun for this
scoped transport fix. Ticket closure does not certify those gates.

Verify this evidence directory with `shasum -a 256 -c SHA256SUMS`.
