# Ticket #166 pilot preparation and entry assessment

**Status: repository preparation implemented; technical no-go; pilot not started.**
The named owner's entry decision is still pending. No enrollment, data admission,
external deployment, live workload, operator acceptance or measured exit decision
was performed. This evidence does not complete #166 or the M5 epic.

The [operating procedure](../../../pilot_operations.md),
[campaign template](../../../../release/pilot/campaign.example.json) and
[offline checker](../../../../scripts/pilot_review.py) provide the entry review,
bounded cohort, daily observations, user tasks, feedback, stop/rollback and exit
handoff. The checker verifies retained file hashes, exact identity bindings,
declared reviews and duration/sample/task arithmetic. It does not verify the
truth of raw measurements, reviewer signatures, remote retention or live controls.
Even a complete record cannot authorize pilot entry, resume or expansion.
A failed/stopped pilot's truthful record remains incomplete for success readiness.

## Current gate assessment

[readiness-assessment.json](readiness-assessment.json) binds the assessment to
repository revision `714ea55adb7568fd670e89f66f57dc5c832468d2` and source evidence
hashes. It records prioritized blockers, owning tickets and concrete next actions.
[tracker-snapshot.json](tracker-snapshot.json) retains the read-only issue states.
Some preparation tickets are administratively closed; their actual retained
qualification gaps still block pilot entry.

- #159: exact specification review and operational-role acceptance are pending.
- #160/#162: the retained `e51cb39cc84b819ecbed43b56513888a956812cf` candidate
  predates newer fixes, has unresolved image findings (69 total, including
  3 critical and 14 high), and lacks an immutable registry manifest and durable
  binary handoff. Current-source tests cannot qualify that earlier image.
- #161: environment, owner and restricted handoff fields remain placeholders.
- #162/#163: no signed completed audit or passing selected-candidate UAT exists.
  The retained UAT has 12 passed, 2 failed and 66 incomplete cases, including
  the unresolved eager oversize-upload finding `LOCAL-163-001`.
- #164/#165: recovery, sustained load/capacity and actual alert delivery remain
  local preparation, without the required live campaigns.
- #166: no accepted roster, predeclared staffed windows, named entry approval,
  fourteen-day pilot history or measured exit exists.

Hosted CI: `skipped: user instruction; known GitHub billing/spending restriction`.
The #158 release-qualification gap remains. No hosted workflow was dispatched,
rerun, watched or polled; no restoration or billing action is requested here.

The next roadmap is ordered by these entry blockers. No new defect was
reproduced in this preparation and no pilot user needs or expansion conclusions
are invented. Actual exit findings must become prioritized linked tickets and
update the roadmap, handbook and generated mirror. #166 remains open.

## Local validation

**404 tests passed, zero failures or skips:** 122 pilot-record regressions and
282 related recovery/load-review and ticket-mirror tests. Repository Ruff,
mypy (164 application files), Bandit and whitespace checks passed.

[validation.json](validation.json) records the exact implementation identities,
commands, results and material gaps. [tests.log.gz](tests.log.gz) and
[tests.xml.gz](tests.xml.gz) retain the local regression results; fixtures use
only temporary files, fabricated identities and synthetic observations.
[unobserved-entry.json](unobserved-entry.json) and
[unobserved-exit.json](unobserved-exit.json) retain expected rejection of the
unchanged blank template. Exit code 1 is the expected negative control, not a
failed live pilot. The generated ticket mirror was refreshed from GitHub and
checked without editing ticket states or acceptance checkboxes.

Application runtime, dependencies, deployment configuration and workflow
security/regression gates are unchanged. A full product coverage run, package
rebuild, dependency audit and live integration tests were not repeated for this
offline tooling/documentation change. Local validation cannot satisfy the
candidate's outstanding hosted, staging, audit, recovery, load or pilot gates.

Verify retained files from this directory with:

```bash
shasum -a 256 -c SHA256SUMS
```
