# Ticket #164 recovery campaign preparation

**Status: local implementation validated; live qualification remains pending.**
Do not close #164 from this evidence. The accepted #159 specification, qualified
#160 candidate and actual #161 staging environment are not recorded. No live
service fault, restore, credential/key change, deployment or operator approval
was performed. All retained test data uses local synthetic fixtures.

The [campaign runbook](../../../recovery_qualification.md) specifies 28 trial
records: three repetitions each of worker transfer/publication interruptions,
database/broker/POSIX/S3 disconnects and uncertain submission; credential/CA
rotation and both grant-revocation campaigns; and three independent full
restores. It defines complete cross-plane fencing/reconciliation, generation
quarantine, retained-key access, rollback compatibility and individual RPO/RTO
limits of 24 hours / 60 minutes.

The offline checker verifies retained file hashes and candidate/configuration/
specification bindings, per-trial tenant/connection evidence, repetitions,
independence, restoration counts and timestamps. It rejects unobserved templates,
malformed/ambiguous data and incomplete or over-target observations. Even a
complete input only becomes `complete-for-review`; production qualification and
resume authorization always remain false. Operator review of raw observations,
identity semantics, signatures, durable retention and complete live coverage
remains necessary.

The new restore regression executes five journal checkpoints, PREPARED through
CLEANUP, across default, active and former tenant partitions. Actual SQLite
backups and full POSIX-tree copies preserve catalog placements, job identity,
legal holds, access history and independently anchored audit continuity.
Copied files acquire new generations: recovery durably quarantines the original
job, preserves all storage bytes and the backup, and cannot bypass a legal hold.
This is intentionally a local application regression; it does not qualify
PostgreSQL, real S3/JetStream, provider metadata/sidecars, encryption-key service,
physical power loss, production outage time or any selected live RPO/RTO.

**Local validation: 425 tests passed, zero failures or skips.** This includes
247 evidence-checker cases, five new multi-partition restore cases and 173
existing recovery/authorization/queue/migration regressions. Repository Ruff,
mypy (164 application files), Bandit and whitespace checks passed.

[validation.json](validation.json) identifies the base revision, exact modified
source hashes, local runtime, commands, results and limitations. The retained
[regression log](tests.log.gz) and [JUnit report](tests.xml.gz) cover the checker,
new restore drill and existing movement, publication-crash, secret/TLS rotation,
worker/grant, queue/retry and schema migration regressions.
[quality.json](quality.json) retains static-check results.
[unobserved-campaign.json](unobserved-campaign.json) is the expected rejection of
a freshly generated unobserved template, not a failed live drill.

The full product suite, package rebuild and dependency audit were not repeated:
application and dependency definitions are unchanged. No live service test or
platform matrix was substituted with fixture outcomes. No accepted runtime
upgrade/rollback between release images or production recovery measurement is
claimed. Source hashes identify the tested worktree changes relative to the
recorded `origin/main` base; a base revision alone does not identify those edits.

Verify retained evidence from this directory with:

```bash
shasum -a 256 -c SHA256SUMS
```

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
Suspended hosted checks remain an explicit release-qualification gap. No hosted
workflow was dispatched, rerun, watched or polled.

## Integration validation after concurrent merges

After #163/#165 reached main at `36430dcdc35120cc793ffce811da7570d7a2535b`,
the #164 branch was rebased and the shared evidence-index conflict was resolved
by retaining all three tickets' entries. The #164 implementation and tests
were unchanged. The incoming queue/worker metrics changes justified a fresh
recovery regression run plus observability and worker-health tests.

On revision `d39a23da884fd631950220dcfe01073bcdc58a10`, **455 tests passed with
zero failures or skips**, and repository Ruff, mypy and Bandit passed.
[merge-validation.json](merge-validation.json), [test log](merge-tests.log.gz),
[JUnit report](merge-tests.xml.gz), and [static checks](merge-quality.json)
retain the commands and results. The original 425-test record remains the
historical pre-integration result; its evidence-index and README hashes refer
to that earlier snapshot. Both records retain the same live qualification gaps.
