# Operator runbook drill evidence

Ticket [#73](https://github.com/melliott18/CogniStore/issues/73) is exercised by
[`operator-drill.json`](operator-drill.json), a real local run completed at
`2026-09-19T21:23:39.347829Z`. All **37 assertions and 19 commands passed**.
The reproducible harness is
[`scripts/operator_drills.py`](../../../scripts/operator_drills.py); the
operator entry point is [the handbook](../../operator_handbook.md).

The run used macOS 14.8.7 on arm64, CPython 3.12.2, and SQLite 3.45.1. A new
virtual environment installed a captured copy of the checkout non-editably,
resolved the required dependencies, and ran the installed `cognistore` entry
point outside the checkout. It did not inherit system site packages or
`COGNISTORE_*` settings. Commands explicitly selected the development security
profile, local driver configuration, and `--no-config`.

## What actually ran

| Drill | Observed result |
| --- | --- |
| Clean installation and first success | Fresh venv and package installation; uploaded, retrieved, listed, and indexed one 73-byte object; a full scoped scan was consistent. |
| Quiesced backup and restore | All synchronous CLI writers had exited. The SQLite backup API captured the catalog; both entire POSIX tier roots and configuration files were copied. The restored catalog's logical SHA-256 matched before writes, `PRAGMA integrity_check` passed, object download SHA-256 matched, and a fresh restored scan was consistent. |
| Incident and repair | A deliberately synthetic expired `prepared` move produced one `partial_job` finding. `consistency-repair --dry-run` left source and report bytes unchanged; the ordinary plan only audited its decision. Explicit `--enable-repair` completed the same job, a repeated invocation returned `resolved`, the catalog placement was `warm`, source cleanup completed, and a fresh scan was consistent. |

The fixture's SHA-256 is
`8378d0aadcd83e13c2ef2fb444b64ccceee42b618b1a068ac1b00b9e33d31960`.
The catalog reached packaged migration `0014_legal_holds` and used
`journal_mode=delete`. The harness uses the SQLite backup API even though this
particular run did not exercise a WAL catalog. The backup remained unchanged
through the incident drill. Complete command arguments, output, durations,
assertions, catalog logical digest, and backup file digests are in the JSON.

This is a fresh virtual environment and isolated configuration on an existing
OS account, not a newly created OS user. The synthetic abandoned journal does
not claim an actual worker was killed. The run covers local POSIX/SQLite in the
development profile; PostgreSQL, NATS, Kubernetes, cloud storage, TLS, OIDC,
encryption, service upgrade, and rollback were not exercised. No hosted system
or customer data was involved.

The restored backup had no nonterminal move. The repair drill then injected a
synthetic move into the original fixture, independently of the restore. It does
not qualify resuming pending moves after restoration: copying POSIX objects
changes inode/device/ctime-based generation tokens, and safe repair can
quarantine the resulting source/destination generation mismatch even when bytes
match. Such cases require reviewed reconciliation rather than editing journal
generations to force repair.

## Reproduce

From the repository root with Python 3.10+ on a supported POSIX platform and
package-index access, select unused workspace and output paths:

```sh
python3 scripts/operator_drills.py \
  --workspace /tmp/cognistore-operator-drill-reproduction \
  --evidence /tmp/cognistore-operator-evidence/operator-drill.json
```

The workspace must not exist. Existing workspace or evidence artifacts are
rejected; the harness never deletes or reuses them. It creates the evidence
parent directory. It leaves the venv, captured package source, live fixture,
backup, restored fixture, and scan reports in the selected workspace for
inspection. On failure the JSON records `status: "failed"` with the commands
and checks completed so far; only all successful checks yield `status: "passed"`.

The recorded run used `/tmp/cognistore-operator-73-final` (resolved by macOS
to `/private/tmp/cognistore-operator-73-final`). Its package source came from
base revision `7336c94ba0ac5e85a5d7869dfb4d98c13e52ae7f` with uncommitted ticket
documentation, navigation, CI and test changes, and the then-untracked harness.
The JSON records the exact dirty status at capture. The source manifest hashes
the installed package and build inputs, including the modified root README,
plus the harness; unrelated operator documentation is not an install input.
Checking out the base commit alone does not recreate the uncommitted harness.
The manifest is historical provenance, not a gate for future revisions.

## Archived artifacts and integrity

- [`operator-drill.json`](operator-drill.json): results, environment, limitations,
  and complete command transcript.
- [`operator-drill.install.log.gz`](operator-drill.install.log.gz): actual pip
  build and installation output, compressed without a timestamp.
- [`operator-drill.requirements.txt`](operator-drill.requirements.txt): installed
  distribution versions. This is an observation, not a lock file; the local
  `cognistore @ file://` entry refers to the captured source workspace.
- [`operator-drill.source-sha256.json`](operator-drill.source-sha256.json): hashes
  of captured package/build inputs and the executed harness.
- [`operator-SHA256SUMS`](operator-SHA256SUMS): hashes of these four artifacts.

Dependency resolution can change on later runs. The archived versions and
installation log identify the environment that produced this result. From
this evidence directory:

```sh
shasum -a 256 -c operator-SHA256SUMS
python3 -m json.tool operator-drill.json >/dev/null
gzip -t operator-drill.install.log.gz
```
