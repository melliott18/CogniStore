# M1 verification and repository review — 2026-08-27

> **Closeout update:** [PR #93](https://github.com/melliott18/CogniStore/pull/93)
> closed #26, #89, and #90 with passing CI. Canonical full-profile run
> `full-20260827-205845` subsequently passed from clean revision `7961c82`.
> [PR #95](https://github.com/melliott18/CogniStore/pull/95) retained its
> [report and checksum](evidence/m1/README.md), after which #29, #16, and the M1
> milestone closed on 2026-08-29. The findings below preserve the repository
> state at the time of this historical review.

This review verifies the five-step M1 completion sequence against commit
`052487d`, the current GitHub tracker, the latest successful `main` CI run, and
fresh local validation. It supersedes the delivery-status and next-step
sections of the [2026-08-20 review](m1_review_2026-08-20.md); that earlier
document remains the historical record for the movement-race reproductions.

## Decision

At the time of this review, M1 was **not complete**. The scheduler and Docker
tickets were correctly closed, but the CLI ticket had been reopened, the
full-scale qualification remained unproven, and two newly tracked M1
follow-ups still had to be resolved before the epic could close.

| Step | Verified result | Tracker action |
| --- | --- | --- |
| #19 — recurring scheduler | Complete against all four written criteria | Kept closed; criteria checked |
| #26 — CLI standardization | Partial: three criteria pass; secret redaction fails | Reopened; redaction criterion left open |
| #28 — Docker environment | Complete against all four written criteria | Kept closed; criteria checked |
| #29 — scale and recovery qualification | Partial: harness and reduced evidence pass; no full campaign exists | Kept open; two of four criteria checked |
| #16 — close the M1 epic | Not permitted while #26, #29, #89, and #90 remain open | Kept open; child and follow-up lists reconciled |

No new S0 data-loss or corruption defect was found. The review did find an S1
availability defect in scheduled-run recovery, a secret-disclosure path in the
CLI, a broken documented full-suite test command, and previously documented
POSIX containment risk that had no durable ticket.

## Ticket verification

### #19 — recurring scheduler

All written acceptance criteria pass:

- schedules durably enqueue catalog scans and policy passes instead of running
  them inline;
- an active logical scope prevents overlapping scheduled occurrences;
- intervals, enable/disable state, and schedule payloads are strictly
  configurable; and
- the scheduler exposes a controllable clock and has restart, reservation,
  publication, interval-change, retry, redrive, and extension-point tests.

A focused scheduler, worker-runtime, and CLI run passed 90 tests. Closing #19
is correct.

The written criteria did not cover hard-process-loss recovery. If a worker
dies after an occurrence becomes `running`, the same JetStream delivery is
rejected forever, its scope remains active, and later intervals cannot run.
This fail-closed behavior prevents overlapping side effects, but requires a
safe explicit recovery workflow. [Issue #89](https://github.com/melliott18/CogniStore/issues/89)
now owns that S1 availability gap.

### #26 — CLI standardization

Configuration precedence, the mutating-command dry-run matrix, and versioned
JSON with non-zero failure status are implemented and well tested. Durable
manual moves now support caller-owned idempotency keys plus status, list, and
resume commands. The former BUG-2026-005 is fixed.

Secret redaction is incomplete. This command returns exit status `2`, but its
JSON error repeats the dummy secret verbatim:

```bash
python -m cognistore.cli --json ls bucket --base /tmp \
  --password swordfish
```

The redactor covers recognized sensitive mappings and assignments,
credential-bearing URLs, authorization and cookie headers, Bearer/Basic forms,
and PEM blocks. It does not cover a space-separated value after an unrecognized
sensitive option. That directly fails the fourth acceptance criterion, so #26
was reopened.

Three lower-severity contract gaps remain in the same ticket:

- `move-resume --dry-run` reports journal intent, not actual resumability. It
  does not check live ownership or phase-specific storage preconditions.
- The documented semantic split between exit codes `1` and `2` does not match
  every handler and preflight path. The stable contract is currently `0` for
  success and non-zero plus the JSON `error_type` for failure.
- Verbosity enabled by an environment value, profile, or file default has no
  `--no-verbose` CLI override.

### #28 — Docker environment

All written acceptance criteria pass:

- the health-gated stack starts from a clean checkout with documented commands;
- integration tests execute wholly inside Compose;
- runtime, development, NATS, and MinIO images run as non-root without baked
  secrets; and
- the shutdown probe records, inspects, and rechecks an interrupted move across
  container restart.

The latest [`main` CI run](https://github.com/melliott18/CogniStore/actions/runs/32994130655)
passed every container build, health, integration, shutdown, reduced
qualification, evidence-export, and teardown step. Closing #28 is correct.

The base images are digest-pinned, but Python requirements remain open-ended.
The stack is repeatable at the documented interface, not yet a byte-for-byte
reproducible build. Dependency locking remains delivery hardening rather than a
#28 acceptance failure.

### #29 — scale and failure qualification

The harness is implemented and the reduced profile is repeatable in CI. Three
independent artifacts from runs
[`32994297511`](https://github.com/melliott18/CogniStore/actions/runs/32994297511),
[`32993638832`](https://github.com/melliott18/CogniStore/actions/runs/32993638832),
and
[`32992886400`](https://github.com/melliott18/CogniStore/actions/runs/32992886400)
each reported:

- 48 logical moves;
- eight of eight fault scenarios recovered;
- 12 injected failure events;
- zero silent loss and zero corruption; and
- `acceptance_status: reduced_scale_only`.

The report schema contains the required environment, configuration,
throughput, p50/p95/p99 latency, failure, and recovery fields. Those two
acceptance criteria are checked.

No retained report has `acceptance_status: full_scale_passed`, and the
qualification guide explicitly states that no one-million-object run has been
executed. The full profile requires one million objects on each of the POSIX
and S3 paths. The documented canonical two-path command therefore seeds two
million objects and performs four million logical moves; additional configured
paths would add one million objects each. A documented command is not execution
evidence, so the one-million-object and manual full-scale repeatability criteria
remain open.

The harness injects storage/mover failures directly. JetStream delivery, DLQ
publication, and redrive have separate integration coverage and are not claims
made by a qualification artifact.

## Full review findings

### Release and high-severity work

| Finding | Severity | Status and owner |
| --- | --- | --- |
| Catalog scan stores a sampled digest as `sha256` and replaces metadata | S1 High | BUG-2026-004; planned in #34 |
| CLI usage errors can disclose a space-separated secret | S2 Medium; M1 acceptance blocker | BUG-2026-009; reopened #26 |
| Hard worker loss can permanently wedge a scheduled scope | S1 High | BUG-2026-010; issue #89 |
| POSIX containment remains check-then-use under concurrent symlink swaps | S1 High security hardening | BUG-2026-012; issue #91 in M4 |

The POSIX finding requires a defined tier-root threat model and race-safe
descriptor-relative operations. It is not an observed silent-corruption result
under the current isolated test topology, but it must not remain an undocumented
assumption for production.

### Medium and lower-severity work

- BUG-2026-011 / #90: plain `python -m pytest` fails collection because the
  unit and integration qualification files share one top-level module name.
  `--import-mode=importlib` proves the implementation suite itself is healthy,
  but CI must exercise the documented default command.
- #26: recovery preview reports journal state rather than readiness, the exit
  code taxonomy is inconsistent, and configured verbosity has no negative CLI
  override.
- Docker builds resolve unpinned Python dependencies, so identical source can
  produce different dependency sets over time.
- `setup.sh` remains an unused zero-byte scaffold. It is not a supported setup
  entry point; `README.md` and `docs/setup_guide.md` contain the maintained
  procedures. Remove or implement the scaffold in a future repository-cleanup
  change rather than advertising it.
- The local workspace `.venv` was created from an Anaconda Python whose own
  `readline` extension segfaults. This is a local environment defect; healthy
  CPython 3.12.2 completed the repository validation.

## Validation evidence

Validation used a clean CPython 3.12.2 environment after isolating the broken
workspace virtual environment:

- Ruff: passed.
- mypy: no issues in 42 source files.
- Full suite with collision-safe import mode: 564 passed, 10 documented
  service-dependent skips.
- Coverage: 83.78%, above the configured 80% branch threshold.
- Bandit: no medium- or high-severity findings.
- Project dependency audit: no known vulnerabilities.
- Wheel and source distribution build plus Twine metadata checks: passed.
- `docker compose --profile integration config --quiet`: passed.
- Latest `main` CI: all Python 3.10–3.14, lint/type, package, security, secret,
  Compose integration, shutdown, and reduced-qualification jobs passed.

Plain `python -m pytest` remains a known failing command until #90 is fixed;
the passing full-suite result used `--import-mode=importlib` to isolate that
collection defect from test behavior.

## M1 closure gate

Complete these in order:

1. Fix #26's secret redaction and reconcile its preview/exit documentation.
2. Implement #90 so the documented default test command is enforced by CI.
3. Implement #89's fenced, audited stale-run recovery and live hard-kill test.
4. Run #29's canonical full profile from a clean revision, retain the complete
   JSON artifact, and independently confirm `full_scale_passed` with no loss or
   corruption.
5. Check the remaining #29 and #16 criteria and close both issues only after the
   evidence is linked from the tracker.

The dependency-ordered plan beyond that gate is in the
[next-ticket execution roadmap](next_ticket_roadmap_2026-08-27.md).
