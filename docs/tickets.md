# CogniStore Ticket Mirror

> Snapshot synchronized from GitHub Issues through 2026-09-20T07:41:49Z (latest tracker update). GitHub is the source of truth; this file is a generated, read-only reference.

- **Repository:** [melliott18/CogniStore](https://github.com/melliott18/CogniStore)
- **Master tracker:** [#12](https://github.com/melliott18/CogniStore/issues/12)
- **Source roadmap:** [roadmap.md](./roadmap.md)
- **Original proposal:** [proposal.md](./proposal.md)
- **Snapshot:** 80 issues; 12 open, 68 closed

## How to use this mirror

- Start with the master tracker and milestone epic for sequencing.
- Child tickets are listed in live epic order; their **Dependencies** sections are authoritative.
- Statuses and checkboxes reflect the live GitHub issue bodies in this file.
- Make tracker changes in GitHub first, then regenerate this file.

## Synchronize and validate

Authenticate GitHub CLI, then run these commands from the repository root:

```bash
gh auth status
python scripts/sync_ticket_mirror.py --write
python scripts/sync_ticket_mirror.py --check
GITHUB_TOKEN="$(gh auth token)" lychee --no-progress docs/tickets.md docs/roadmap.md docs/next_ticket_roadmap_2026-08-27.md
git diff --check
```

`--write` replaces this mirror from live issue and milestone data. `--check` re-fetches the same data, prints a unified diff on drift, and exits nonzero without changing the file.

## Milestone index

| Milestone | Epic | Original | Follow-ups | Other | Open / total | GitHub |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| M1 – Reliable multi-backend movement | [#16](https://github.com/melliott18/CogniStore/issues/16) | 13 | 2 | 0 | 0 / 16 | [Closed milestone](https://github.com/melliott18/CogniStore/milestone/4) |
| M2 – Knowledge layer and search | [#13](https://github.com/melliott18/CogniStore/issues/13) | 13 | 3 | 0 | 0 / 17 | [Closed milestone](https://github.com/melliott18/CogniStore/milestone/1) |
| M3 – Explainable policy engine | [#15](https://github.com/melliott18/CogniStore/issues/15) | 11 | 0 | 0 | 0 / 12 | [Closed milestone](https://github.com/melliott18/CogniStore/milestone/2) |
| M4 – Production platform | [#14](https://github.com/melliott18/CogniStore/issues/14) | 20 | 1 | 0 | 0 / 22 | [Closed milestone](https://github.com/melliott18/CogniStore/milestone/3) |
| M5 – Release readiness and controlled pilot | [#155](https://github.com/melliott18/CogniStore/issues/155) | 11 | 0 | 0 | 12 / 12 | [Open milestone](https://github.com/melliott18/CogniStore/milestone/5) |

## Label taxonomy

- `area:api` — External APIs and SDKs
- `area:control-plane` — Catalog, queues, scheduling, and control-plane state
- `area:delivery` — Packaging, CI/CD, deployment, and developer experience
- `area:docs` — Operator, migration, architecture, and reference documentation
- `area:indexing` — Extraction, embeddings, search, and knowledge services
- `area:observability` — Metrics, tracing, logging, SLOs, and repair
- `area:orchestration` — Movement workflows, reliability, and throughput controls
- `area:policy` — Placement policy signals, models, guardrails, and explanations
- `area:security` — Authentication, authorization, encryption, and governance
- `area:storage` — Storage drivers, tiers, pools, and backend integrations
- `area:ui` — Administrative and user interfaces
- `bug` — Something isn't working
- `documentation` — Improvements or additions to documentation
- `roadmap` — Approved work from the CogniStore roadmap
- `type:chore` — Engineering, delivery, documentation, or maintenance work
- `type:epic` — Tracking issue that groups a milestone or major initiative
- `type:feature` — User-facing or platform capability

## Master tracker

### [#12 — Roadmap: CogniStore delivery plan](https://github.com/melliott18/CogniStore/issues/12)

- **Kind:** Master tracker
- **Status:** Closed
- **Milestone:** None
- **Labels:** `roadmap`, `type:epic`
- **Last updated:** 2026-09-20

<h4 id="issue-12-purpose">Purpose</h4>
<p>This is the top-level tracker for converting the original CogniStore roadmap into an executable GitHub Issues backlog.</p>
<p>Each formal milestone has one epic. Delivery work lives in separately scoped child issues with explicit acceptance criteria, exclusions, and dependency links.</p>
<h4 id="issue-12-tracking-conventions">Tracking conventions</h4>
<ul>
<li><strong>Milestones</strong> communicate delivery sequence: M1 through M4.</li>
<li><strong>Epic issues</strong> group work; they are not implementation tickets.</li>
<li><strong>Child issues</strong> carry implementation scope and are ordered approximately by dependency inside each epic.</li>
<li>An issue is complete only when its acceptance criteria, tests, and relevant documentation are satisfied.</li>
<li>Cross-milestone work remains assigned to the earliest milestone that needs it.</li>
</ul>
<h4 id="issue-12-milestone-epics">Milestone epics</h4>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a> — <a href="https://github.com/melliott18/CogniStore/milestone/4">M1 milestone</a>: reliable multi-backend movement</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a> — <a href="https://github.com/melliott18/CogniStore/milestone/1">M2 milestone</a>: knowledge layer and search</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a> — <a href="https://github.com/melliott18/CogniStore/milestone/2">M3 milestone</a>: explainable policy engine</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a> — <a href="https://github.com/melliott18/CogniStore/milestone/3">M4 milestone</a>: production platform</li>
</ul>
<h4 id="issue-12-backlog-inventory">Backlog inventory</h4>
<ul>
<li>M1: 15 delivery issues (13 original + 2 verification follow-ups)</li>
<li>M2: 16 tracked issues (13 original delivery issues + 3 verification/tracking follow-ups)</li>
<li>M3: 11 delivery issues</li>
<li>M4: 21 delivery/hardening issues (20 original + <a href="https://github.com/melliott18/CogniStore/issues/91">#91</a>)</li>
</ul>
<h4 id="issue-12-source-of-truth">Source of truth</h4>
<p>&quot;Backlog is maintained against the default <code>main</code> branch; GitHub Issues is the canonical tracker.&quot;</p>
<h4 id="issue-12-m1-completion">M1 completion</h4>
<p>M1 completed on 2026-08-29. Epic <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a> and milestone 4 are closed. The canonical clean full-scale qualification report is retained at <a href="https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/full-20260827-205845.json">https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/full-20260827-205845.json</a> (SHA-256 <code>0647793f7546a4996a9f5ae36d8d6c9e7310f209e2751bea8434552a42950f39</code>).</p>
<h4 id="issue-12-m2-completion">M2 completion</h4>
<p>M2 completed on 2026-09-08. Epic <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a> and <a href="https://github.com/melliott18/CogniStore/milestone/1">milestone 1</a> are closed; all 13 delivery issues and three verification/tracking follow-ups are complete. The <a href="https://github.com/melliott18/CogniStore/blob/fa0501bbc85856427b695928db2ce9cfa2887f68/docs/evidence/m2/README.md">retained closeout evidence</a> maps every milestone success criterion to the qualified source revision <code>2dcde3b70bf085c79d4b79ed9a93e280d6a92332</code>, <a href="https://github.com/melliott18/CogniStore/actions/runs/33665981824">passing CI</a>, and archived JUnit/coverage reports. M4 subsequently completed; see the M4 completion record below.</p>
<h4 id="issue-12-m3-completion">M3 completion</h4>
<p>M3 completed on 2026-09-12. Epic <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a> and <a href="https://github.com/melliott18/CogniStore/milestone/2">milestone 2</a> are closed; all eleven delivery issues (<a href="https://github.com/melliott18/CogniStore/issues/43">#43</a>–<a href="https://github.com/melliott18/CogniStore/issues/53">#53</a>) are complete. The <a href="https://github.com/melliott18/CogniStore/blob/692c8850353544c0abd240d9adcea001e0e0c945/docs/evidence/m3/README.md">retained closeout evidence</a> maps their acceptance criteria to application revision <code>fe700326f3cc99ba498536afd07b897575709d53</code>, passing local full and service-enabled integration suites, archived reports, and reproducible offline evaluations. GitHub Actions remains blocked by account billing/spending limits; no remote CI pass is claimed. M4 subsequently completed; see the M4 completion record below.</p>
<h4 id="issue-12-m4-and-roadmap-completion">M4 and roadmap completion</h4>
<p>M4 completed on 2026-09-19. Epic <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a> and <a href="https://github.com/melliott18/CogniStore/milestone/3">milestone 3</a> are closed with all twenty delivery issues and POSIX hardening <a href="https://github.com/melliott18/CogniStore/issues/91">#91</a> complete. The <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">retained closeout evidence</a> records acceptance mapping, fresh local test/quality/operations checks, historical deployment qualification, and explicit limits. Hosted CI remains blocked by account billing/spending limits; no hosted pass is claimed.</p>
<p>All four delivery milestones M1–M4 are complete. The final reconciliation also updates stale acceptance checkboxes on the 21 M4 tickets and seven previously accepted M1 tickets (<a href="https://github.com/melliott18/CogniStore/issues/17">#17</a>, <a href="https://github.com/melliott18/CogniStore/issues/18">#18</a>, <a href="https://github.com/melliott18/CogniStore/issues/20">#20</a>, <a href="https://github.com/melliott18/CogniStore/issues/22">#22</a>, <a href="https://github.com/melliott18/CogniStore/issues/23">#23</a>, <a href="https://github.com/melliott18/CogniStore/issues/24">#24</a>, <a href="https://github.com/melliott18/CogniStore/issues/25">#25</a>). Repository roadmap and live-ticket mirror are synchronized by PR <a href="https://github.com/melliott18/CogniStore/pull/154">#154</a>. No open implementation tickets remain.</p>
<h4 id="issue-12-post-delivery-qualification-2026-09-20">Post-delivery qualification — 2026-09-20</h4>
<p>The completed M1–M4 delivery is followed by <a href="https://github.com/melliott18/CogniStore/milestone/5">M5 – Release readiness and controlled pilot</a>, tracked in <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>. Its eleven child tickets cover the two newly reproduced P1 defects, CI restoration, deployment/workload definition, an immutable candidate, full-system audit, production-configured staging, manual acceptance, recovery and load qualification, and a gated pilot. The historical M1–M4 completion record remains unchanged; <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a> is the active execution tracker for the new phase.</p>

## M1 – Reliable multi-backend movement

- **GitHub milestone:** [M1 – Reliable multi-backend movement](https://github.com/melliott18/CogniStore/milestone/4)
- **Original delivery tickets:** 13
- **Verification follow-ups:** 2
- **Other tracking issues:** 0
- **Status:** 0 open, 16 closed (16 including the epic)

### Epic

#### [#16 — \[Epic\] M1 – Reliable multi-backend movement](https://github.com/melliott18/CogniStore/issues/16)

- **Kind:** Milestone epic
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:orchestration`, `roadmap`, `type:epic`
- **Last updated:** 2026-08-29

<p>Parent roadmap: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-16-outcome">Outcome</h5>
<p>CogniStore can move data between POSIX and S3-compatible tiers using a streaming, integrity-checked, idempotent workflow that is safe to preview and operate under load.</p>
<h5 id="issue-16-milestone-success-criteria">Milestone success criteria</h5>
<ul>
<li>Move 1 million small objects between hot and warm tiers without silent loss or corruption.</li>
<li>Every move is resumable or safely retryable.</li>
<li>Operators can preview decisions and inspect machine-readable results.</li>
<li>CI exercises the supported runtime and backend matrix.</li>
</ul>
<h5 id="issue-16-child-issues">Child issues</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/17">#17</a> — Implement an S3-compatible storage driver and conformance suite</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — Add a message bus and background worker runtime</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/19">#19</a> — Schedule catalog scans and policy passes</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/20">#20</a> — Enforce movement and CLI safety guardrails</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/21">#21</a> — Stream object moves with bounded memory and S3 multipart upload</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/22">#22</a> — Verify object integrity before deleting the source</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/23">#23</a> — Make move jobs idempotent with two-phase catalog updates</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/24">#24</a> — Add retry, backoff, dead-letter, and redrive handling</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/25">#25</a> — Add per-tier concurrency, rate limiting, and backpressure</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/26">#26</a> — Standardize CLI configuration, profiles, dry-run, verbose, and JSON output</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/27">#27</a> — Establish Python packaging and CI quality gates</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/28">#28</a> — Provide a Docker-based development and integration environment</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/29">#29</a> — Qualify one-million-object moves and failure recovery</li>
</ul>
<p>Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.</p>
<h5 id="issue-16-source">Source</h5>
<p><code>docs/roadmap.md</code>: M1, platform foundation, scale-out orchestration, S3 backend, CLI polish, benchmarks, packaging, and CI/CD.</p>
<h5 id="issue-16-verification-follow-ups">Verification follow-ups</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/89">#89</a> — Recover stale scheduled runs safely after worker loss</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/90">#90</a> — Make the default pytest command collect the full test suite</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/91">#91</a> — Make POSIX path containment race-safe against symlink swaps (scheduled for M4 hardening)</li>
</ul>
<p>Issues <a href="https://github.com/melliott18/CogniStore/issues/26">#26</a>, <a href="https://github.com/melliott18/CogniStore/issues/89">#89</a>, and <a href="https://github.com/melliott18/CogniStore/issues/90">#90</a> closed through <a href="https://github.com/melliott18/CogniStore/pull/93">#93</a>. <a href="https://github.com/melliott18/CogniStore/issues/29">#29</a> then closed from the independently validated canonical full-profile report retained by <a href="https://github.com/melliott18/CogniStore/pull/95">#95</a>. <a href="https://github.com/melliott18/CogniStore/issues/91">#91</a> remains scheduled for M4 hardening and is not an M1 blocker.</p>
<h5 id="issue-16-closeout-evidence">Closeout evidence</h5>
<ul>
<li>Clean qualified revision: <code>7961c82b560cc661587d20a925a63266d870b4e1</code></li>
<li>Immutable report: <a href="https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/full-20260827-205845.json">https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/full-20260827-205845.json</a></li>
<li>SHA-256: <code>0647793f7546a4996a9f5ae36d8d6c9e7310f209e2751bea8434552a42950f39</code></li>
<li>Evidence PR: <a href="https://github.com/melliott18/CogniStore/pull/95">#95</a></li>
</ul>
<p>All M1 child issues and verification follow-ups are complete.</p>

### Original delivery tickets

#### [#17 — \[M1\] Implement an S3-compatible storage driver and conformance suite](https://github.com/melliott18/CogniStore/issues/17)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:storage`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-17-goal">Goal</h5>
<p>Provide a production-oriented S3 backend that obeys the same storage contract as the POSIX driver.</p>
<h5 id="issue-17-scope">Scope</h5>
<ul>
<li>Define driver capability flags and a reusable backend conformance suite.</li>
<li>Implement put, get, ranged get, delete, paginated list, and stat for MinIO and AWS S3.</li>
<li>Load endpoint, region, bucket behavior, and credentials through configuration without logging secrets.</li>
</ul>
<h5 id="issue-17-out-of-scope">Out of scope</h5>
<ul>
<li>Multipart move orchestration; that belongs to the streaming mover ticket.</li>
<li>Azure Blob and GCS support.</li>
</ul>
<h5 id="issue-17-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The S3 driver is selectable from <code>drivers.yaml</code>.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> POSIX and S3 drivers pass the shared conformance suite.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Integration tests run against MinIO and cover pagination, ranges, missing objects, and idempotent deletes.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Configuration and supported S3 semantics are documented.</li>
</ul>
<h5 id="issue-17-dependencies">Dependencies</h5>
<ul>
<li>None.</li>
</ul>
<h5 id="issue-17-roadmap-coverage">Roadmap coverage</h5>
<p>Multi-backend storage → S3 with multipart support (core driver portion).</p>
<h5 id="issue-17-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/77">#77</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#18 — \[M1\] Add a message bus and background worker runtime](https://github.com/melliott18/CogniStore/issues/18)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:control-plane`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-18-goal">Goal</h5>
<p>Move scans and placement work out of synchronous CLI execution into a durable worker model.</p>
<h5 id="issue-18-scope">Scope</h5>
<ul>
<li>Record an ADR selecting one message-bus and worker stack.</li>
<li>Implement enqueue, claim, acknowledge, negative-acknowledge, redelivery, and graceful shutdown.</li>
<li>Carry job IDs and correlation metadata through the worker boundary.</li>
</ul>
<h5 id="issue-18-out-of-scope">Out of scope</h5>
<ul>
<li>Periodic scheduling.</li>
<li>Job-specific retry policy and dead-letter redrive.</li>
</ul>
<h5 id="issue-18-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A queued test job survives a worker restart without silent loss.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Duplicate delivery is expected and documented for consumers.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Health/readiness checks expose bus and worker status.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unit and integration tests cover enqueue, acknowledgement, redelivery, and shutdown.</li>
</ul>
<h5 id="issue-18-dependencies">Dependencies</h5>
<ul>
<li>None.</li>
</ul>
<h5 id="issue-18-roadmap-coverage">Roadmap coverage</h5>
<p>Platform foundation → message bus and background workers.</p>
<h5 id="issue-18-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/78">#78</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#19 — \[M1\] Schedule catalog scans and policy passes](https://github.com/melliott18/CogniStore/issues/19)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:control-plane`, `roadmap`, `type:feature`
- **Last updated:** 2026-08-27

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-19-goal">Goal</h5>
<p>Provide configurable periodic execution for recurring control-plane work.</p>
<h5 id="issue-19-scope">Scope</h5>
<ul>
<li>Implement a scheduler that enqueues catalog scans and policy passes.</li>
<li>Support per-job intervals, enable/disable controls, and single-run locking.</li>
<li>Provide an extension point for future repair jobs.</li>
</ul>
<h5 id="issue-19-out-of-scope">Out of scope</h5>
<ul>
<li>Repair logic itself.</li>
<li>Calendar-based UI management.</li>
</ul>
<h5 id="issue-19-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Scheduled work is enqueued rather than executed inline.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Overlapping runs of the same scoped job are prevented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Intervals and disabled jobs are configurable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests use a controllable clock and cover restart behavior.</li>
</ul>
<h5 id="issue-19-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
</ul>
<h5 id="issue-19-roadmap-coverage">Roadmap coverage</h5>
<p>Platform foundation → periodic scheduler for scans, policy passes, and repair jobs.</p>

#### [#20 — \[M1\] Enforce movement and CLI safety guardrails](https://github.com/melliott18/CogniStore/issues/20)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:orchestration`, `bug`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-20-goal">Goal</h5>
<p>Prevent known unsafe move plans and ensure preview output truthfully describes side effects.</p>
<h5 id="issue-20-scope">Scope</h5>
<ul>
<li>Reject same-source/destination moves, unknown tiers, disallowed destinations, and unsafe POSIX paths.</li>
<li>Define and enforce destination-collision behavior.</li>
<li>Make dry-run execute zero storage or catalog writes and report actions as planned rather than completed.</li>
</ul>
<h5 id="issue-20-out-of-scope">Out of scope</h5>
<ul>
<li>Streaming and integrity verification.</li>
<li>Policy learning behavior.</li>
</ul>
<h5 id="issue-20-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Regression tests prove a same-tier move cannot delete data.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Resolved POSIX paths must remain inside the configured tier root.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Allowed-tier constraints apply consistently to every policy mode.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Dry-run output is distinguishable from completed movement in human and JSON modes.</li>
</ul>
<h5 id="issue-20-dependencies">Dependencies</h5>
<ul>
<li>None.</li>
</ul>
<h5 id="issue-20-roadmap-coverage">Roadmap coverage</h5>
<p>M1 guardrails and CLI dry-run; CLI polish.</p>
<h5 id="issue-20-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/75">#75</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#21 — \[M1\] Stream object moves with bounded memory and S3 multipart upload](https://github.com/melliott18/CogniStore/issues/21)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:orchestration`, `roadmap`, `type:feature`
- **Last updated:** 2026-08-22

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-21-goal">Goal</h5>
<p>Replace whole-object buffering with a transport that scales to large objects and S3.</p>
<h5 id="issue-21-scope">Scope</h5>
<ul>
<li>Introduce streaming read/write primitives with bounded buffers.</li>
<li>Use multipart upload for S3 destinations and clean up incomplete uploads on cancellation.</li>
<li>Preserve metadata needed by the mover and catalog.</li>
</ul>
<h5 id="issue-21-out-of-scope">Out of scope</h5>
<ul>
<li>Final checksum enforcement.</li>
<li>Retry/DLQ policy and job state transitions.</li>
</ul>
<h5 id="issue-21-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Peak memory remains bounded for objects substantially larger than the configured chunk size.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> POSIX↔POSIX and POSIX↔S3 transfers pass integration tests.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Interrupted multipart uploads are aborted or resumable without orphaning parts.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Chunk size and multipart thresholds are configurable.</li>
</ul>
<h5 id="issue-21-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/17">#17</a> — [M1] Implement an S3-compatible storage driver and conformance suite</li>
</ul>
<h5 id="issue-21-roadmap-coverage">Roadmap coverage</h5>
<p>Scale-out orchestration → zero-copy/streaming where possible and S3 multipart uploads.</p>

#### [#22 — \[M1\] Verify object integrity before deleting the source](https://github.com/melliott18/CogniStore/issues/22)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:orchestration`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-22-goal">Goal</h5>
<p>Guarantee that source data is retained unless the destination has been verified.</p>
<h5 id="issue-22-scope">Scope</h5>
<ul>
<li>Compute or obtain canonical source and destination checksums.</li>
<li>Verify byte count and checksum before catalog commit or source deletion.</li>
<li>Record verification outcomes and actionable failure details.</li>
</ul>
<h5 id="issue-22-out-of-scope">Out of scope</h5>
<ul>
<li>Content-addressed deduplication.</li>
<li>Cross-replica repair.</li>
</ul>
<h5 id="issue-22-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The source is never deleted when verification fails or is incomplete.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Checksum mismatch produces a failed result with both observed digests.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests inject truncation and corruption for POSIX and S3 destinations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Verified checksum and size are persisted with the placement.</li>
</ul>
<h5 id="issue-22-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/21">#21</a> — [M1] Stream object moves with bounded memory and S3 multipart upload</li>
</ul>
<h5 id="issue-22-roadmap-coverage">Roadmap coverage</h5>
<p>Scale-out orchestration → integrity checks before/after movement.</p>
<h5 id="issue-22-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/79">#79</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#23 — \[M1\] Make move jobs idempotent with two-phase catalog updates](https://github.com/melliott18/CogniStore/issues/23)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:orchestration`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-23-goal">Goal</h5>
<p>Represent moves as durable state machines that can be repeated or recovered without duplicate destructive effects.</p>
<h5 id="issue-23-scope">Scope</h5>
<ul>
<li>Define move-job states, transitions, idempotency keys, and ownership/lease rules.</li>
<li>Implement prepare/transfer/verify/commit/cleanup phases with two-phase catalog updates.</li>
<li>Recover incomplete jobs after process failure.</li>
</ul>
<h5 id="issue-23-out-of-scope">Out of scope</h5>
<ul>
<li>Retry timing and dead-letter redrive.</li>
<li>General distributed transactions outside move placement.</li>
</ul>
<h5 id="issue-23-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Replaying the same idempotency key cannot duplicate or lose an object.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Catalog placement changes only after destination verification.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Recovery tests cover crashes at every state transition.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> State transitions and terminal reasons are queryable.</li>
</ul>
<h5 id="issue-23-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/22">#22</a> — [M1] Verify object integrity before deleting the source</li>
</ul>
<h5 id="issue-23-roadmap-coverage">Roadmap coverage</h5>
<p>Scale-out orchestration → idempotent jobs and two-phase catalog updates.</p>
<h5 id="issue-23-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/80">#80</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#24 — \[M1\] Add retry, backoff, dead-letter, and redrive handling](https://github.com/melliott18/CogniStore/issues/24)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:control-plane`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-24-goal">Goal</h5>
<p>Handle transient movement failures predictably while isolating permanent failures.</p>
<h5 id="issue-24-scope">Scope</h5>
<ul>
<li>Classify retryable and terminal errors.</li>
<li>Implement bounded exponential backoff with jitter and attempt accounting.</li>
<li>Send exhausted jobs to a dead-letter queue with an operator-triggered redrive path.</li>
</ul>
<h5 id="issue-24-out-of-scope">Out of scope</h5>
<ul>
<li>Automatic data repair.</li>
<li>User-facing administration UI.</li>
</ul>
<h5 id="issue-24-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Transient failures retry without violating move idempotency.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Terminal and exhausted jobs retain complete diagnostic context.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Redrive preserves the original idempotency key and audit chain.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests cover timeout, throttling, unavailable backend, malformed request, and exhaustion.</li>
</ul>
<h5 id="issue-24-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/23">#23</a> — [M1] Make move jobs idempotent with two-phase catalog updates</li>
</ul>
<h5 id="issue-24-roadmap-coverage">Roadmap coverage</h5>
<p>Scale-out orchestration → retries with backoff and DLQs.</p>
<h5 id="issue-24-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/81">#81</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#25 — \[M1\] Add per-tier concurrency, rate limiting, and backpressure](https://github.com/melliott18/CogniStore/issues/25)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:orchestration`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-25-goal">Goal</h5>
<p>Keep movement workloads within backend and host capacity limits.</p>
<h5 id="issue-25-scope">Scope</h5>
<ul>
<li>Add independent concurrency pools for source and destination tiers.</li>
<li>Support configurable byte/operation rate limits.</li>
<li>Propagate queue saturation as measurable backpressure rather than unbounded buffering.</li>
</ul>
<h5 id="issue-25-out-of-scope">Out of scope</h5>
<ul>
<li>Autoscaling workers in Kubernetes.</li>
<li>Policy cost/carbon budgets.</li>
</ul>
<h5 id="issue-25-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Limits can be configured per tier without restarting in-flight jobs unsafely.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Load tests demonstrate bounded queue and memory growth.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Fairness prevents one tier from starving unrelated tiers.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Metrics expose active jobs, queue depth, throttling, and saturation.</li>
</ul>
<h5 id="issue-25-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/21">#21</a> — [M1] Stream object moves with bounded memory and S3 multipart upload</li>
</ul>
<h5 id="issue-25-roadmap-coverage">Roadmap coverage</h5>
<p>Scale-out orchestration → concurrency pools, rate limiting, and backpressure.</p>
<h5 id="issue-25-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/82">#82</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>This is a tracker correction for previously accepted M1 work. The retained M1 scale evidence and the separately scoped live NATS/DLQ integration evidence remain the original acceptance sources; their scope is not combined.</p>

#### [#26 — \[M1\] Standardize CLI configuration, profiles, dry-run, verbose, and JSON output](https://github.com/melliott18/CogniStore/issues/26)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:api`, `roadmap`, `type:feature`
- **Last updated:** 2026-08-27

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-26-goal">Goal</h5>
<p>Make the CLI predictable for both operators and automation.</p>
<h5 id="issue-26-scope">Scope</h5>
<ul>
<li>Add a global configuration file and named profiles with documented precedence.</li>
<li>Standardize dry-run semantics across mutating commands.</li>
<li>Add verbose diagnostics and stable JSON output without mixing human text into stdout.</li>
</ul>
<h5 id="issue-26-out-of-scope">Out of scope</h5>
<ul>
<li>A graphical administration UI.</li>
<li>Remote authentication flows.</li>
</ul>
<h5 id="issue-26-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Configuration precedence is covered by tests and documented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Every mutating command either supports dry-run or clearly documents why it cannot.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> JSON output has versioned schemas and non-zero exit codes on failure.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Secrets are redacted from verbose and error output.</li>
</ul>
<h5 id="issue-26-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/20">#20</a> — [M1] Enforce movement and CLI safety guardrails</li>
</ul>
<h5 id="issue-26-roadmap-coverage">Roadmap coverage</h5>
<p>API, CLI, and UI → CLI polish.</p>
<h5 id="issue-26-verification-follow-ups-2026-08-27">Verification follow-ups — 2026-08-27</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Usage-error redaction covers space-separated values after sensitive option names without exposing the value.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <code>move-resume --dry-run</code> reports which ownership and phase-specific storage preconditions were and were not checked; it must not imply readiness when it only inspected journal state.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The exit-status contract is made consistent and tested, or its versioned documentation explicitly defines the narrower guarantee automation can rely on.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A CLI option can disable verbosity enabled by a lower-precedence environment, profile, or default value.</li>
</ul>

#### [#27 — \[M1\] Establish Python packaging and CI quality gates](https://github.com/melliott18/CogniStore/issues/27)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:delivery`, `roadmap`, `type:chore`
- **Last updated:** 2026-08-22

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-27-goal">Goal</h5>
<p>Create a reproducible package and automated quality baseline for supported Python versions.</p>
<h5 id="issue-27-scope">Scope</h5>
<ul>
<li>Populate <code>pyproject.toml</code> with build metadata, dependencies, and tool configuration.</li>
<li>Run Ruff, mypy, pytest across a Python test matrix, and collect coverage.</li>
<li>Add coverage thresholds plus dependency, secret, and static security checks.</li>
</ul>
<h5 id="issue-27-out-of-scope">Out of scope</h5>
<ul>
<li>Container image publishing and Kubernetes deployment.</li>
<li>Release automation beyond producing a verified Python artifact.</li>
</ul>
<h5 id="issue-27-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A clean checkout can build and install the package.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Pull requests run lint, type, unit, and integration checks.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Coverage and security failures block CI with actionable output.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Supported Python versions and local commands are documented.</li>
</ul>
<h5 id="issue-27-dependencies">Dependencies</h5>
<ul>
<li>None.</li>
</ul>
<h5 id="issue-27-roadmap-coverage">Roadmap coverage</h5>
<p>Delivery and DX → CI/CD; packaging baseline.</p>

#### [#28 — \[M1\] Provide a Docker-based development and integration environment](https://github.com/melliott18/CogniStore/issues/28)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:delivery`, `roadmap`, `type:chore`
- **Last updated:** 2026-08-27

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-28-goal">Goal</h5>
<p>Make the application, S3 backend, and worker infrastructure reproducible without host-specific setup.</p>
<h5 id="issue-28-scope">Scope</h5>
<ul>
<li>Build a minimal runtime image and a developer/test image.</li>
<li>Provide Compose services for CogniStore, MinIO, and the selected message bus/worker dependencies.</li>
<li>Add health checks, persistent test volumes, and one-command integration startup.</li>
</ul>
<h5 id="issue-28-out-of-scope">Out of scope</h5>
<ul>
<li>Production Kubernetes manifests.</li>
<li>Terraform-managed cloud infrastructure.</li>
</ul>
<h5 id="issue-28-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The stack starts from a clean checkout with documented commands.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Integration tests can run entirely inside the stack.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Images run as non-root and do not bake in secrets.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Shutdown leaves no incomplete test movement without a diagnosable job record.</li>
</ul>
<h5 id="issue-28-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/17">#17</a> — [M1] Implement an S3-compatible storage driver and conformance suite</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
</ul>
<h5 id="issue-28-roadmap-coverage">Roadmap coverage</h5>
<p>Delivery and DX → Docker images and production-like configuration samples (development portion).</p>

#### [#29 — \[M1\] Qualify one-million-object moves and failure recovery](https://github.com/melliott18/CogniStore/issues/29)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:observability`, `roadmap`, `type:chore`
- **Last updated:** 2026-08-29

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-29-goal">Goal</h5>
<p>Demonstrate the M1 reliability target with repeatable scale and resilience evidence.</p>
<h5 id="issue-29-scope">Scope</h5>
<ul>
<li>Create a benchmark harness for configurable object counts and mixed sizes.</li>
<li>Measure throughput plus p50/p95/p99 latency for POSIX and S3 paths.</li>
<li>Inject timeouts, worker termination, throttling, and backend unavailability and verify recovery.</li>
</ul>
<h5 id="issue-29-out-of-scope">Out of scope</h5>
<ul>
<li>Long-term production SLO alerting.</li>
<li>Multi-region cloud qualification.</li>
</ul>
<h5 id="issue-29-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A documented run moves 1 million small objects hot↔warm without silent loss or corruption.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> All injected failures respect idempotency, retry limits, and source-retention rules.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Results include environment, configuration, throughput, tail latency, failures, and recovery time.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The harness is repeatable in CI at reduced scale and manually at full scale.</li>
</ul>
<h5 id="issue-29-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/17">#17</a> — [M1] Implement an S3-compatible storage driver and conformance suite</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/22">#22</a> — [M1] Verify object integrity before deleting the source</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/23">#23</a> — [M1] Make move jobs idempotent with two-phase catalog updates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/24">#24</a> — [M1] Add retry, backoff, dead-letter, and redrive handling</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/25">#25</a> — [M1] Add per-tier concurrency, rate limiting, and backpressure</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/27">#27</a> — [M1] Establish Python packaging and CI quality gates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/28">#28</a> — [M1] Provide a Docker-based development and integration environment</li>
</ul>
<h5 id="issue-29-roadmap-coverage">Roadmap coverage</h5>
<p>M1 success criterion; benchmarks, scale tests, and chaos/resilience.</p>
<h5 id="issue-29-evidence">Evidence</h5>
<p>Canonical full-profile run <code>full-20260827-205845</code> passed and is retained on <code>main</code>.</p>
<ul>
<li>Clean source revision: <code>7961c82b560cc661587d20a925a63266d870b4e1</code> (<code>dirty: false</code>)</li>
<li>Immutable report: <a href="https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/full-20260827-205845.json">https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/full-20260827-205845.json</a></li>
<li>Evidence index: <a href="https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/README.md">https://github.com/melliott18/CogniStore/blob/5183495fbfd5082d2e7226107a4a661d8ec6879c/docs/evidence/m1/README.md</a></li>
<li>Report SHA-256: <code>0647793f7546a4996a9f5ae36d8d6c9e7310f209e2751bea8434552a42950f39</code></li>
<li>Evidence PR: <a href="https://github.com/melliott18/CogniStore/pull/95">#95</a></li>
<li>Result: <code>status: passed</code>, <code>acceptance_status: full_scale_passed</code></li>
<li>Coverage: exactly 1,000,000 objects on each required POSIX and S3-compatible path; 4,000,000 logical moves; full forward/reverse audits; 8/8 standard fault scenarios recovered; zero ambient failure objects, silent loss, or corruption</li>
</ul>
<p>Three independent reduced CI artifacts recorded in the verification comment provide repeated harness executions. The same documented command then completed this clean canonical manual full-scale execution. This satisfies the stated repeatability gate under the ticket’s recorded closure rule—retain one independently reviewed <code>full_scale_passed</code> report—without claiming multiple manual full campaigns.</p>

### Verification follow-ups

#### [#89 — \[M1\] Recover stale scheduled runs safely after worker loss](https://github.com/melliott18/CogniStore/issues/89)

- **Kind:** Verification follow-up
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:control-plane`, `area:orchestration`, `bug`, `roadmap`
- **Last updated:** 2026-08-27

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Found by the 2026-08-27 M1 verification review.</p>
<h5 id="issue-89-problem">Problem</h5>
<p>If a worker exits after a scheduled occurrence enters <code>running</code>, JetStream redelivers the same envelope and job ID, but the scheduler coordinator rejects that same occurrence indefinitely. The active job continues to own its scope, later occurrences cannot be reserved, and the delivery can redeliver forever. Lease expiry is intentionally not sufficient for automatic takeover because it does not prove that prior side effects stopped.</p>
<p>This is fail-closed and prevents overlapping side effects, but it is an S1 availability and operability gap: one hard crash can permanently halt one recurring scope.</p>
<h5 id="issue-89-scope">Scope</h5>
<ul>
<li>Add read-only list/status inspection for quarantined or stale <code>running</code> scheduled occurrences.</li>
<li>Add an explicit, audited recovery operation that requires the prior worker to be fenced or stopped.</li>
<li>Atomically transition the same job and generation to a retryable state while preserving its logical scope and identity.</li>
<li>Clear execution ownership only through that fenced recovery path.</li>
<li>Add a live JetStream test that kills a worker process after the <code>running</code> transition, restarts processing, and proves recovery without overlap.</li>
</ul>
<h5 id="issue-89-safety-constraints">Safety constraints</h5>
<ul>
<li>Never authorize takeover solely because a lease TTL expired.</li>
<li>Never create a successor occurrence while the original occurrence remains unresolved.</li>
<li>Preserve job ID, redrive generation, transition history, and operator-supplied recovery reason.</li>
</ul>
<h5 id="issue-89-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Operators can list and inspect stale/quarantined scheduled runs without direct database queries.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> An explicitly fenced recovery resumes the same occurrence without overlapping the former worker.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Recovery is durable, audited, idempotent, and safe across process restarts.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A live JetStream hard-kill/restart test proves the scope resumes and later intervals can run.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The operator runbook documents fencing, inspection, recovery, and failure handling.</li>
</ul>
<h5 id="issue-89-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — message bus and worker runtime</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/19">#19</a> — recurring scheduler</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/24">#24</a> — retry, dead-letter, and redrive handling</li>
</ul>

#### [#90 — \[M1\] Make the default pytest command collect the full test suite](https://github.com/melliott18/CogniStore/issues/90)

- **Kind:** Verification follow-up
- **Status:** Closed
- **Milestone:** M1 – Reliable multi-backend movement
- **Labels:** `area:delivery`, `bug`, `roadmap`, `type:chore`
- **Last updated:** 2026-08-27

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/16">#16</a>
Found by the 2026-08-27 M1 verification review.</p>
<h5 id="issue-90-problem">Problem</h5>
<p>The documented <code>python -m pytest</code> command fails during collection because both <code>tests/unit/test_move_qualification.py</code> and <code>tests/integration/test_move_qualification.py</code> import as the top-level module <code>test_move_qualification</code>. CI currently avoids the collision by running test groups separately.</p>
<p>A clean CPython 3.12 environment reports an import-file mismatch before executing the full suite. Running with <code>--import-mode=importlib</code> collects 573 tests and completes with 564 passed and 10 service-dependent skips.</p>
<h5 id="issue-90-scope">Scope</h5>
<ul>
<li>Make the repository's default pytest configuration collect duplicate basenames safely, or rename/package tests to guarantee unique module identities.</li>
<li>Add a CI check that invokes the documented default full-suite command.</li>
<li>Preserve explicit service-dependent skips and the branch-coverage threshold.</li>
<li>Align README and contributor commands with the enforced configuration.</li>
</ul>
<h5 id="issue-90-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <code>python -m pytest</code> collects and runs the full default suite from a clean development install.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unit and integration files may not collide through their import names.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> CI exercises the default invocation on every supported Python version or in one dedicated full-suite job.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Coverage and documented external-service skip behavior remain intact.</li>
</ul>
<h5 id="issue-90-reproduction">Reproduction</h5>
<pre><code class="language-text">import file mismatch:
imported module 'test_move_qualification' has this __file__ attribute:
  tests/integration/test_move_qualification.py
which is not the same as:
  tests/unit/test_move_qualification.py
</code></pre>

## M2 – Knowledge layer and search

- **GitHub milestone:** [M2 – Knowledge layer and search](https://github.com/melliott18/CogniStore/milestone/1)
- **Original delivery tickets:** 13
- **Verification follow-ups:** 3
- **Other tracking issues:** 0
- **Status:** 0 open, 17 closed (17 including the epic)

### Epic

#### [#13 — \[Epic\] M2 – Knowledge layer and search](https://github.com/melliott18/CogniStore/issues/13)

- **Kind:** Milestone epic
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:epic`
- **Last updated:** 2026-09-08

<p>Parent roadmap: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-13-outcome">Outcome</h5>
<p>CogniStore has a durable Postgres/pgvector catalog and can extract, index, embed, search, and answer questions about stored content through stable external APIs.</p>
<h5 id="issue-13-milestone-success-criteria">Milestone success criteria</h5>
<ul>
<li>Objects are queryable by metadata, vector similarity, and keyword.</li>
<li>PDF and DOCX content can be extracted, chunked, and deduplicated.</li>
<li>The Ask service blends retrieval signals and returns source-backed results.</li>
<li>Public APIs, a Python SDK, and a search UI expose the supported workflow.</li>
<li>Placement policies can consume MIME and embedding-derived features.</li>
</ul>
<h5 id="issue-13-child-issues">Child issues</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — Migrate the catalog to Postgres and pgvector through a DAL</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — Persist move, policy, failure, and retry audit events</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> — Detect MIME types using libmagic with safe fallback</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> — Extract normalized text and metadata from PDF and DOCX</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/34">#34</a> — Add canonical checksums, deterministic chunking, and CAS keys</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/35">#35</a> — Implement safe deduplication reference and deletion semantics</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/36">#36</a> — Generate embeddings and query them through pgvector</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/37">#37</a> — Build a rebuildable keyword search index</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/38">#38</a> — Implement hybrid metadata, vector, and keyword Ask retrieval</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — Expose a versioned REST API and OpenAPI contract</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/40">#40</a> — Publish a typed Python SDK for the public API</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/41">#41</a> — Deliver a content-search UI and end-to-end sample corpus</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/42">#42</a> — Feed MIME and embedding features into placement policies</li>
</ul>
<p>Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.</p>
<h5 id="issue-13-source">Source</h5>
<p><code>docs/roadmap.md</code>: persistent control plane, indexing and knowledge layer, external APIs, sample datasets, and the M2 success criteria.</p>
<h5 id="issue-13-verification-follow-ups">Verification follow-ups</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/100">#100</a> — Make CatalogStore read snapshots mutation-safe across backends</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/101">#101</a> — Close PostgreSQL catalog lifecycle and race regression gaps</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/102">#102</a> — Reconcile completed-ticket checklists and roadmap mirrors</li>
</ul>
<h5 id="issue-13-m2-completion">M2 completion</h5>
<p>Accepted on 2026-09-08. All 13 delivery issues (<a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>–<a href="https://github.com/melliott18/CogniStore/issues/42">#42</a>) and all three verification/tracking follow-ups (<a href="https://github.com/melliott18/CogniStore/issues/100">#100</a>–<a href="https://github.com/melliott18/CogniStore/issues/102">#102</a>) are complete. Final delivery <a href="https://github.com/melliott18/CogniStore/issues/42">#42</a> merged through <a href="https://github.com/melliott18/CogniStore/pull/119">PR #119</a>.</p>
<ul>
<li>Qualified source revision: <code>2dcde3b70bf085c79d4b79ed9a93e280d6a92332</code>.</li>
<li><a href="https://github.com/melliott18/CogniStore/actions/runs/33665981824">Passing CI run 33665981824</a>: 11/11 jobs successful, including the Python 3.10–3.14 matrix and isolated PostgreSQL/pgvector, NATS, and MinIO integration.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/fa0501bbc85856427b695928db2ce9cfa2887f68/docs/evidence/m2/README.md">Retained acceptance mapping, CI provenance, JUnit, coverage, and scope limits</a>.</li>
</ul>

### Original delivery tickets

#### [#30 — \[M2\] Migrate the catalog to Postgres and pgvector through a DAL](https://github.com/melliott18/CogniStore/issues/30)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:control-plane`, `roadmap`, `type:feature`
- **Last updated:** 2026-08-31

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-30-goal">Goal</h5>
<p>Replace the prototype catalog with a durable, migration-managed control-plane store while preserving a clean domain interface.</p>
<h5 id="issue-30-scope">Scope</h5>
<ul>
<li>Define normalized schemas for objects, placements, tiers, pools, and vector extension setup.</li>
<li>Add reversible migrations and a small transactional data-access layer.</li>
<li>Provide a documented SQLite-to-Postgres migration path for existing prototype data.</li>
</ul>
<h5 id="issue-30-out-of-scope">Out of scope</h5>
<ul>
<li>Tenant isolation and RBAC.</li>
<li>Search indexing beyond pgvector schema support.</li>
</ul>
<h5 id="issue-30-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Clean install, upgrade, downgrade, and failed-migration paths are tested.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Current catalog behavior passes backend-parity tests.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Concurrent placement updates preserve uniqueness and placement invariants.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Application code uses the DAL rather than direct database-specific SQL.</li>
</ul>
<h5 id="issue-30-dependencies">Dependencies</h5>
<ul>
<li>None.</li>
</ul>
<h5 id="issue-30-roadmap-coverage">Roadmap coverage</h5>
<p>Platform foundation → Postgres + pgvector, migrations, and DAL.</p>
<h5 id="issue-30-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/97">PR #97</a>, merged 2026-08-29.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33270327958">run 33270327958</a> (11/11 jobs successful).</li>
</ul>

#### [#31 — \[M2\] Persist move, policy, failure, and retry audit events](https://github.com/melliott18/CogniStore/issues/31)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:control-plane`, `roadmap`, `type:feature`
- **Last updated:** 2026-08-31

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-31-goal">Goal</h5>
<p>Create an append-oriented operational history that correlates decisions, jobs, objects, and outcomes.</p>
<h5 id="issue-31-scope">Scope</h5>
<ul>
<li>Define event schemas with actor, object, job, policy/version, timestamps, outcome, and structured details.</li>
<li>Write events for moves, policy decisions, failures, retries, and manual actions.</li>
<li>Provide retention configuration and query methods.</li>
</ul>
<h5 id="issue-31-out-of-scope">Out of scope</h5>
<ul>
<li>Tamper-evident or immutable storage guarantees.</li>
<li>Admin UI rendering.</li>
</ul>
<h5 id="issue-31-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Every terminal move and policy decision has a correlated event chain.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Retry and failure details remain queryable after worker restarts.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Schema evolution and retention behavior are tested.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Sensitive values are redacted before persistence.</li>
</ul>
<h5 id="issue-31-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
</ul>
<h5 id="issue-31-roadmap-coverage">Roadmap coverage</h5>
<p>Platform foundation → event/audit tables for moves, policy decisions, failures, and retries.</p>
<h5 id="issue-31-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/98">PR #98</a>, merged 2026-08-30.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33323766125">run 33323766125</a> (11/11 jobs successful).</li>
</ul>

#### [#32 — \[M2\] Detect MIME types using libmagic with safe fallback](https://github.com/melliott18/CogniStore/issues/32)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-08-31

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-32-goal">Goal</h5>
<p>Replace extension-only MIME inference with content-aware detection and provenance.</p>
<h5 id="issue-32-scope">Scope</h5>
<ul>
<li>Integrate libmagic behind an indexing adapter.</li>
<li>Retain filename inference as a documented fallback.</li>
<li>Record detector, confidence/provenance, and disagreements.</li>
</ul>
<h5 id="issue-32-out-of-scope">Out of scope</h5>
<ul>
<li>Document text extraction.</li>
<li>PII classification.</li>
</ul>
<h5 id="issue-32-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Representative binary and text fixtures produce expected MIME types.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Missing native libraries degrade without aborting an entire scan.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Malformed and empty files are handled per object.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Detection results and provenance persist in the catalog.</li>
</ul>
<h5 id="issue-32-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
</ul>
<h5 id="issue-32-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → robust MIME detection with libmagic.</p>
<h5 id="issue-32-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/99">PR #99</a>, merged 2026-08-30.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33328551830">run 33328551830</a> (11/11 jobs successful).</li>
</ul>

#### [#33 — \[M2\] Extract normalized text and metadata from PDF and DOCX](https://github.com/melliott18/CogniStore/issues/33)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-33-goal">Goal</h5>
<p>Create a bounded, pluggable document extraction pipeline for the first two roadmap formats.</p>
<h5 id="issue-33-scope">Scope</h5>
<ul>
<li>Define a parser adapter and choose/document the initial parsing runtime.</li>
<li>Extract normalized text plus core document metadata from PDF and DOCX.</li>
<li>Apply file-size, execution-time, and output-size limits and record per-object failures.</li>
</ul>
<h5 id="issue-33-out-of-scope">Out of scope</h5>
<ul>
<li>OCR and scanned-image recognition.</li>
<li>Formats beyond PDF and DOCX.</li>
</ul>
<h5 id="issue-33-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Licensed fixtures yield deterministic normalized text and metadata.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Corrupt, encrypted, and unsupported files fail without stopping unrelated indexing.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Reprocessing is idempotent and versioned by parser implementation.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Timeout and size-limit behavior is covered by tests.</li>
</ul>
<h5 id="issue-33-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> — [M2] Detect MIME types using libmagic with safe fallback</li>
</ul>
<h5 id="issue-33-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → PDF/DOCX parsing pipeline.</p>
<h5 id="issue-33-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/107">PR #107</a>, merged 2026-09-01.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33457940050">run 33457940050</a> (11/11 jobs successful).</li>
</ul>

#### [#34 — \[M2\] Add canonical checksums, deterministic chunking, and CAS keys](https://github.com/melliott18/CogniStore/issues/34)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-34-goal">Goal</h5>
<p>Represent content with stable full-object and chunk identities suitable for deduplication and retrieval.</p>
<h5 id="issue-34-scope">Scope</h5>
<ul>
<li>Compute full-object SHA-256 while streaming.</li>
<li>Define deterministic chunk boundaries and versioned chunk metadata.</li>
<li>Derive content-addressed storage keys and persist object-to-chunk mappings.</li>
</ul>
<h5 id="issue-34-out-of-scope">Out of scope</h5>
<ul>
<li>Garbage-collecting unreferenced content.</li>
<li>Model embeddings.</li>
</ul>
<h5 id="issue-34-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The same bytes always produce the same object digest, chunks, and CAS keys.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Large objects are processed with bounded memory.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Chunking version changes cannot silently mix incompatible layouts.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Catalog scan regression tests cover one-byte, empty, and multi-chunk objects.</li>
</ul>
<h5 id="issue-34-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> — [M2] Extract normalized text and metadata from PDF and DOCX</li>
</ul>
<h5 id="issue-34-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → chunking, checksums, and CAS keys.</p>
<h5 id="issue-34-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/108">PR #108</a>, merged 2026-09-01.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33536330098">run 33536330098</a> (11/11 jobs successful).</li>
</ul>

#### [#35 — \[M2\] Implement safe deduplication reference and deletion semantics](https://github.com/melliott18/CogniStore/issues/35)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-35-goal">Goal</h5>
<p>Allow logical objects to share content without allowing one deletion to break remaining references.</p>
<h5 id="issue-35-scope">Scope</h5>
<ul>
<li>Track reference counts or equivalent ownership for shared CAS objects and chunks.</li>
<li>Define logical deletion, physical reclamation eligibility, and race handling.</li>
<li>Add a non-destructive reconciliation report for reference inconsistencies.</li>
</ul>
<h5 id="issue-35-out-of-scope">Out of scope</h5>
<ul>
<li>Automated orphan deletion in production.</li>
<li>Cross-region replication.</li>
</ul>
<h5 id="issue-35-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Identical content maps to one canonical catalog blob identity and manifest while each logical object remains independently addressable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Deleting one duplicate leaves all other references readable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Concurrent create/delete tests preserve reference invariants.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Physical reclamation requires zero references and a configurable grace period.</li>
</ul>
<h5 id="issue-35-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/34">#34</a> — [M2] Add canonical checksums, deterministic chunking, and CAS keys</li>
</ul>
<h5 id="issue-35-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → checksum/dedup pipeline.</p>
<h5 id="issue-35-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/111">PR #111</a>, merged 2026-09-01.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33551954785">run 33551954785</a> (11/11 jobs successful).</li>
</ul>

#### [#36 — \[M2\] Generate embeddings and query them through pgvector](https://github.com/melliott18/CogniStore/issues/36)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-36-goal">Goal</h5>
<p>Index document chunks as versioned embeddings and expose filtered similarity search.</p>
<h5 id="issue-36-scope">Scope</h5>
<ul>
<li>Define an embedding-provider interface with sentence-transformers and API-compatible implementations.</li>
<li>Batch and retry embedding work with model/version metadata.</li>
<li>Store vectors in pgvector and support similarity queries with metadata filters.</li>
</ul>
<h5 id="issue-36-out-of-scope">Out of scope</h5>
<ul>
<li>Model fine-tuning.</li>
<li>Hybrid ranking with keyword results.</li>
</ul>
<h5 id="issue-36-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Chunks can be embedded, queried, and re-embedded reproducibly.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Incompatible model versions are never mixed in one search space.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Provider failures are retryable without duplicating vectors.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Similarity query latency and index configuration are measured on the sample corpus.</li>
</ul>
<h5 id="issue-36-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/34">#34</a> — [M2] Add canonical checksums, deterministic chunking, and CAS keys</li>
</ul>
<h5 id="issue-36-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → embeddings into pgvector.</p>
<h5 id="issue-36-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/109">PR #109</a>, merged 2026-09-01.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33552829832">run 33552829832</a> (11/11 jobs successful).</li>
</ul>

#### [#37 — \[M2\] Build a rebuildable keyword search index](https://github.com/melliott18/CogniStore/issues/37)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-37-goal">Goal</h5>
<p>Provide ranked full-text retrieval over extracted chunks and object metadata.</p>
<h5 id="issue-37-scope">Scope</h5>
<ul>
<li>Record an ADR selecting OpenSearch or a Tantivy-based implementation.</li>
<li>Index normalized chunks and searchable metadata through an adapter.</li>
<li>Support ranking, filters, updates, deletes, and full rebuild from Postgres.</li>
</ul>
<h5 id="issue-37-out-of-scope">Out of scope</h5>
<ul>
<li>Hybrid Ask ranking.</li>
<li>Multi-region search clusters.</li>
</ul>
<h5 id="issue-37-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Fixture queries return expected ranked results and metadata filters.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Updates and deletes become visible within documented consistency bounds.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The entire index can be reconstructed from the catalog.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Backend-specific failures do not corrupt catalog state.</li>
</ul>
<h5 id="issue-37-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> — [M2] Extract normalized text and metadata from PDF and DOCX</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/34">#34</a> — [M2] Add canonical checksums, deterministic chunking, and CAS keys</li>
</ul>
<h5 id="issue-37-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → keyword index.</p>
<h5 id="issue-37-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/110">PR #110</a>, merged 2026-09-01.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33554303945">run 33554303945</a> (11/11 jobs successful).</li>
</ul>

#### [#38 — \[M2\] Implement hybrid metadata, vector, and keyword Ask retrieval](https://github.com/melliott18/CogniStore/issues/38)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:indexing`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-38-goal">Goal</h5>
<p>Blend catalog, pgvector, and keyword results into grounded answers and source-backed retrieval responses.</p>
<h5 id="issue-38-scope">Scope</h5>
<ul>
<li>Define a retrieval contract with filters, limits, and score components.</li>
<li>Fuse metadata, similarity, and keyword results with deterministic ranking rules.</li>
<li>Return object/chunk citations and optionally synthesize an answer through a provider interface.</li>
</ul>
<h5 id="issue-38-out-of-scope">Out of scope</h5>
<ul>
<li>Conversation memory and autonomous agents.</li>
<li>Admin workflows unrelated to search.</li>
</ul>
<h5 id="issue-38-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Every returned passage identifies its source object and chunk.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Score components and retrieval mode are inspectable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Missing vector, keyword, or generation providers degrade to supported retrieval modes.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Golden fixture queries validate ranking and citations.</li>
</ul>
<h5 id="issue-38-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/36">#36</a> — [M2] Generate embeddings and query them through pgvector</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/37">#37</a> — [M2] Build a rebuildable keyword search index</li>
</ul>
<h5 id="issue-38-roadmap-coverage">Roadmap coverage</h5>
<p>Indexing and knowledge layer → Ask service blending metadata, vector, and keyword.</p>
<h5 id="issue-38-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/113">PR #113</a>, merged 2026-09-02.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33577346056">run 33577346056</a> (11/11 jobs successful).</li>
</ul>

#### [#39 — \[M2\] Expose a versioned REST API and OpenAPI contract](https://github.com/melliott18/CogniStore/issues/39)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:api`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-39-goal">Goal</h5>
<p>Provide a stable external interface for objects, catalog/search, policies, and asynchronous actions.</p>
<h5 id="issue-39-scope">Scope</h5>
<ul>
<li>Implement versioned FastAPI endpoints for supported object, catalog, search/Ask, policy, and action operations.</li>
<li>Standardize pagination, validation, error envelopes, and asynchronous job responses.</li>
<li>Generate and validate an OpenAPI contract; record whether gRPC has a justified near-term use case.</li>
</ul>
<h5 id="issue-39-out-of-scope">Out of scope</h5>
<ul>
<li>Python SDK implementation.</li>
<li>Authentication and tenant enforcement beyond explicit extension hooks.</li>
</ul>
<h5 id="issue-39-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Contract tests cover success, validation, pagination, missing resources, and backend failure mapping.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Long-running actions return job IDs and expose status.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> OpenAPI generation is deterministic and checked in CI.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> No endpoint bypasses the service/DAL abstractions.</li>
</ul>
<h5 id="issue-39-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/38">#38</a> — [M2] Implement hybrid metadata, vector, and keyword Ask retrieval</li>
</ul>
<h5 id="issue-39-roadmap-coverage">Roadmap coverage</h5>
<p>API, CLI, and UI → REST and/or gRPC external API (REST selected for this milestone).</p>
<h5 id="issue-39-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/114">PR #114</a>, merged 2026-09-02.</li>
<li>CI compatibility follow-up: <a href="https://github.com/melliott18/CogniStore/pull/115">PR #115</a>, merged 2026-09-02.</li>
<li>Passing completion CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33583260966">run 33583260966</a> (11/11 jobs successful). PR <a href="https://github.com/melliott18/CogniStore/pull/114">#114</a>'s original run had one lint/type failure corrected by PR <a href="https://github.com/melliott18/CogniStore/pull/115">#115</a>.</li>
</ul>

#### [#40 — \[M2\] Publish a typed Python SDK for the public API](https://github.com/melliott18/CogniStore/issues/40)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:api`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-40-goal">Goal</h5>
<p>Give Python clients a versioned, testable interface to CogniStore without coupling to server internals.</p>
<h5 id="issue-40-scope">Scope</h5>
<ul>
<li>Generate or hand-maintain typed clients from the OpenAPI contract.</li>
<li>Support configuration, timeouts, pagination, errors, and asynchronous action polling.</li>
<li>Package examples and compatibility tests.</li>
</ul>
<h5 id="issue-40-out-of-scope">Out of scope</h5>
<ul>
<li>SDKs for other languages.</li>
<li>Direct database or driver access.</li>
</ul>
<h5 id="issue-40-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> SDK contract tests run against the API test server.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> All public endpoints used by the M2 workflow have typed methods.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Version compatibility and deprecation policy are documented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Package installation and a minimal query example work from a clean environment.</li>
</ul>
<h5 id="issue-40-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
</ul>
<h5 id="issue-40-roadmap-coverage">Roadmap coverage</h5>
<p>API, CLI, and UI → Python SDK.</p>
<h5 id="issue-40-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/116">PR #116</a>, merged 2026-09-02.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33589536717">run 33589536717</a> (11/11 jobs successful).</li>
<li>Closeout verification: the installed clean-wheel example ran against the documented sample API and returned typed, cited results.</li>
</ul>

#### [#41 — \[M2\] Deliver a content-search UI and end-to-end sample corpus](https://github.com/melliott18/CogniStore/issues/41)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:ui`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-41-goal">Goal</h5>
<p>Demonstrate that users can discover stored content through the M2 knowledge layer.</p>
<h5 id="issue-41-scope">Scope</h5>
<ul>
<li>Provide search and Ask views with filters, ranked results, citations, and object metadata.</li>
<li>Create a licensed, sanitized sample corpus covering PDF, DOCX, and duplicate content.</li>
<li>Add an end-to-end workflow that ingests, indexes, searches, and opens cited results.</li>
</ul>
<h5 id="issue-41-out-of-scope">Out of scope</h5>
<ul>
<li>Driver, tier, and policy administration.</li>
<li>Conversational memory.</li>
</ul>
<h5 id="issue-41-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A fresh environment can load the sample corpus using documented steps.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Users can run keyword, vector, and Ask queries and inspect citations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Loading, empty, degraded-provider, and error states are handled.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The end-to-end workflow runs automatically at reduced scale.</li>
</ul>
<h5 id="issue-41-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/40">#40</a> — [M2] Publish a typed Python SDK for the public API</li>
</ul>
<h5 id="issue-41-roadmap-coverage">Roadmap coverage</h5>
<p>M2 success criterion → query objects by content via API/UI; sample datasets.</p>
<h5 id="issue-41-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/117">PR #117</a>, merged 2026-09-02.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33597369495">run 33597369495</a> (11/11 jobs successful).</li>
<li>Closeout verification: an isolated Compose sample loaded successfully; browser keyword, vector, Ask, empty-result, and validation-error flows passed.</li>
</ul>

#### [#42 — \[M2\] Feed MIME and embedding features into placement policies](https://github.com/melliott18/CogniStore/issues/42)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-08

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-42-goal">Goal</h5>
<p>Make the new knowledge-layer signals available to placement evaluation without coupling policy code to indexing backends.</p>
<h5 id="issue-42-scope">Scope</h5>
<ul>
<li>Define a versioned policy-feature projection for MIME and embedding-derived signals.</li>
<li>Load features through the catalog/service boundary.</li>
<li>Expose missing/stale feature state and deterministic fallbacks.</li>
</ul>
<h5 id="issue-42-out-of-scope">Out of scope</h5>
<ul>
<li>Learning-based policy training.</li>
<li>Cost/carbon optimization.</li>
</ul>
<h5 id="issue-42-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Policies can select on MIME and configured embedding-derived classifications or similarity signals.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Feature provenance and freshness are included in dry-run output.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Missing indexing providers never cause unsafe movement.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Integration tests cover reindexing and policy reevaluation.</li>
</ul>
<h5 id="issue-42-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> — [M2] Detect MIME types using libmagic with safe fallback</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/36">#36</a> — [M2] Generate embeddings and query them through pgvector</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
</ul>
<h5 id="issue-42-roadmap-coverage">Roadmap coverage</h5>
<p>M2 success criterion → policy uses MIME and embeddings features.</p>
<h5 id="issue-42-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/119">PR #119</a>, merged 2026-09-02.</li>
<li>Qualified revision: <code>2dcde3b70bf085c79d4b79ed9a93e280d6a92332</code>.</li>
<li>Passing <a href="https://github.com/melliott18/CogniStore/actions/runs/33665981824">CI run 33665981824</a>: all 11 jobs successful; Python 3.10–3.14 each passed 1,222 unit/conformance tests and 160 integration tests, with one expected S3 range-write capability skip.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/fa0501bbc85856427b695928db2ce9cfa2887f68/docs/evidence/m2/README.md">Retained closeout evidence and criterion mapping</a> covers fresh MIME/embedding selection, dry-run provenance, fail-closed provider states, and live pgvector reindexing/policy reevaluation.</li>
</ul>

### Verification follow-ups

#### [#100 — \[M2\] Make CatalogStore read snapshots mutation-safe across backends](https://github.com/melliott18/CogniStore/issues/100)

- **Kind:** Verification follow-up
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:control-plane`, `bug`, `roadmap`
- **Last updated:** 2026-08-31

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a>
Follow-up to: <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a></p>
<h5 id="issue-100-problem">Problem</h5>
<p><code>Catalog.get()</code> and <code>Catalog.list()</code> return mutable <code>ObjectRecord</code> instances that alias the in-memory catalog. A caller can mutate tier or nested metadata without using a catalog method, bypassing catalog locking and movement/scan fencing. <code>SQLCatalog</code> reconstructs detached records, so the same caller behavior does not persist on SQLite or PostgreSQL.</p>
<h5 id="issue-100-goal">Goal</h5>
<p>Give every <code>CatalogStore</code> backend identical snapshot semantics: reading catalog state must not grant an implicit mutation channel.</p>
<h5 id="issue-100-scope">Scope</h5>
<ul>
<li>Define the ownership and mutability contract for <code>ObjectRecord</code> and its metadata.</li>
<li>Make <code>get()</code> and <code>list()</code> return mutation-safe snapshots on the in-memory, SQLite, and PostgreSQL backends.</li>
<li>Add one shared backend contract suite for read isolation, equality, ordering, and existing error behavior.</li>
<li>Preserve mutation through explicit DAL methods only.</li>
</ul>
<h5 id="issue-100-out-of-scope">Out of scope</h5>
<ul>
<li>Tier/pool CRUD and pool assignment; <a href="https://github.com/melliott18/CogniStore/issues/51">#51</a> owns that model.</li>
<li>Changes to storage-driver object payload semantics.</li>
<li>Public REST response design.</li>
</ul>
<h5 id="issue-100-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Mutating a value returned by <code>get()</code> cannot alter catalog state on any backend.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Mutating a record or nested metadata returned by <code>list()</code> cannot alter catalog state or another returned record.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> In-memory, SQLite, and live PostgreSQL pass the same snapshot-semantics contract tests.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Catalog state changes remain possible only through explicit <code>CatalogStore</code> mutation methods.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Existing object, move, scan-fence, and backend-parity suites remain green.</li>
</ul>
<h5 id="issue-100-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
</ul>
<h5 id="issue-100-verification-context">Verification context</h5>
<p>The 2026-08-31 <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> follow-up audit reproduced the aliasing difference directly on the in-memory and SQL implementations.</p>
<h5 id="issue-100-roadmap-coverage">Roadmap coverage</h5>
<p>Platform foundation → durable backend-neutral catalog behavior.</p>
<h5 id="issue-100-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/103">PR #103</a>, merged 2026-08-31.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33441939790">run 33441939790</a> (11/11 jobs successful).</li>
</ul>

#### [#101 — \[M2\] Close PostgreSQL catalog lifecycle and race regression gaps](https://github.com/melliott18/CogniStore/issues/101)

- **Kind:** Verification follow-up
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:control-plane`, `area:delivery`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-02

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a>
Follow-up to: <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a></p>
<h5 id="issue-101-goal">Goal</h5>
<p>Turn the remaining PostgreSQL catalog lifecycle and concurrency assumptions from manual audit checks into required regressions.</p>
<h5 id="issue-101-scope">Scope</h5>
<ul>
<li>Run the existing scan/move coordination race scenarios against live PostgreSQL using independent catalog handles.</li>
<li>Exercise concurrent fresh-schema initialization from separate processes to verify the PostgreSQL advisory migration lock.</li>
<li>Cover current-head read-only operation, write rejection, and refusal of absent or outdated schemas.</li>
<li>Cover pgvector ownership when the extension is platform-provided versus created by CogniStore.</li>
<li>Exercise the actual prototype SQLite layout through an isolated live PostgreSQL import.</li>
<li>Add an injected SQLite migration failure that proves schema/data rollback.</li>
</ul>
<h5 id="issue-101-out-of-scope">Out of scope</h5>
<ul>
<li>New catalog features or schema changes unless a regression exposes a correctness defect.</li>
<li>Production load or failover qualification.</li>
<li>Embedding generation and vector indexes from <a href="https://github.com/melliott18/CogniStore/issues/36">#36</a>.</li>
</ul>
<h5 id="issue-101-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Live PostgreSQL passes the scanner/move fencing race suite with independent connections.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A multi-process clean start reaches one valid migration head without partial schemas or startup races.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> PostgreSQL read-only tests prove reads at head, write rejection, and absent/outdated-schema refusal.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A preinstalled <code>vector</code> extension records <code>owned=false</code> and survives downgrade; a CogniStore-owned extension follows the documented cleanup behavior.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A prototype SQLite catalog imports into fresh PostgreSQL with objects, placements, move jobs, ordered transitions, metadata, generations, migration head, and source immutability verified.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> An injected SQLite mid-migration failure leaves the prior schema and data intact.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> These tests use isolated databases and run as required live-PostgreSQL CI coverage.</li>
</ul>
<h5 id="issue-101-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
</ul>
<h5 id="issue-101-verification-context">Verification context</h5>
<p>The 2026-08-31 audit passed concurrent PostgreSQL initialization, read-only enforcement, pre-owned pgvector downgrade, and prototype SQLite import as ad-hoc checks. This ticket makes those guarantees durable and adds the unexecuted race/failure cases.</p>
<h5 id="issue-101-roadmap-coverage">Roadmap coverage</h5>
<p>Platform foundation → migration-managed PostgreSQL catalog reliability.</p>
<h5 id="issue-101-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/105">PR #105</a>, merged 2026-08-31.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33452279320">run 33452279320</a> (11/11 jobs successful).</li>
</ul>

#### [#102 — \[M2\] Reconcile completed-ticket checklists and roadmap mirrors](https://github.com/melliott18/CogniStore/issues/102)

- **Kind:** Verification follow-up
- **Status:** Closed
- **Milestone:** M2 – Knowledge layer and search
- **Labels:** `area:docs`, `documentation`, `roadmap`, `type:chore`
- **Last updated:** 2026-08-31

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-102-problem">Problem</h5>
<p>Issues <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>, <a href="https://github.com/melliott18/CogniStore/issues/31">#31</a>, and <a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> are closed through merged PRs <a href="https://github.com/melliott18/CogniStore/pull/97">#97</a>, <a href="https://github.com/melliott18/CogniStore/pull/98">#98</a>, and <a href="https://github.com/melliott18/CogniStore/pull/99">#99</a>, but their live acceptance checklists and the M2 epic remain unchecked. The repository roadmap, dated ticket mirror, and execution roadmap still describe the completed foundation waves as pending.</p>
<h5 id="issue-102-goal">Goal</h5>
<p>Make the live tracker and repository documentation accurately reflect completed M2 work and the active next wave.</p>
<h5 id="issue-102-scope">Scope</h5>
<ul>
<li>Re-verify and check the acceptance criteria in live issues <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>, <a href="https://github.com/melliott18/CogniStore/issues/31">#31</a>, and <a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> with PR and CI evidence links.</li>
<li>Check <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>, <a href="https://github.com/melliott18/CogniStore/issues/31">#31</a>, and <a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> in epic <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a>.</li>
<li>Regenerate <code>docs/tickets.md</code> from the live tracker with current counts, states, and checklists.</li>
<li>Mark the PostgreSQL DAL, audit-event persistence, and MIME detection roadmap items complete.</li>
<li>Mark Group 2 complete and Group 3 / <a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> active in the execution roadmap.</li>
<li>Document a repeatable tracker-to-mirror synchronization and validation command.</li>
</ul>
<h5 id="issue-102-out-of-scope">Out of scope</h5>
<ul>
<li>Implementation changes to <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>, <a href="https://github.com/melliott18/CogniStore/issues/31">#31</a>, or <a href="https://github.com/melliott18/CogniStore/issues/32">#32</a>.</li>
<li>Marking <a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> or later delivery tickets complete.</li>
<li>General operator runbooks owned by <a href="https://github.com/melliott18/CogniStore/issues/73">#73</a>.</li>
</ul>
<h5 id="issue-102-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Live <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>–<a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> acceptance checklists are checked and include links to their merged implementation and passing CI.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Epic <a href="https://github.com/melliott18/CogniStore/issues/13">#13</a> marks <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a>–<a href="https://github.com/melliott18/CogniStore/issues/32">#32</a> complete and includes any approved verification follow-ups.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <code>docs/tickets.md</code> reports current M2 counts, status, and acceptance state.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <code>docs/roadmap.md</code> and <code>docs/next_ticket_roadmap_2026-08-27.md</code> identify <a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> as the active delivery ticket.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A documented repeatable sync/validation procedure prevents silent tracker drift.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Markdown links and <code>git diff --check</code> pass.</li>
</ul>
<h5 id="issue-102-dependencies">Dependencies</h5>
<ul>
<li>None. PRs <a href="https://github.com/melliott18/CogniStore/pull/97">#97</a>, <a href="https://github.com/melliott18/CogniStore/pull/98">#98</a>, and <a href="https://github.com/melliott18/CogniStore/pull/99">#99</a> are the completion evidence inputs.</li>
</ul>
<h5 id="issue-102-roadmap-coverage">Roadmap coverage</h5>
<p>Delivery and DX → accurate roadmap and delivery tracking.</p>
<h5 id="issue-102-completion-evidence">Completion evidence</h5>
<ul>
<li>Implementation: <a href="https://github.com/melliott18/CogniStore/pull/104">PR #104</a>, merged 2026-08-31.</li>
<li>Passing CI: <a href="https://github.com/melliott18/CogniStore/actions/runs/33448259292">run 33448259292</a> (11/11 jobs successful).</li>
</ul>

## M3 – Explainable policy engine

- **GitHub milestone:** [M3 – Explainable policy engine](https://github.com/melliott18/CogniStore/milestone/2)
- **Original delivery tickets:** 11
- **Verification follow-ups:** 0
- **Other tracking issues:** 0
- **Status:** 0 open, 12 closed (12 including the epic)

### Epic

#### [#15 — \[Epic\] M3 – Explainable policy engine](https://github.com/melliott18/CogniStore/issues/15)

- **Kind:** Milestone epic
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:epic`
- **Last updated:** 2026-09-12

<p>Parent roadmap: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-15-outcome">Outcome</h5>
<p>Placement decisions use behavioral and content signals, remain stable under guardrails, expose structured explanations, and optimize within cost and carbon budgets.</p>
<h5 id="issue-15-milestone-success-criteria">Milestone success criteria</h5>
<ul>
<li>Automated moves do not flap between tiers.</li>
<li>Policy inputs, decisions, and reasons are reproducible and queryable.</li>
<li>Learning and LLM-assisted modes have offline evaluation and safe fallbacks.</li>
<li>Budget, locality, and sustainability constraints are enforceable and explainable.</li>
</ul>
<h5 id="issue-15-child-issues">Child issues</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/43">#43</a> — Capture access events and compute recency/frequency signals</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/44">#44</a> — Add importance tags and minimum-residency rules</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/45">#45</a> — Prevent tier flapping with hysteresis and cooldowns</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/46">#46</a> — Log versioned policy features and outcome labels</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/47">#47</a> — Train and evaluate a supervised placement baseline</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/48">#48</a> — Integrate schema-validated LLM-assisted placement decisions</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/49">#49</a> — Persist structured policy decision reasons</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/50">#50</a> — Expose placement explanations and before/after diffs</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/51">#51</a> — Model tier pools, regions, latency, cost, and carbon attributes</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/52">#52</a> — Build calibrated storage cost and carbon estimators</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/53">#53</a> — Enforce cost/carbon budgets and provide what-if simulation</li>
</ul>
<p>Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.</p>
<h5 id="issue-15-source">Source</h5>
<p><code>docs/roadmap.md</code>: policy engine v2, tier/pool abstractions, explainability, and cost/carbon modeling.</p>
<h5 id="issue-15-completion-evidence">Completion evidence</h5>
<p>Accepted on 2026-09-12 after reviewing all eleven child issues against their implementation and written acceptance criteria. All children are merged and closed. The <a href="https://github.com/melliott18/CogniStore/blob/692c8850353544c0abd240d9adcea001e0e0c945/docs/evidence/m3/README.md">retained closeout evidence</a> binds validation to application revision <code>fe700326f3cc99ba498536afd07b897575709d53</code> and archives JUnit, coverage, and reproducible offline baseline/LLM evaluations.</p>
<p>Local validation: 2,857 default tests passed (84.90% coverage), and 245 service-enabled PostgreSQL/pgvector, NATS, and MinIO integration tests passed with one unsupported-capability skip. Independent focused reviews, Ruff, mypy, OpenAPI, packaging, dependency audits, Bandit, and current-source secret scanning passed. The review corrected a stale PostgreSQL rollback assertion; application code required no changes.</p>
<p>GitHub Actions could not start jobs because of account billing/spending-limit configuration; no remote matrix result is claimed. Stability controls and modeled budgets require configuration. The learned baseline remains an offline execution-success experiment with production promotion disabled; synthetic LLM fixtures establish boundary safety, not hosted-model quality. See the evidence record for complete scope limits.</p>

### Original delivery tickets

#### [#43 — \[M3\] Capture access events and compute recency/frequency signals](https://github.com/melliott18/CogniStore/issues/43)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-43-goal">Goal</h5>
<p>Give placement policies reliable behavioral signals derived from object access history.</p>
<h5 id="issue-43-scope">Scope</h5>
<ul>
<li>Define access-event semantics for reads, writes, listings, and policy-relevant touches.</li>
<li>Persist events or aggregates and compute configurable recency/frequency windows.</li>
<li>Expose signal freshness, sampling, and missing-data state to policies.</li>
</ul>
<h5 id="issue-43-out-of-scope">Out of scope</h5>
<ul>
<li>Importance tags and residency rules.</li>
<li>Model training.</li>
</ul>
<h5 id="issue-43-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> API and supported driver access paths emit correlated events without double-counting retries.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Windowed features are reproducible for fixture histories.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Sparse or unavailable history produces documented safe defaults.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Volume, retention, and aggregation behavior are measured.</li>
</ul>
<h5 id="issue-43-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
</ul>
<h5 id="issue-43-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → recency/frequency from access logs.</p>

#### [#44 — \[M3\] Add importance tags and minimum-residency rules](https://github.com/melliott18/CogniStore/issues/44)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-44-goal">Goal</h5>
<p>Allow explicit business importance and placement residency to constrain automated movement.</p>
<h5 id="issue-44-scope">Scope</h5>
<ul>
<li>Define validated importance tags with actor and provenance.</li>
<li>Track placement start time and configurable minimum residency by tier/policy.</li>
<li>Apply both controls before optimization or learned decisions.</li>
</ul>
<h5 id="issue-44-out-of-scope">Out of scope</h5>
<ul>
<li>Hysteresis and cooldown behavior.</li>
<li>RBAC for who may set tags.</li>
</ul>
<h5 id="issue-44-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Importance and residency constraints appear in dry-run decisions.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A move cannot violate an active minimum-residency rule.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tag changes are audited and trigger deterministic reevaluation.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Boundary-time and missing-tag behavior are tested.</li>
</ul>
<h5 id="issue-44-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/43">#43</a> — [M3] Capture access events and compute recency/frequency signals</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
</ul>
<h5 id="issue-44-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → importance tags and residency timers.</p>

#### [#45 — \[M3\] Prevent tier flapping with hysteresis and cooldowns](https://github.com/melliott18/CogniStore/issues/45)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-45-goal">Goal</h5>
<p>Keep automatic placement stable when signals fluctuate near a decision boundary.</p>
<h5 id="issue-45-scope">Scope</h5>
<ul>
<li>Define policy-level hysteresis bands and post-move cooldown periods.</li>
<li>Persist enough state to apply guardrails across worker restarts.</li>
<li>Instrument suppressed moves and their reasons.</li>
</ul>
<h5 id="issue-45-out-of-scope">Out of scope</h5>
<ul>
<li>Cost/carbon budget optimization.</li>
<li>General model evaluation.</li>
</ul>
<h5 id="issue-45-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Repeated evaluation of unchanged or boundary-adjacent objects does not flap tiers.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Cooldown and hysteresis are configurable and visible in explanations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Property/soak tests cover noisy signals and clock boundaries.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Emergency or compliance overrides are explicit and audited.</li>
</ul>
<h5 id="issue-45-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/44">#44</a> — [M3] Add importance tags and minimum-residency rules</li>
</ul>
<h5 id="issue-45-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → hysteresis/cooldowns; M3 no-tier-flapping success criterion.</p>

#### [#46 — \[M3\] Log versioned policy features and outcome labels](https://github.com/melliott18/CogniStore/issues/46)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-46-goal">Goal</h5>
<p>Create a reproducible dataset contract for offline placement evaluation and supervised learning.</p>
<h5 id="issue-46-scope">Scope</h5>
<ul>
<li>Define versioned feature, decision, outcome, and label schemas.</li>
<li>Record policy/model versions and data provenance.</li>
<li>Provide privacy-aware export and validation tooling.</li>
</ul>
<h5 id="issue-46-out-of-scope">Out of scope</h5>
<ul>
<li>Training a production model.</li>
<li>Online learning.</li>
</ul>
<h5 id="issue-46-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A decision can be reconstructed from its stored feature snapshot.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Schema changes are versioned and migration-compatible.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Exports exclude configured sensitive fields and document sampling.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Validation detects missing, leaking, or temporally invalid labels.</li>
</ul>
<h5 id="issue-46-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/43">#43</a> — [M3] Capture access events and compute recency/frequency signals</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/42">#42</a> — [M2] Feed MIME and embedding features into placement policies</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-46-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → log features/labels for a supervised baseline.</p>

#### [#47 — \[M3\] Train and evaluate a supervised placement baseline](https://github.com/melliott18/CogniStore/issues/47)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-47-goal">Goal</h5>
<p>Establish a repeatable learned baseline and compare it with existing rules before any promotion.</p>
<h5 id="issue-47-scope">Scope</h5>
<ul>
<li>Select a simple interpretable baseline and version its training configuration.</li>
<li>Build repeatable offline train/evaluate commands and time-aware splits.</li>
<li>Compare accuracy, movement cost, constraint violations, and stability against rule policies.</li>
</ul>
<h5 id="issue-47-out-of-scope">Out of scope</h5>
<ul>
<li>Online training and contextual bandits.</li>
<li>Automatic promotion to production.</li>
</ul>
<h5 id="issue-47-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Training and evaluation are reproducible from a versioned dataset snapshot.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Leakage and class-imbalance checks run automatically.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A promotion threshold and safe rule fallback are documented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Evaluation artifacts capture metrics, model version, code version, and data window.</li>
</ul>
<h5 id="issue-47-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/46">#46</a> — [M3] Log versioned policy features and outcome labels</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/45">#45</a> — [M3] Prevent tier flapping with hysteresis and cooldowns</li>
</ul>
<h5 id="issue-47-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → supervised baseline and offline evaluation.</p>

#### [#48 — \[M3\] Integrate schema-validated LLM-assisted placement decisions](https://github.com/melliott18/CogniStore/issues/48)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-48-goal">Goal</h5>
<p>Replace the threshold mock with a provider-neutral, auditable LLM decision path that fails safely.</p>
<h5 id="issue-48-scope">Scope</h5>
<ul>
<li>Define provider adapters, prompt/version metadata, and a strict decision JSON schema.</li>
<li>Apply timeouts, retries appropriate to inference, redaction, and allowed-tier validation.</li>
<li>Run all existing policy guardrails after parsing and support a zero-write dry-run.</li>
</ul>
<h5 id="issue-48-out-of-scope">Out of scope</h5>
<ul>
<li>Fine-tuning and unrestricted agent/tool use.</li>
<li>Using LLM output to bypass compliance controls.</li>
</ul>
<h5 id="issue-48-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Malformed, late, or unavailable provider responses cannot trigger a move.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Deterministic fallback behavior is covered by tests.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Prompts, redacted responses, schema errors, and final decisions are auditable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> No external provider is required for the default test suite.</li>
</ul>
<h5 id="issue-48-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/45">#45</a> — [M3] Prevent tier flapping with hysteresis and cooldowns</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-48-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → LLM-assisted decisions with schema validation, dry-run, and safe fallbacks.</p>

#### [#49 — \[M3\] Persist structured policy decision reasons](https://github.com/melliott18/CogniStore/issues/49)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-49-goal">Goal</h5>
<p>Represent why a placement was proposed or suppressed in a stable, queryable schema.</p>
<h5 id="issue-49-scope">Scope</h5>
<ul>
<li>Define reason codes, decisive signals, constraints, policy/model versions, and confidence fields.</li>
<li>Persist both move and stay/suppressed decisions.</li>
<li>Link decisions to feature snapshots, audit events, and resulting jobs.</li>
</ul>
<h5 id="issue-49-out-of-scope">Out of scope</h5>
<ul>
<li>Rendering explanations in the UI.</li>
<li>Counterfactual simulation.</li>
</ul>
<h5 id="issue-49-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Every evaluated object produces a structured reason or an explicitly sampled record.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Reason schemas are versioned and documented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Secrets and raw sensitive content are excluded.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A decision can be traced from inputs through action and final outcome.</li>
</ul>
<h5 id="issue-49-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/45">#45</a> — [M3] Prevent tier flapping with hysteresis and cooldowns</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/46">#46</a> — [M3] Log versioned policy features and outcome labels</li>
</ul>
<h5 id="issue-49-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → persist structured reasons.</p>

#### [#50 — \[M3\] Expose placement explanations and before/after diffs](https://github.com/melliott18/CogniStore/issues/50)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:ui`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-50-goal">Goal</h5>
<p>Make policy behavior understandable through the API and UI before and after execution.</p>
<h5 id="issue-50-scope">Scope</h5>
<ul>
<li>Expose structured reasons and decisive signals through versioned API resources.</li>
<li>Render current versus proposed placement, changed fields, and guardrail outcomes.</li>
<li>Use one schema for dry-run and executed decisions, with execution state layered on top.</li>
</ul>
<h5 id="issue-50-out-of-scope">Out of scope</h5>
<ul>
<li>Editing policies through a visual workflow builder.</li>
<li>General administration UI.</li>
</ul>
<h5 id="issue-50-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Users can inspect why an object moved, stayed, or was suppressed.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Dry-run diffs cannot be confused with completed actions.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> API/UI handle unavailable model details without hiding rule or constraint reasons.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> End-to-end tests trace a decision through its final job outcome.</li>
</ul>
<h5 id="issue-50-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/49">#49</a> — [M3] Persist structured policy decision reasons</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/41">#41</a> — [M2] Deliver a content-search UI and end-to-end sample corpus</li>
</ul>
<h5 id="issue-50-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 → render reasons and before/after placement diffs in API/UI.</p>

#### [#51 — \[M3\] Model tier pools, regions, latency, cost, and carbon attributes](https://github.com/melliott18/CogniStore/issues/51)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:storage`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a>
Follow-up contract context: <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a></p>
<h5 id="issue-51-goal">Goal</h5>
<p>Give policies a validated topology and attribute model beyond the current hot/warm labels.</p>
<h5 id="issue-51-scope">Scope</h5>
<ul>
<li>Define pools, membership, region, latency, capacity, price, and carbon-intensity attributes.</li>
<li>Record source, units, timestamps, and freshness for measured or configured values.</li>
<li>Expose hard locality constraints separately from optimization objectives.</li>
<li>Extend the backend-neutral <code>CatalogStore</code> contract with tier/pool registration, lookup, listing, membership, and eligible-placement operations.</li>
<li>Assign, move, or clear an object placement pool atomically while preserving pool/tier integrity.</li>
<li>Define lifecycle and referential rules for active tiers/pools and historical move-journal tier names.</li>
</ul>
<h5 id="issue-51-out-of-scope">Out of scope</h5>
<ul>
<li>Cloud billing reconciliation.</li>
<li>Policy budget optimization itself.</li>
<li>General catalog snapshot semantics, which are tracked by <a href="https://github.com/melliott18/CogniStore/issues/100">#100</a>.</li>
</ul>
<h5 id="issue-51-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Invalid units, missing required topology, and stale attributes are explicit.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Policies can enumerate eligible placements without backend-specific logic.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Hard region/locality constraints always filter candidates before scoring.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Migrations and configuration examples cover multi-pool tiers.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> In-memory, SQLite, and PostgreSQL implement the same backend-neutral tier/pool contract.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Pool assignment and reassignment are atomic and preserve the composite pool/tier invariant under concurrent updates.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Application and policy callers do not require concrete <code>SQLCatalog</code> access or direct table updates for tier/pool operations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests and documentation distinguish constrained active placement references from intentionally retained historical move-journal names.</li>
</ul>
<h5 id="issue-51-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/100">#100</a> — [M2] Make CatalogStore read snapshots mutation-safe across backends</li>
</ul>
<h5 id="issue-51-roadmap-coverage">Roadmap coverage</h5>
<p>Multi-backend storage → tier/pool abstractions with regions and cost/latency/carbon attributes.</p>

#### [#52 — \[M3\] Build calibrated storage cost and carbon estimators](https://github.com/melliott18/CogniStore/issues/52)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-52-goal">Goal</h5>
<p>Estimate the financial and carbon impact of current and candidate placements using versioned assumptions.</p>
<h5 id="issue-52-scope">Scope</h5>
<ul>
<li>Estimate storage, request, transfer, and retrieval costs by tier/backend.</li>
<li>Estimate carbon using documented intensity and energy assumptions.</li>
<li>Support calibration inputs and expose uncertainty or unavailable data.</li>
</ul>
<h5 id="issue-52-out-of-scope">Out of scope</h5>
<ul>
<li>Invoice-grade accounting.</li>
<li>Budget enforcement and what-if UX.</li>
</ul>
<h5 id="issue-52-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> All units, sources, effective dates, and formulas are versioned.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Golden fixtures produce reproducible totals.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unknown attributes cannot silently become zero cost or zero carbon.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Estimator outputs are available to policies and explanations.</li>
</ul>
<h5 id="issue-52-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/51">#51</a> — [M3] Model tier pools, regions, latency, cost, and carbon attributes</li>
</ul>
<h5 id="issue-52-roadmap-coverage">Roadmap coverage</h5>
<p>Reliability, performance, cost → estimators per tier.</p>

#### [#53 — \[M3\] Enforce cost/carbon budgets and provide what-if simulation](https://github.com/melliott18/CogniStore/issues/53)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M3 – Explainable policy engine
- **Labels:** `area:policy`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-12

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/15">#15</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-53-goal">Goal</h5>
<p>Let operators test and enforce placement policies within financial and sustainability constraints.</p>
<h5 id="issue-53-scope">Scope</h5>
<ul>
<li>Define budget periods, scopes, hard limits, and soft objectives.</li>
<li>Integrate estimator outputs into candidate scoring and constraint checks.</li>
<li>Simulate policy changes without moving data and compare current versus proposed cost/carbon/placement.</li>
</ul>
<h5 id="issue-53-out-of-scope">Out of scope</h5>
<ul>
<li>Billing settlement and carbon offsets.</li>
<li>Operational paging configuration.</li>
</ul>
<h5 id="issue-53-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Hard budgets cannot be exceeded by an automated action without an explicit audited override.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> What-if runs perform zero storage/catalog mutations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Results show assumptions, deltas, affected objects, and binding constraints.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests cover competing cost, performance, locality, and carbon objectives.</li>
</ul>
<h5 id="issue-53-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/52">#52</a> — [M3] Build calibrated storage cost and carbon estimators</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/50">#50</a> — [M3] Expose placement explanations and before/after diffs</li>
</ul>
<h5 id="issue-53-roadmap-coverage">Roadmap coverage</h5>
<p>Policy engine v2 and reliability/cost → budget guardrails and what-if simulations.</p>

## M4 – Production platform

- **GitHub milestone:** [M4 – Production platform](https://github.com/melliott18/CogniStore/milestone/3)
- **Original delivery tickets:** 20
- **Verification follow-ups:** 1
- **Other tracking issues:** 0
- **Status:** 0 open, 22 closed (22 including the epic)

### Epic

#### [#14 — \[Epic\] M4 – Production platform](https://github.com/melliott18/CogniStore/issues/14)

- **Kind:** Milestone epic
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:delivery`, `roadmap`, `type:epic`
- **Last updated:** 2026-09-20

<p>Parent roadmap: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-14-outcome">Outcome</h5>
<p>CogniStore is multi-tenant, secure, observable, repairable, deployable on Kubernetes, and operable across multiple cloud storage backends.</p>
<h5 id="issue-14-milestone-success-criteria">Milestone success criteria</h5>
<ul>
<li>Tenant isolation and RBAC are enforced end to end.</li>
<li>Audit, metrics, traces, logs, SLOs, and repair workflows support production operation.</li>
<li>Azure Blob and GCS are supported alongside POSIX and S3.</li>
<li>Helm-based deployment, autoscaling, administration UI, and operator documentation are complete.</li>
</ul>
<h5 id="issue-14-child-issues">Child issues</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/54">#54</a> — Implement an Azure Blob storage driver</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/55">#55</a> — Implement a Google Cloud Storage driver</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/56">#56</a> — Add JWT and OIDC authentication</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/57">#57</a> — Define and enforce role-based authorization</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — Enforce per-tenant ownership and isolation</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/59">#59</a> — Integrate Vault/KMS-backed secrets and key providers</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/60">#60</a> — Enforce encryption at rest and in transit</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/61">#61</a> — Add pluggable PII detection and policy hooks</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/62">#62</a> — Implement legal holds and deletion protection</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/63">#63</a> — Make the audit trail tamper-evident and coverage-complete</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/64">#64</a> — Enforce data-locality constraints</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/65">#65</a> — Add Prometheus metrics, OpenTelemetry traces, and structured logs</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/66">#66</a> — Define SLOs, error budgets, and operational alerts</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/67">#67</a> — Build catalog-to-storage consistency checks</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/68">#68</a> — Add idempotent auto-repair workflows</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/69">#69</a> — Clean confirmed orphaned content with grace periods</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/70">#70</a> — Deliver the production administration UI</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/71">#71</a> — Deploy CogniStore with Helm and Kubernetes autoscaling</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/72">#72</a> — Publish Terraform and production reference configurations</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/73">#73</a> — Publish operator runbooks, migration guides, and reference architectures</li>
</ul>
<p>Dependencies listed inside each child issue are authoritative; checklist order is the suggested implementation sequence.</p>
<h5 id="issue-14-source">Source</h5>
<p><code>docs/roadmap.md</code>: multi-backend storage, security/compliance/tenancy, observability/ops, admin UI, resilience, deployment, and documentation.</p>
<h5 id="issue-14-hardening-follow-ups">Hardening follow-ups</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/91">#91</a> — Make POSIX path containment race-safe against symlink swaps</li>
</ul>
<h5 id="issue-14-m4-completion-2026-09-19">M4 completion — 2026-09-19</h5>
<p>All twenty delivery issues (<a href="https://github.com/melliott18/CogniStore/issues/54">#54</a>–<a href="https://github.com/melliott18/CogniStore/issues/73">#73</a>) and hardening <a href="https://github.com/melliott18/CogniStore/issues/91">#91</a> are implemented, merged to <code>main</code>, and acceptance-verified. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Immutable closeout evidence</a> maps every issue to its delivery and tests and retains the qualification reports for application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>.</p>
<p>Fresh validation: 5,192 default tests passed with 86.90% coverage; 391 service-enabled integration tests passed, 31 skipped and one documented GCS emulator xfail; quality/security/packaging gates, six mocked Terraform plans, 23 Helm tests, Prometheus rules and a 37-check installed operator drill passed. Historical Kubernetes evidence verifies installation, upgrade, rollback, persistence and 1→3→1 autoscaling, with its older runtime and development-profile limits explicit.</p>
<p>Hosted Actions could not start because of account billing/spending limits. Local qualification and the owner-approved <a href="https://github.com/melliott18/CogniStore/issues/72">#72</a> exception are explicit; this closure does not claim hosted CI, a fresh final-revision cluster campaign, or environment-specific production-cloud certification. All previously stale child acceptance boxes are reconciled.</p>

### Original delivery tickets

#### [#54 — \[M4\] Implement an Azure Blob storage driver](https://github.com/melliott18/CogniStore/issues/54)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:storage`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-54-goal">Goal</h5>
<p>Add Azure Blob as a conforming, stream-capable storage backend.</p>
<h5 id="issue-54-scope">Scope</h5>
<ul>
<li>Implement the shared driver contract and capability declarations.</li>
<li>Support configuration, ranged reads, block uploads, pagination, metadata, and normalized errors.</li>
<li>Add emulator-based tests and an opt-in live-cloud validation path.</li>
</ul>
<h5 id="issue-54-out-of-scope">Out of scope</h5>
<ul>
<li>Azure Files and Data Lake-specific APIs.</li>
<li>Cloud infrastructure provisioning.</li>
</ul>
<h5 id="issue-54-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The shared driver conformance suite passes.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Large uploads use bounded memory and safe block commit semantics.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Credentials are loaded through approved configuration and never logged.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Provider throttling, missing objects, and partial upload cleanup are tested.</li>
</ul>
<h5 id="issue-54-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/17">#17</a> — [M1] Implement an S3-compatible storage driver and conformance suite</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/21">#21</a> — [M1] Stream object moves with bounded memory and S3 multipart upload</li>
</ul>
<h5 id="issue-54-roadmap-coverage">Roadmap coverage</h5>
<p>Multi-backend storage → Azure Blob driver.</p>
<h5 id="issue-54-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/134">#134</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#55 — \[M4\] Implement a Google Cloud Storage driver](https://github.com/melliott18/CogniStore/issues/55)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:storage`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-55-goal">Goal</h5>
<p>Add GCS as a conforming, resumable storage backend.</p>
<h5 id="issue-55-scope">Scope</h5>
<ul>
<li>Implement the shared driver contract and capability declarations.</li>
<li>Support configuration, ranged reads, resumable uploads, pagination, metadata, and normalized errors.</li>
<li>Add emulator-based tests and an opt-in live-cloud validation path.</li>
</ul>
<h5 id="issue-55-out-of-scope">Out of scope</h5>
<ul>
<li>Filestore and BigQuery integrations.</li>
<li>Cloud infrastructure provisioning.</li>
</ul>
<h5 id="issue-55-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The shared driver conformance suite passes.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Interrupted large uploads can resume or cleanly abort.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Credentials are loaded through approved configuration and never logged.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Provider throttling, missing objects, and partial upload behavior are tested.</li>
</ul>
<h5 id="issue-55-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/17">#17</a> — [M1] Implement an S3-compatible storage driver and conformance suite</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/21">#21</a> — [M1] Stream object moves with bounded memory and S3 multipart upload</li>
</ul>
<h5 id="issue-55-roadmap-coverage">Roadmap coverage</h5>
<p>Multi-backend storage → GCS driver.</p>
<h5 id="issue-55-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/137">#137</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#56 — \[M4\] Add JWT and OIDC authentication](https://github.com/melliott18/CogniStore/issues/56)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-56-goal">Goal</h5>
<p>Authenticate human and service clients through standards-based identity providers.</p>
<h5 id="issue-56-scope">Scope</h5>
<ul>
<li>Validate issuer, audience, signature, expiry, and required claims.</li>
<li>Support OIDC discovery/JWKS rotation and service-to-service identities.</li>
<li>Propagate a normalized principal through API and job submission boundaries.</li>
</ul>
<h5 id="issue-56-out-of-scope">Out of scope</h5>
<ul>
<li>Role and permission evaluation.</li>
<li>Tenant data partitioning.</li>
</ul>
<h5 id="issue-56-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Invalid, expired, wrong-audience, and unknown-key tokens fail closed.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> JWKS refresh and rotation are tested without restart.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Authentication failures expose safe, consistent API errors.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Principal identity is available for audit events without persisting raw tokens.</li>
</ul>
<h5 id="issue-56-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-56-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → JWT/OIDC authentication.</p>
<h5 id="issue-56-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/135">#135</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#57 — \[M4\] Define and enforce role-based authorization](https://github.com/melliott18/CogniStore/issues/57)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-57-goal">Goal</h5>
<p>Ensure every protected API and operation checks an explicit permission model.</p>
<h5 id="issue-57-scope">Scope</h5>
<ul>
<li>Define roles and permissions for read, write, policy, movement, administration, and audit operations.</li>
<li>Centralize authorization checks at service boundaries and worker job validation.</li>
<li>Audit allow/deny outcomes at an appropriate sampling and sensitivity level.</li>
</ul>
<h5 id="issue-57-out-of-scope">Out of scope</h5>
<ul>
<li>Tenant row/data isolation.</li>
<li>External policy engines beyond the initial RBAC model.</li>
</ul>
<h5 id="issue-57-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> An endpoint/operation permission matrix is documented and tested.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> No protected endpoint relies only on UI hiding.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Workers revalidate authorization-sensitive job context where required.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Authorization failures are fail-closed and do not leak resource existence.</li>
</ul>
<h5 id="issue-57-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/56">#56</a> — [M4] Add JWT and OIDC authentication</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-57-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → RBAC.</p>
<h5 id="issue-57-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/139">#139</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#58 — \[M4\] Enforce per-tenant ownership and isolation](https://github.com/melliott18/CogniStore/issues/58)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-58-goal">Goal</h5>
<p>Prevent data, jobs, search results, and telemetry from crossing tenant boundaries.</p>
<h5 id="issue-58-scope">Scope</h5>
<ul>
<li>Add tenant ownership to persisted resources and job envelopes.</li>
<li>Enforce tenant scoping in DAL, API, index/search, workers, caches, and storage key namespaces.</li>
<li>Provide adversarial cross-tenant integration tests.</li>
</ul>
<h5 id="issue-58-out-of-scope">Out of scope</h5>
<ul>
<li>Billing and tenant self-service.</li>
<li>Cross-tenant sharing.</li>
</ul>
<h5 id="issue-58-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Cross-tenant reads, writes, search, policy actions, and job status access fail closed.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unique constraints and cache keys include tenant scope where required.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Background jobs cannot lose or change tenant context.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Isolation tests cover direct IDs, enumeration, filters, and crafted job payloads.</li>
</ul>
<h5 id="issue-58-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/57">#57</a> — [M4] Define and enforce role-based authorization</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/38">#38</a> — [M2] Implement hybrid metadata, vector, and keyword Ask retrieval</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
</ul>
<h5 id="issue-58-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → per-tenant isolation.</p>
<h5 id="issue-58-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/143">#143</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#59 — \[M4\] Integrate Vault/KMS-backed secrets and key providers](https://github.com/melliott18/CogniStore/issues/59)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-59-goal">Goal</h5>
<p>Remove production credentials and keys from static configuration while supporting controlled rotation.</p>
<h5 id="issue-59-scope">Scope</h5>
<ul>
<li>Define provider interfaces for Vault and cloud KMS/secret managers.</li>
<li>Resolve backend credentials and encryption keys at runtime with caching and expiry.</li>
<li>Implement redaction, least-privilege guidance, rotation, and failure handling.</li>
</ul>
<h5 id="issue-59-out-of-scope">Out of scope</h5>
<ul>
<li>A custom secrets server.</li>
<li>Data encryption policy and TLS enforcement.</li>
</ul>
<h5 id="issue-59-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Production examples contain no plaintext secrets.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Rotation can occur without rebuilding images or losing in-flight job state.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Secret values never appear in logs, traces, errors, or audit payloads.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Provider outage and expired-cache behavior fail according to documented policy.</li>
</ul>
<h5 id="issue-59-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/54">#54</a> — [M4] Implement an Azure Blob storage driver</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/55">#55</a> — [M4] Implement a Google Cloud Storage driver</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/56">#56</a> — [M4] Add JWT and OIDC authentication</li>
</ul>
<h5 id="issue-59-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → KMS/Vault integration.</p>
<h5 id="issue-59-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/141">#141</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#60 — \[M4\] Enforce encryption at rest and in transit](https://github.com/melliott18/CogniStore/issues/60)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-60-goal">Goal</h5>
<p>Define and verify encryption requirements for services, catalogs, and storage backends.</p>
<h5 id="issue-60-scope">Scope</h5>
<ul>
<li>Require verified TLS for external and internal service connections in production profiles.</li>
<li>Configure supported backends and database for provider-managed or application-approved encryption at rest.</li>
<li>Document key ownership, rotation, and exception handling.</li>
</ul>
<h5 id="issue-60-out-of-scope">Out of scope</h5>
<ul>
<li>Inventing custom cryptographic primitives.</li>
<li>End-user file encryption formats.</li>
</ul>
<h5 id="issue-60-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Production startup rejects insecure connections unless an explicit development-only mode is selected.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests validate certificate verification and common misconfiguration failures.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> At-rest encryption status/configuration is observable without exposing keys.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Rotation and backup/restore procedures are documented and exercised.</li>
</ul>
<h5 id="issue-60-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/59">#59</a> — [M4] Integrate Vault/KMS-backed secrets and key providers</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
</ul>
<h5 id="issue-60-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → encryption at rest and in transit.</p>
<h5 id="issue-60-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/142">#142</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#61 — \[M4\] Add pluggable PII detection and policy hooks](https://github.com/melliott18/CogniStore/issues/61)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-61-goal">Goal</h5>
<p>Identify potentially sensitive content and make classification available to placement and governance controls.</p>
<h5 id="issue-61-scope">Scope</h5>
<ul>
<li>Define a replaceable detector interface and normalized finding schema.</li>
<li>Run detection on supported extracted content with configurable limits.</li>
<li>Persist redacted classifications and expose policy hooks without storing sensitive snippets by default.</li>
</ul>
<h5 id="issue-61-out-of-scope">Out of scope</h5>
<ul>
<li>Automated regulatory certification.</li>
<li>A universal classifier for every file type.</li>
</ul>
<h5 id="issue-61-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Detectors can be enabled per tenant/policy and replaced without schema changes.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Findings include type, confidence, provenance, and detector version.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Sensitive raw matches are not logged or exposed by default.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Detection failure produces explicit unknown state and safe policy behavior.</li>
</ul>
<h5 id="issue-61-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/33">#33</a> — [M2] Extract normalized text and metadata from PDF and DOCX</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — [M4] Enforce per-tenant ownership and isolation</li>
</ul>
<h5 id="issue-61-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → PII detection hooks.</p>
<h5 id="issue-61-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/145">#145</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#62 — \[M4\] Implement legal holds and deletion protection](https://github.com/melliott18/CogniStore/issues/62)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-62-goal">Goal</h5>
<p>Prevent deletion or destructive movement of held data across all execution paths.</p>
<h5 id="issue-62-scope">Scope</h5>
<ul>
<li>Model scoped, auditable legal holds with lifecycle and actor metadata.</li>
<li>Enforce holds in APIs, policies, workers, repair, and cleanup eligibility.</li>
<li>Provide read-only inspection and authorized release workflows.</li>
</ul>
<h5 id="issue-62-out-of-scope">Out of scope</h5>
<ul>
<li>Legal case-management software.</li>
<li>Retention scheduling beyond hold semantics.</li>
</ul>
<h5 id="issue-62-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Held objects cannot be deleted, overwritten, garbage-collected, or moved in violation of the hold.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Policy and worker paths fail closed and emit an audit event.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Concurrent hold placement and deletion races preserve the hold.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Release requires explicit authorization and retains historical audit records.</li>
</ul>
<h5 id="issue-62-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — [M4] Enforce per-tenant ownership and isolation</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/23">#23</a> — [M1] Make move jobs idempotent with two-phase catalog updates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-62-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → legal holds.</p>
<h5 id="issue-62-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/146">#146</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#63 — \[M4\] Make the audit trail tamper-evident and coverage-complete](https://github.com/melliott18/CogniStore/issues/63)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-63-goal">Goal</h5>
<p>Provide verifiable, complete evidence for security- and lifecycle-relevant actions.</p>
<h5 id="issue-63-scope">Scope</h5>
<ul>
<li>Define an event-coverage matrix for API, policy, worker, storage, governance, and admin actions.</li>
<li>Add append-only/tamper-evident storage controls and integrity verification.</li>
<li>Document retention, access, export, and verification procedures.</li>
</ul>
<h5 id="issue-63-out-of-scope">Out of scope</h5>
<ul>
<li>A full SIEM product.</li>
<li>Blockchain-based storage.</li>
</ul>
<h5 id="issue-63-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Automated tests prove required actions emit the expected correlated events.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unauthorized update/delete of audit records is denied.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Integrity verification detects missing or altered records.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Audit access and export are tenant-scoped and themselves audited.</li>
</ul>
<h5 id="issue-63-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — [M4] Enforce per-tenant ownership and isolation</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/59">#59</a> — [M4] Integrate Vault/KMS-backed secrets and key providers</li>
</ul>
<h5 id="issue-63-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → immutable audit logs; M4 audit-complete criterion.</p>
<h5 id="issue-63-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/147">#147</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#64 — \[M4\] Enforce data-locality constraints](https://github.com/melliott18/CogniStore/issues/64)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-64-goal">Goal</h5>
<p>Ensure placement candidates and moves respect configured geographic and residency requirements.</p>
<h5 id="issue-64-scope">Scope</h5>
<ul>
<li>Define tenant/object locality rules over the tier/pool region model.</li>
<li>Filter placement candidates before optimization and revalidate at execution.</li>
<li>Audit rejected plans and explicit, authorized exceptions.</li>
</ul>
<h5 id="issue-64-out-of-scope">Out of scope</h5>
<ul>
<li>Legal interpretation of residency regulations.</li>
<li>Cloud account provisioning.</li>
</ul>
<h5 id="issue-64-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A move cannot cross a prohibited locality boundary even if another policy favors it.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unknown or stale region data fails according to documented conservative behavior.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Dry-run explains the binding locality constraint.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Tests cover conflicting cost, performance, carbon, and locality objectives.</li>
</ul>
<h5 id="issue-64-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/51">#51</a> — [M3] Model tier pools, regions, latency, cost, and carbon attributes</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — [M4] Enforce per-tenant ownership and isolation</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/57">#57</a> — [M4] Define and enforce role-based authorization</li>
</ul>
<h5 id="issue-64-roadmap-coverage">Roadmap coverage</h5>
<p>Security, compliance, tenancy → data locality constraints.</p>
<h5 id="issue-64-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/144">#144</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#65 — \[M4\] Add Prometheus metrics, OpenTelemetry traces, and structured logs](https://github.com/melliott18/CogniStore/issues/65)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:observability`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-65-goal">Goal</h5>
<p>Make API, worker, policy, index, and storage behavior observable end to end.</p>
<h5 id="issue-65-scope">Scope</h5>
<ul>
<li>Define bounded-cardinality metrics for requests, jobs, drivers, policies, and indexes.</li>
<li>Propagate trace and correlation context across API and queue boundaries.</li>
<li>Emit structured, redacted logs and provide baseline Grafana dashboards.</li>
</ul>
<h5 id="issue-65-out-of-scope">Out of scope</h5>
<ul>
<li>Formal SLOs and paging policies.</li>
<li>Vendor-specific hosted-observability configuration.</li>
</ul>
<h5 id="issue-65-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A request can be traced through queued work to storage and catalog operations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Metrics document units, labels, and cardinality constraints.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Secrets and content are redacted from logs and spans.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Local dashboards visualize movement throughput, failures, queue depth, and latency.</li>
</ul>
<h5 id="issue-65-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> — [M2] Expose a versioned REST API and OpenAPI contract</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/18">#18</a> — [M1] Add a message bus and background worker runtime</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-65-roadmap-coverage">Roadmap coverage</h5>
<p>Observability and ops → Prometheus/Grafana, OpenTelemetry, and structured logs.</p>
<h5 id="issue-65-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/136">#136</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#66 — \[M4\] Define SLOs, error budgets, and operational alerts](https://github.com/melliott18/CogniStore/issues/66)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:observability`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-66-goal">Goal</h5>
<p>Turn telemetry into measurable service expectations and actionable alerts.</p>
<h5 id="issue-66-scope">Scope</h5>
<ul>
<li>Define move latency/error, API availability, indexing lag, and throughput SLOs.</li>
<li>Implement burn-rate, capacity, cost, and carbon alerts with runbook links.</li>
<li>Expose dashboards for SLO attainment and budget consumption.</li>
</ul>
<h5 id="issue-66-out-of-scope">Out of scope</h5>
<ul>
<li>On-call staffing and escalation policy.</li>
<li>Invoice-grade cost reporting.</li>
</ul>
<h5 id="issue-66-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Every SLO has an owner, formula, data source, target, and review window.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Alert tests exercise breach and recovery behavior.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Cost/carbon alerts use the same versioned estimators as policy budgets.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Full-scale M1 qualification results can be evaluated against the SLO model.</li>
</ul>
<h5 id="issue-66-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/65">#65</a> — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/53">#53</a> — [M3] Enforce cost/carbon budgets and provide what-if simulation</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/29">#29</a> — [M1] Qualify one-million-object moves and failure recovery</li>
</ul>
<h5 id="issue-66-roadmap-coverage">Roadmap coverage</h5>
<p>Observability and ops → SLOs, throughput targets, cost/carbon guardrails, dashboards, and alerts.</p>
<h5 id="issue-66-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/140">#140</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#67 — \[M4\] Build catalog-to-storage consistency checks](https://github.com/melliott18/CogniStore/issues/67)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:observability`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-67-goal">Goal</h5>
<p>Detect missing, duplicate, partial, mismatched, and untracked objects without modifying data.</p>
<h5 id="issue-67-scope">Scope</h5>
<ul>
<li>Compare catalog placements, backend listings/stats, checksums, and job state.</li>
<li>Classify discrepancies with stable reason codes and severity.</li>
<li>Support scoped, resumable, read-only scans and reports.</li>
</ul>
<h5 id="issue-67-out-of-scope">Out of scope</h5>
<ul>
<li>Automatic repair and deletion.</li>
<li>Cross-tenant aggregate reports.</li>
</ul>
<h5 id="issue-67-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Dry-run/read-only behavior is guaranteed.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Fixtures cover missing source/destination, checksum mismatch, duplicate placement, partial job, and untracked object.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Large scans are resumable and rate-limited.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Results are tenant-scoped, audited, and exportable.</li>
</ul>
<h5 id="issue-67-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/22">#22</a> — [M1] Verify object integrity before deleting the source</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/31">#31</a> — [M2] Persist move, policy, failure, and retry audit events</li>
</ul>
<h5 id="issue-67-roadmap-coverage">Roadmap coverage</h5>
<p>Observability and ops → consistency checks.</p>
<h5 id="issue-67-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/133">#133</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#68 — \[M4\] Add idempotent auto-repair workflows](https://github.com/melliott18/CogniStore/issues/68)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:observability`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-68-goal">Goal</h5>
<p>Repair unambiguous consistency failures safely while quarantining uncertain cases.</p>
<h5 id="issue-68-scope">Scope</h5>
<ul>
<li>Map eligible discrepancy classes to idempotent repair jobs.</li>
<li>Require verification, audit, and policy/hold/locality checks before commit.</li>
<li>Quarantine ambiguous cases for operator review.</li>
</ul>
<h5 id="issue-68-out-of-scope">Out of scope</h5>
<ul>
<li>Deleting confirmed orphaned content.</li>
<li>Undelete after retention expiry.</li>
</ul>
<h5 id="issue-68-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Repair defaults to plan-only mode and requires explicit enablement.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Repeated repair attempts converge without duplicate placement or loss.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Held, locality-constrained, or uncertain objects are never auto-modified.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Failure-injection tests cover interruption at each repair phase.</li>
</ul>
<h5 id="issue-68-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/67">#67</a> — [M4] Build catalog-to-storage consistency checks</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/23">#23</a> — [M1] Make move jobs idempotent with two-phase catalog updates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/62">#62</a> — [M4] Implement legal holds and deletion protection</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/64">#64</a> — [M4] Enforce data-locality constraints</li>
</ul>
<h5 id="issue-68-roadmap-coverage">Roadmap coverage</h5>
<p>Observability and ops → auto-repair.</p>
<h5 id="issue-68-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/149">#149</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#69 — \[M4\] Clean confirmed orphaned content with grace periods](https://github.com/melliott18/CogniStore/issues/69)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:observability`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-69-goal">Goal</h5>
<p>Reclaim backend content only after proving it is unreferenced and unprotected.</p>
<h5 id="issue-69-scope">Scope</h5>
<ul>
<li>Define orphan eligibility across catalog, CAS references, in-flight jobs, holds, and retention windows.</li>
<li>Provide report, quarantine, grace-period, and explicit execution stages.</li>
<li>Record irreversible actions in the audit trail.</li>
</ul>
<h5 id="issue-69-out-of-scope">Out of scope</h5>
<ul>
<li>General retention policy management.</li>
<li>Immediate deletion on first detection.</li>
</ul>
<h5 id="issue-69-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Dry-run is the default and reports every blocking or qualifying condition.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> No object with a reference, active job, legal hold, or unresolved tenant can be deleted.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Concurrent reference creation invalidates pending cleanup safely.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Deletion failures are retryable and never hide partially completed cleanup.</li>
</ul>
<h5 id="issue-69-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/67">#67</a> — [M4] Build catalog-to-storage consistency checks</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/35">#35</a> — [M2] Implement safe deduplication reference and deletion semantics</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/62">#62</a> — [M4] Implement legal holds and deletion protection</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/63">#63</a> — [M4] Make the audit trail tamper-evident and coverage-complete</li>
</ul>
<h5 id="issue-69-roadmap-coverage">Roadmap coverage</h5>
<p>Observability and ops → orphan cleanup.</p>
<h5 id="issue-69-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/151">#151</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#70 — \[M4\] Deliver the production administration UI](https://github.com/melliott18/CogniStore/issues/70)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:ui`, `roadmap`, `type:feature`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-70-goal">Goal</h5>
<p>Provide authorized operational views and actions for drivers, tiers, policies, jobs, audits, and repairs.</p>
<h5 id="issue-70-scope">Scope</h5>
<ul>
<li>Add driver/tier health and configuration views, policy/action management, job history, and repair workflows.</li>
<li>Render audit trails, explanations, and dry-run diffs with tenant scoping.</li>
<li>Apply RBAC to every view and mutation.</li>
</ul>
<h5 id="issue-70-out-of-scope">Out of scope</h5>
<ul>
<li>A visual policy programming language.</li>
<li>Cloud billing administration.</li>
</ul>
<h5 id="issue-70-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Operators can inspect health, preview and submit allowed actions, follow jobs, and review audit evidence.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unauthorized controls are absent and server-side checks still enforce every action.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Loading, partial failure, empty, and stale-data states are handled.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Critical destructive actions require an explicit confirmation and show scope.</li>
</ul>
<h5 id="issue-70-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/50">#50</a> — [M3] Expose placement explanations and before/after diffs</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/57">#57</a> — [M4] Define and enforce role-based authorization</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — [M4] Enforce per-tenant ownership and isolation</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/65">#65</a> — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/68">#68</a> — [M4] Add idempotent auto-repair workflows</li>
</ul>
<h5 id="issue-70-roadmap-coverage">Roadmap coverage</h5>
<p>API, CLI, and UI → admin UI for drivers, tiers, policies, actions, audit trail, previews, and diffs.</p>
<h5 id="issue-70-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/152">#152</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#71 — \[M4\] Deploy CogniStore with Helm and Kubernetes autoscaling](https://github.com/melliott18/CogniStore/issues/71)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:delivery`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-71-goal">Goal</h5>
<p>Provide a secure, upgradeable Kubernetes deployment for API, workers, scheduler, and UI.</p>
<h5 id="issue-71-scope">Scope</h5>
<ul>
<li>Create a Helm chart with production profiles, probes, resources, network/security settings, and secret references.</li>
<li>Support migrations, upgrades, rollback, and persistent dependencies.</li>
<li>Configure HPA/KEDA behavior for API and workers and validate it under load.</li>
</ul>
<h5 id="issue-71-out-of-scope">Out of scope</h5>
<ul>
<li>Every cloud topology and managed-service permutation.</li>
<li>Terraform provisioning.</li>
</ul>
<h5 id="issue-71-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A clean cluster install passes an automated smoke test.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Upgrade and rollback preserve catalog/job invariants.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Workloads run as non-root with least-privilege defaults.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Autoscaling responds to documented request/queue signals without duplicate work.</li>
</ul>
<h5 id="issue-71-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/28">#28</a> — [M1] Provide a Docker-based development and integration environment</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/56">#56</a> — [M4] Add JWT and OIDC authentication</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> — [M4] Enforce per-tenant ownership and isolation</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/65">#65</a> — [M4] Add Prometheus metrics, OpenTelemetry traces, and structured logs</li>
</ul>
<h5 id="issue-71-roadmap-coverage">Roadmap coverage</h5>
<p>Delivery and DX plus M4 success → Helm, production configs, Kubernetes, and autoscaling.</p>
<h5 id="issue-71-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/148">#148</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

#### [#72 — \[M4\] Publish Terraform and production reference configurations](https://github.com/melliott18/CogniStore/issues/72)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:delivery`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-72-goal">Goal</h5>
<p>Provide reproducible examples for the infrastructure surrounding a production deployment.</p>
<h5 id="issue-72-scope">Scope</h5>
<ul>
<li>Publish at least one supported reference topology with network, database, object storage, secrets, and Kubernetes dependencies.</li>
<li>Parameterize environments without embedding credentials.</li>
<li>Document cost, security, scaling, backup, and destroy considerations.</li>
</ul>
<h5 id="issue-72-out-of-scope">Out of scope</h5>
<ul>
<li>Turnkey support for every cloud.</li>
<li>Automatic production deployment from the repository.</li>
</ul>
<h5 id="issue-72-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Example plans pass the resource-free CI verification script locally under the owner-approved hosted-CI exception recorded in PR <a href="https://github.com/melliott18/CogniStore/pull/150">#150</a>; no hosted CI pass is claimed.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Inputs, outputs, provider versions, and state assumptions are documented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> The topology is compatible with the Helm production profile.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Destructive operations and data-retention implications are explicit.</li>
</ul>
<h5 id="issue-72-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/71">#71</a> — [M4] Deploy CogniStore with Helm and Kubernetes autoscaling</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/54">#54</a> — [M4] Implement an Azure Blob storage driver</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/55">#55</a> — [M4] Implement a Google Cloud Storage driver</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/59">#59</a> — [M4] Integrate Vault/KMS-backed secrets and key providers</li>
</ul>
<h5 id="issue-72-roadmap-coverage">Roadmap coverage</h5>
<p>Delivery and DX → Terraform samples and reference architectures.</p>
<h5 id="issue-72-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/150">#150</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>
<p>The original hosted-CI acceptance wording is qualified by the owner authorization explicitly recorded in merged PR <a href="https://github.com/melliott18/CogniStore/pull/150">#150</a>. Fresh execution of <code>scripts/terraform/verify.sh</code> passed six mocked plan cases and production Helm handoff validation without creating cloud resources.</p>

#### [#73 — \[M4\] Publish operator runbooks, migration guides, and reference architectures](https://github.com/melliott18/CogniStore/issues/73)

- **Kind:** Original delivery ticket
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:docs`, `documentation`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<h5 id="issue-73-goal">Goal</h5>
<p>Give operators tested procedures for installing, upgrading, recovering, and troubleshooting CogniStore.</p>
<h5 id="issue-73-scope">Scope</h5>
<ul>
<li>Write install/upgrade/rollback, backup/restore, incident, queue/DLQ, repair, and security runbooks.</li>
<li>Document SQLite-to-Postgres and backend/deployment migrations.</li>
<li>Publish supported reference architectures and troubleshooting decision trees.</li>
</ul>
<h5 id="issue-73-out-of-scope">Out of scope</h5>
<ul>
<li>Formal training curriculum.</li>
<li>Undocumented experimental topologies.</li>
</ul>
<h5 id="issue-73-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A fresh-user walkthrough succeeds from a clean environment.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Backup/restore and at least one incident/repair drill are exercised and recorded.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Commands, diagrams, and configuration match the shipped release.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Every SLO alert links to an actionable runbook.</li>
</ul>
<h5 id="issue-73-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> — [M2] Migrate the catalog to Postgres and pgvector through a DAL</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/24">#24</a> — [M1] Add retry, backoff, dead-letter, and redrive handling</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/66">#66</a> — [M4] Define SLOs, error budgets, and operational alerts</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/68">#68</a> — [M4] Add idempotent auto-repair workflows</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/71">#71</a> — [M4] Deploy CogniStore with Helm and Kubernetes autoscaling</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/72">#72</a> — [M4] Publish Terraform and production reference configurations</li>
</ul>
<h5 id="issue-73-roadmap-coverage">Roadmap coverage</h5>
<p>Delivery and DX → operator runbooks, migration guides, and reference architectures.</p>
<h5 id="issue-73-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/153">#153</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

### Verification follow-ups

#### [#91 — Make POSIX path containment race-safe against symlink swaps](https://github.com/melliott18/CogniStore/issues/91)

- **Kind:** Verification follow-up
- **Status:** Closed
- **Milestone:** M4 – Production platform
- **Labels:** `area:security`, `area:storage`, `bug`, `roadmap`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/14">#14</a>
Roadmap tracker: <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a></p>
<p>Found by the M1 implementation review and reconfirmed on 2026-08-27.</p>
<h5 id="issue-91-problem">Problem</h5>
<p>The POSIX driver rejects symlinks and validates that paths remain below the configured tier root, but it later performs ordinary path-based open, stat, replace, and unlink operations. A concurrent directory-to-symlink swap can invalidate that check before use.</p>
<p>Current tests cover static symlink paths; they do not prove containment under a concurrent swap. The repository also does not define and enforce a trusted, non-mutating tier-root threat boundary.</p>
<h5 id="issue-91-scope">Scope</h5>
<ul>
<li>Define the supported POSIX tier-root trust and mutation model.</li>
<li>Use descriptor-relative, no-follow operations or an equivalent race-safe design where the platform supports them.</li>
<li>Fail closed when the platform cannot provide required containment guarantees.</li>
<li>Add deterministic concurrent-swap regressions for read, publish, stat, list, and delete paths.</li>
<li>Document residual platform limitations and deployment permissions.</li>
</ul>
<h5 id="issue-91-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> A concurrent symlink or directory swap cannot make CogniStore read, publish, or delete outside the configured tier root.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Containment guarantees and platform limitations are documented.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> POSIX conformance and mover cleanup tests cover adversarial swaps.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" checked="checked" disabled="disabled" type="checkbox"> Unsupported platforms or filesystems fail closed rather than silently weakening containment.</li>
</ul>
<h5 id="issue-91-related-evidence">Related evidence</h5>
<ul>
<li><code>cognistore/drivers/posix_driver.py</code></li>
<li><code>tests/unit/test_posix_driver.py</code></li>
<li><code>docs/m1_review_2026-08-20.md</code></li>
</ul>
<h5 id="issue-91-acceptance-verification-2026-09-19">Acceptance verification — 2026-09-19</h5>
<p>Reconciled the four stale acceptance checkboxes after reviewing merged PR <a href="https://github.com/melliott18/CogniStore/pull/138">#138</a>, current implementation and regression coverage. <a href="https://github.com/melliott18/CogniStore/blob/9fe50e3319b08dbec3325f4d23272cea6f4d1f5a/docs/evidence/m4/README.md">Retained M4 closeout evidence</a> records application revision <code>ad20be8fa1d1f00224064d65324527d2a200dc52</code>, 5,192 passing default tests (86.90% coverage), 391 passing service-enabled integration tests, qualified skips/expected failure, and the applicable acceptance mapping.</p>
<p>GitHub Actions remains blocked before job startup by account billing/spending limits; local validation is recorded without claiming a hosted CI pass. Environment-specific production qualification and documented operating limits remain applicable.</p>

## M5 – Release readiness and controlled pilot

- **GitHub milestone:** [M5 – Release readiness and controlled pilot](https://github.com/melliott18/CogniStore/milestone/5)
- **Original delivery tickets:** 11
- **Verification follow-ups:** 0
- **Other tracking issues:** 0
- **Status:** 12 open, 0 closed (12 including the epic)

### Epic

#### [#155 — \[Epic\] M5 – Release readiness and controlled pilot](https://github.com/melliott18/CogniStore/issues/155)

- **Kind:** Milestone epic
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:delivery`, `roadmap`, `type:epic`
- **Last updated:** 2026-09-20

<h5 id="issue-155-outcome">Outcome</h5>
<p>Qualify one explicit CogniStore release candidate and production deployment, resolve demonstrated safety defects, and complete a controlled pilot with evidence-backed expansion criteria.</p>
<p>M1–M4 delivery and roadmap <a href="https://github.com/melliott18/CogniStore/issues/12">#12</a> remain completed historical scope. This new milestone follows the 2026-09-20 readiness assessment of <code>2cce6ff4d43fd287ad008197f1e17f168c3580c4</code>.</p>
<h5 id="issue-155-why-this-phase-exists">Why this phase exists</h5>
<p>The assessment verified 289 fresh targeted tests, successful local CLI/API/SDK workflows, and the retained M4 evidence. It also reproduced two release blockers: a case-insensitive POSIX tenant namespace/hold bypass, and a concurrent DELETE/PUT catalog race. Hosted CI is blocked before job startup; historical development-kind and mocked Terraform evidence do not establish final-candidate production qualification.</p>
<h5 id="issue-155-execution-order">Execution order</h5>
<ol>
<li>Fix both confirmed defects; restore hosted CI and select the target deployment/workload in parallel.</li>
<li>Assemble an immutable candidate from the fixes and passing gates. Start source/configuration audit as soon as scope is selected.</li>
<li>Deploy isolated production-configured staging; finish live security/audit checks.</li>
<li>Run manual acceptance, recovery/upgrade/rotation, and realistic load/alert qualification in parallel where their environments can be isolated.</li>
<li>Admit a bounded pilot only after release blockers and qualification gates close; derive the next roadmap from pilot outcomes.</li>
</ol>
<p>Dependencies inside child issues are authoritative. Audit can start early; its final signoff must identify the tested candidate and environment. Keep destructive drills in dedicated test scopes so one campaign cannot remove another's dependencies.</p>
<h5 id="issue-155-child-issues">Child issues</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/156">#156</a> — [P1] Prevent case-insensitive POSIX tenant namespace aliases from bypassing isolation and legal holds</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/157">#157</a> — [P1] Prevent an older DELETE from removing a concurrent replacement PUT catalog record</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/158">#158</a> — Restore hosted CI and qualify the supported runtime matrix</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — Define the first production deployment, workload, and pilot acceptance gates</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/160">#160</a> — Assemble and freeze a reproducible release candidate for the selected workload</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/162">#162</a> — Audit system safety, security, and data consistency for the release candidate</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/161">#161</a> — Deploy isolated staging with the selected production security and infrastructure configuration</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/163">#163</a> — Run manual end-to-end acceptance across CLI, API, SDK, search, and administration</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/164">#164</a> — Qualify failure recovery, coherent restore, credential rotation, and upgrade rollback</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/165">#165</a> — Qualify realistic load, sustained operation, capacity limits, and alert delivery</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> <a href="https://github.com/melliott18/CogniStore/issues/166">#166</a> — Run a gated pilot and decide expansion from measured results</li>
</ul>
<h5 id="issue-155-milestone-success-criteria">Milestone success criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Both reproduced defects and any subsequent release-blocking findings are fixed with regression evidence.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A selected workload/topology has measurable targets, named owners, explicit feature limits and pilot stop conditions.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Exact source, dependency, image, provider and configuration identities are retained; hosted gates pass.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Audit, manual acceptance, recovery and load/alert campaigns pass against the qualified candidate/configuration.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Pilot entry approval is recorded, the agreed pilot runs, and the exit decision is supported by measured results.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Evidence, residual limitations, operator guidance and the next backlog are durable and reviewable.</li>
</ul>
<p>A set of administratively closed children is insufficient to establish readiness. A failed/stopped pilot does not automatically satisfy this epic's success criteria. Material changes after qualification require scoped requalification.</p>
<h5 id="issue-155-evidence-policy">Evidence policy</h5>
<p>Retain source/configuration/environment identities, commands, expected/actual outcomes, sanitized logs, hashes and explicitly explained skips. Store artifacts in the repository or durable linked CI/artifact storage; local ignored paths alone are insufficient. Do not include live credentials, tokens or customer content.</p>
<h5 id="issue-155-scope-boundaries">Scope boundaries</h5>
<p>This phase qualifies the selected deployment and workload. It does not reopen completed M1–M4 delivery or promise every cloud, multi-region availability, distributed SQLite scheduling, OCR or general-availability model quality.</p>
<h5 id="issue-155-source">Source</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/evidence/m4/README.md">M4 qualification evidence and explicit deployment limits</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/reference_architectures.md">Supported reference architectures</a></li>
</ul>
<h5 id="issue-155-start-now">Start now</h5>
<p><a href="https://github.com/melliott18/CogniStore/issues/156">#156</a> and <a href="https://github.com/melliott18/CogniStore/issues/157">#157</a> are P1 release blockers. <a href="https://github.com/melliott18/CogniStore/issues/158">#158</a> and <a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> can proceed independently in parallel. Source/configuration audit in <a href="https://github.com/melliott18/CogniStore/issues/162">#162</a> can begin once the target is selected, with final signoff after candidate staging exists.</p>
<h5 id="issue-155-dependency-summary">Dependency summary</h5>
<table>
<thead>
<tr>
<th>Work</th>
<th>Required before completion</th>
</tr>
</thead>
<tbody>
<tr>
<td>Candidate <a href="https://github.com/melliott18/CogniStore/issues/160">#160</a></td>
<td>Both fixes <a href="https://github.com/melliott18/CogniStore/issues/156">#156</a>, <a href="https://github.com/melliott18/CogniStore/issues/157">#157</a>; CI <a href="https://github.com/melliott18/CogniStore/issues/158">#158</a>; target <a href="https://github.com/melliott18/CogniStore/issues/159">#159</a></td>
</tr>
<tr>
<td>Staging <a href="https://github.com/melliott18/CogniStore/issues/161">#161</a></td>
<td>Target <a href="https://github.com/melliott18/CogniStore/issues/159">#159</a>; candidate <a href="https://github.com/melliott18/CogniStore/issues/160">#160</a></td>
</tr>
<tr>
<td>Audit signoff <a href="https://github.com/melliott18/CogniStore/issues/162">#162</a></td>
<td>Target, candidate and staging evidence; review can start earlier</td>
</tr>
<tr>
<td>UAT <a href="https://github.com/melliott18/CogniStore/issues/163">#163</a>, recovery <a href="https://github.com/melliott18/CogniStore/issues/164">#164</a>, load <a href="https://github.com/melliott18/CogniStore/issues/165">#165</a></td>
<td>Target and staging; campaigns may run in parallel with isolated test scopes</td>
</tr>
<tr>
<td>Pilot <a href="https://github.com/melliott18/CogniStore/issues/166">#166</a></td>
<td>Audit, UAT, recovery and load gates passed; recorded entry decision</td>
</tr>
</tbody>
</table>

### Original delivery tickets

#### [#156 — \[M5\] \[P1\] Prevent case-insensitive POSIX tenant namespace aliases from bypassing isolation and legal holds](https://github.com/melliott18/CogniStore/issues/156)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:security`, `area:storage`, `bug`, `roadmap`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<p><strong>Priority: P1. Release gate: fix before production pilot or admitting important production data.</strong></p>
<p>A JWT-authenticated default-tenant writer can overwrite and delete another tenant's object, including an object under legal hold, when shared POSIX storage resolves a differently capitalized reserved namespace to the same directory. Confirmed during the 2026-09-20 readiness assessment at <code>2cce6ff4d43fd287ad008197f1e17f168c3580c4</code>.</p>
<h5 id="issue-156-preconditions-and-trigger">Preconditions and trigger</h5>
<ul>
<li>POSIX driver backed by a case-insensitive filesystem (the reproduction used a case-insensitive macOS temporary volume).</li>
<li>Default and nondefault tenants share the same storage root and bucket.</li>
<li>Attacker is authorized to write/delete objects in the <strong>default</strong> tenant. No victim-tenant membership or administrative role is required for the attacker.</li>
<li>A nondefault tenant <code>victim</code> owns <code>bucket/private.txt</code>. Its physical key is <code>.cognistore-tenants/&lt;sha256(victim)&gt;/private.txt</code>.</li>
<li>The default-tenant writer submits <code>.COGNISTORE-TENANTS/&lt;sha256(victim)&gt;/private.txt</code> as its logical key. The tenant digest is derived from the tenant ID and is not an authorization control.</li>
</ul>
<h5 id="issue-156-expected-and-actual">Expected and actual</h5>
<p>Expected: reserved namespace aliases are rejected before backend access, and no default-tenant operation can observe or mutate another tenant's physical objects. A legal hold remains effective regardless of spelling aliases in a different tenant's request.</p>
<p>Actual, with authenticated in-process REST calls and synthetic identities:</p>
<pre><code class="language-text">victim PUT: 201
victim hold: 201
victim overwrite (correctly denied): 409
default cross-tenant PUT: 201
victim read: 200 b'replaced'
default cross-tenant DELETE: 204
victim after delete: 404
</code></pre>
<p>The protected object's bytes were overwritten and then deleted. The normal victim-tenant overwrite is correctly denied, demonstrating that the alias bypasses the tenant/hold boundary rather than disabling holds globally.</p>
<h5 id="issue-156-relevant-source">Relevant source</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/drivers/tenancy.py#L43-L86">Tenant physical prefix and case-sensitive reserved-name checks</a>.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/drivers/tenancy.py#L100-L150">Read/write/delete and streaming methods delegate through the tenant key mapping</a>.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/drivers/tenancy.py#L152-L217">Listing filters and pagination also use tenant key mapping</a>.</li>
</ul>
<h5 id="issue-156-reproduction">Reproduction</h5>
<p>In a checkout of the assessed revision with the repository's development dependencies installed, save the following as <code>/tmp/tenant_namespace_repro.py</code> and run <code>PYTHONPATH=. python /tmp/tenant_namespace_repro.py</code> from the repository root. Ensure Python's temporary directory is on a <strong>case-insensitive</strong> filesystem; set <code>TMPDIR</code> to an appropriate disposable volume if needed. It uses temporary directories and mock JWT keys, not a deployed service or real identity provider. The <code>development</code> profile permits this local harness; JWT authentication and RBAC are explicitly enabled in the app.</p>
<p>&lt;details&gt;
&lt;summary&gt;Self-contained reproduction using the repository's JWT fixture&lt;/summary&gt;</p>
<pre><code class="language-python">&quot;&quot;&quot;Synthetic local API demonstration; creates/deletes only a temporary directory.&quot;&quot;&quot;
import os
os.environ['COGNISTORE_SECURITY_PROFILE'] = 'development'
from tempfile import TemporaryDirectory
from hashlib import sha256
from fastapi.testclient import TestClient
from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.auth.tenancy import TenantResolver
from cognistore.core.catalog import Catalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import identity_provider, ISSUER
fixture = identity_provider.__wrapped__()
auth, token, _ = next(fixture)
try:
    with TemporaryDirectory(prefix='cognistore-audit-api-') as folder:
        gateway = CogniStoreGateway(Catalog(), {'hot': PosixDriver(folder)})
        app = create_app(gateway, authentication=auth, authorization=RBACAuthorizer(RBACPolicy({(ISSUER, 'alice'): ['admin'], (ISSUER, 'bob'): ['writer']})), tenancy=TenantResolver({(ISSUER, 'alice'): 'victim', (ISSUER, 'bob'): 'default'}))
        alice = {'Authorization': 'Bearer ' + token('alice')}
        bob = {'Authorization': 'Bearer ' + token('bob')}
        target = '/v1/objects/hot/bucket/private.txt'
        alias = '/v1/objects/hot/bucket/.COGNISTORE-TENANTS/' + sha256(b'victim').hexdigest() + '/private.txt'
        with TestClient(app) as client:
            print('victim PUT:', client.put(target, headers=alice, content=b'original').status_code)
            print('victim hold:', client.post('/v1/legal-holds', headers=alice, json={'bucket':'bucket','key':'private.txt','reason':'test preservation'}).status_code)
            print('victim overwrite (correctly denied):', client.put(target, headers=alice, content=b'denied').status_code)
            print('default cross-tenant PUT:', client.put(alias, headers=bob, content=b'replaced').status_code)
            read = client.get(target, headers=alice)
            print('victim read:', read.status_code, read.content)
            print('default cross-tenant DELETE:', client.delete(alias, headers=bob).status_code)
            print('victim after delete:', client.get(target, headers=alice).status_code)
finally:
    try:
        next(fixture)
    except StopIteration:
        pass
</code></pre>
<p>&lt;/details&gt;</p>
<h5 id="issue-156-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Define and enforce reserved tenant-namespace handling for the supported POSIX filesystem semantics. Case aliases cannot reach a nondefault tenant from the default tenant; reject unsupported storage semantics explicitly if safe isolation cannot be guaranteed.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Add regression coverage for the demonstrated authenticated PUT/DELETE sequence on a case-insensitive backend/volume, including legal holds; victim bytes remain unchanged and readable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Cover mixed-case reserved prefixes and all shared key-entry paths: byte/stream writes, reads, stat/generation, deletion, list/paginated list, and prefixes. Review the corresponding bucket-name boundary for equivalent aliases.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Preserve valid ordinary keys and existing tenant isolation on case-sensitive POSIX and cloud object backends; document any intentional reserved-name compatibility change.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Tests prove rejection happens before a cross-tenant backend mutation, and existing tenant, hold, POSIX containment, and storage conformance tests pass.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Record the fix revision and regression evidence in the release-readiness evidence set.</li>
</ul>
<h5 id="issue-156-scope-boundaries">Scope boundaries</h5>
<p>This ticket fixes the demonstrated reserved-namespace alias boundary and its shared entry points. It does not claim the exploit was reproduced on S3, Azure, GCS, or ordinary case-sensitive Linux storage. It does not require a tenant-storage migration or redesign unless the chosen fix demonstrates one is necessary. A broader tenant/security audit belongs to the release-readiness audit workstream.</p>
<h5 id="issue-156-dependencies">Dependencies</h5>
<p>None. This ticket can start immediately.</p>
<h5 id="issue-156-related-delivery">Related delivery</h5>
<p><a href="https://github.com/melliott18/CogniStore/issues/58">#58</a> (tenant isolation), <a href="https://github.com/melliott18/CogniStore/issues/62">#62</a> (legal holds), <a href="https://github.com/melliott18/CogniStore/issues/91">#91</a> (POSIX containment). These delivered capabilities remain completed; this ticket addresses a newly reproduced defect.</p>

#### [#157 — \[M5\] \[P1\] Prevent an older DELETE from removing a concurrent replacement PUT catalog record](https://github.com/melliott18/CogniStore/issues/157)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:api`, `area:control-plane`, `bug`, `roadmap`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<p><strong>Priority: P1. Release gate: fix before production pilot or admitting important production data.</strong></p>
<p>An older DELETE can remove the catalog entry published by a successful concurrent replacement PUT. Both API requests return success, but subsequent reads return 404 while the replacement bytes remain on the backend. Confirmed during the 2026-09-20 readiness assessment at <code>2cce6ff4d43fd287ad008197f1e17f168c3580c4</code> with authenticated REST, a real SQLite catalog, and POSIX storage.</p>
<h5 id="issue-157-preconditions-and-deterministic-interleaving">Preconditions and deterministic interleaving</h5>
<ul>
<li>Two authorized operations act on the same tenant/bucket/key.</li>
<li>Start with a cataloged object at <code>hot/bucket/item</code> containing <code>initial</code>.</li>
<li>DELETE reads the old generation and successfully deletes that generation from storage.</li>
<li>Pause DELETE after backend deletion has completed but before its unconditional catalog deletion.</li>
<li>Concurrent PUT publishes <code>replacement</code>, updates the catalog, and returns 201.</li>
<li>Resume the earlier DELETE; it removes the catalog entry by bucket/key and returns 204.</li>
</ul>
<p>The reproduction subclasses the real POSIX driver only to pause at this scheduling seam; it calls the real conditional backend deletion and uses the real SQLite catalog.</p>
<h5 id="issue-157-expected-and-actual">Expected and actual</h5>
<p>Expected: operations have an explicit ordering/conflict contract. When a PUT publishes a replacement after the old backend generation has been deleted, the earlier DELETE cannot silently remove the replacement's catalog state. After both requests settle, storage and catalog agree; a successful surviving replacement remains retrievable. A conflicting operation may instead fail explicitly under a documented contract.</p>
<p>Actual:</p>
<pre><code class="language-text">initial PUT: 201
concurrent replacement PUT: 201
original DELETE: [204]
GET after both success: 404
catalog record: None
bytes still in storage: b'replacement'
</code></pre>
<p>This demonstrates catalog inconsistency and loss of API accessibility, <strong>not physical loss of the replacement bytes</strong>.</p>
<h5 id="issue-157-relevant-source">Relevant source</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/api/gateway.py#L627-L653">PUT writes storage then upserts the catalog</a>.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/api/gateway.py#L760-L778">DELETE conditionally removes backend generation then unconditionally deletes catalog key</a>.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/db/catalog.py#L1986-L2008">SQLCatalog.delete deletes the currently matching bucket/key row</a>.</li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/cognistore/api/gateway.py#L501-L505">API reads require a catalog record</a>.</li>
</ul>
<h5 id="issue-157-reproduction">Reproduction</h5>
<p>In a checkout of the assessed revision with the repository's development dependencies installed, save the following as <code>/tmp/delete_put_race_repro.py</code> and run <code>PYTHONPATH=. python /tmp/delete_put_race_repro.py</code> from the repository root. It uses only temporary storage and mock JWT keys. The <code>development</code> profile permits this local harness; JWT authentication and RBAC are explicitly enabled in the app.</p>
<p>&lt;details&gt;
&lt;summary&gt;Self-contained deterministic reproduction using the repository's JWT fixture&lt;/summary&gt;</p>
<pre><code class="language-python">&quot;&quot;&quot;Deterministic scheduler seam between backend delete and catalog delete.&quot;&quot;&quot;
import os
os.environ['COGNISTORE_SECURITY_PROFILE'] = 'development'
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Event, Thread
from fastapi.testclient import TestClient
from cognistore.api.app import create_app
from cognistore.api.gateway import CogniStoreGateway
from cognistore.auth.authorization import RBACAuthorizer, RBACPolicy
from cognistore.db import SQLCatalog
from cognistore.drivers.posix_driver import PosixDriver
from tests.unit.test_api_authentication import identity_provider, ISSUER

removed = Event()
release_delete = Event()
class PausingDelete(PosixDriver):
    def delete_object_if_generation(self, bucket, key, generation):
        result = super().delete_object_if_generation(bucket, key, generation)
        removed.set()
        if not release_delete.wait(10):
            raise RuntimeError('reproduction did not release delete')
        return result
fixture = identity_provider.__wrapped__()
auth, token, _ = next(fixture)
try:
    with TemporaryDirectory(prefix='cognistore-audit-race-') as folder:
        raw = PausingDelete(str(Path(folder) / 'hot'))
        with SQLCatalog(Path(folder) / 'catalog.db') as catalog:
            gateway = CogniStoreGateway(catalog, {'hot': raw})
            app = create_app(gateway, authentication=auth, authorization=RBACAuthorizer(RBACPolicy({(ISSUER, 'alice'): ['writer']})))
            headers = {'Authorization': 'Bearer ' + token('alice')}
            url = '/v1/objects/hot/bucket/item'
            statuses = []
            with TestClient(app) as client:
                print('initial PUT:', client.put(url, headers=headers, content=b'initial').status_code)
                deleting = Thread(target=lambda: statuses.append(client.delete(url, headers=headers).status_code))
                deleting.start()
                assert removed.wait(10)
                try:
                    print('concurrent replacement PUT:', client.put(url, headers=headers, content=b'replacement').status_code)
                finally:
                    release_delete.set()
                deleting.join(10)
                assert not deleting.is_alive()
                print('original DELETE:', statuses)
                print('GET after both success:', client.get(url, headers=headers).status_code)
                print('catalog record:', catalog.get('bucket', 'item'))
                print('bytes still in storage:', raw.get_object('bucket', 'item'))
finally:
    try:
        next(fixture)
    except StopIteration:
        pass
</code></pre>
<p>&lt;/details&gt;</p>
<h5 id="issue-157-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Define the ordering/conflict contract for overlapping PUT and DELETE on one tenant/bucket/key and enforce it across backend and catalog publication.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Add a deterministic regression at the exact interleaving above using the real SQLite catalog and POSIX backend. After successful replacement publication, the stale DELETE cannot erase its catalog state.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Cover opposite operation order, repeated DELETE, failed conditional backend deletion, and failure during catalog finalization; ensure returned statuses and catalog/backend state match the documented contract.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Validate the mechanism with distinct gateway/catalog instances and separate worker processes for supported shared database/storage deployments; an in-process mutex alone is insufficient for a multi-process contract.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Preserve legal-hold serialization and audit outcomes, and keep unrelated keys and tenants independent.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Run relevant API, catalog, legal-hold, storage, and supported database integration tests; record fix revision and evidence in release readiness.</li>
</ul>
<h5 id="issue-157-scope-boundaries">Scope boundaries</h5>
<p>Fix the confirmed API PUT/DELETE race and direct failure/retry semantics. Do not describe the result as physical replacement-byte loss or claim all backends/database combinations have been reproduced. The broader PUT/DELETE/move/scan concurrency matrix, performance qualification, and recovery drills remain separate audit/qualification work. If the fix changes a shared mutation primitive, add focused coverage for its affected callers and link any additional discovered defects.</p>
<h5 id="issue-157-dependencies">Dependencies</h5>
<p>None. This ticket can start immediately.</p>
<h5 id="issue-157-related-delivery">Related delivery</h5>
<p><a href="https://github.com/melliott18/CogniStore/issues/39">#39</a> (REST API), <a href="https://github.com/melliott18/CogniStore/issues/30">#30</a> (persistent catalog), <a href="https://github.com/melliott18/CogniStore/issues/62">#62</a> (legal holds). These delivered capabilities remain completed; this ticket addresses a newly reproduced defect.</p>

#### [#158 — \[M5\] Restore hosted CI and qualify the supported runtime matrix](https://github.com/melliott18/CogniStore/issues/158)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:delivery`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-158-goal">Goal</h5>
<p>Restore trustworthy hosted release gates. The latest main run failed before any steps started because of account payments/spending limits; this is an infrastructure block, not a passing or failing application test result.</p>
<h5 id="issue-158-scope">Scope</h5>
<ul>
<li>Have the repository/account owner resolve the Actions billing or spending-limit block, then run the existing workflows without weakening gates.</li>
<li>Verify Python 3.10–3.14, default collection, service-enabled integrations, native libmagic, quality/security/package gates, and the relevant Helm/Terraform/Kubernetes workflows.</li>
<li>Separate emulator, mocked infrastructure, live-cloud and skipped coverage in the resulting evidence.</li>
</ul>
<h5 id="issue-158-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Actions jobs actually start, and fresh hosted checks on a recorded main revision pass; link run URLs and artifacts.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> All supported Python versions run the configured gates; coverage meets the existing 80% minimum and skips/xfails have explicit reasons.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Relevant deployment workflows execute; a historical green run, mocked Terraform plan or development kind pass is not represented as real-cloud production qualification.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Required checks and failure visibility are reviewed, with an owner/runbook for future CI outages; the final release candidate is checked again in the candidate ticket.</li>
</ul>
<h5 id="issue-158-dependencies">Dependencies</h5>
<p>None. This ticket can start immediately.</p>
<h5 id="issue-158-out-of-scope">Out of scope</h5>
<p>Changing billing/payment settings without the account owner's action; silently replacing hosted checks with local passes; weakening existing gates.</p>
<h5 id="issue-158-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/actions/runs/35482921733">35482921733</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/.github/workflows/ci.yml">ci.yml</a></li>
</ul>

#### [#159 — \[M5\] Define the first production deployment, workload, and pilot acceptance gates](https://github.com/melliott18/CogniStore/issues/159)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:delivery`, `area:docs`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-159-goal">Goal</h5>
<p>Make production readiness a measurable claim for one declared deployment and workload. Existing feature acceptance does not select an operating environment or establish production SLOs.</p>
<h5 id="issue-159-scope">Scope</h5>
<ul>
<li>Select the first backend(s), OS/filesystem semantics, catalog/broker topology, tenant model, scheduler mode, keyword-index ownership, and required user workflows.</li>
<li>Record object-size distribution, data volume, request/concurrency pattern and growth assumptions. Decide whether full semantic Ask, a real model provider, PII, Azure extras, or GCS spooling are required.</li>
<li>Define named operator/security owners, recovery point/time objectives, availability/latency/throughput/capacity targets, pilot cohort/data bounds/duration, and stop/rollback conditions.</li>
<li>Update the forward-looking roadmap and generated ticket mirror to reference this M5 initiative while preserving M1–M4 closeout history.</li>
</ul>
<h5 id="issue-159-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A reviewed, versioned deployment/workload specification names in-scope features and explicitly excluded/unsupported pilot combinations.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Numeric pass/fail targets and test duration/sample-size requirements are chosen before qualification; RPO/RTO, performance and capacity targets are measurable.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Known boundaries have decisions: metadata-only default API, production provider composition, PII/search interaction, single-node scheduler, keyword ownership, encryption attestations, optional backend dependencies and temporary storage sizing.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Named owners and pilot entry/exit/stop criteria are recorded; credentials or customer data are not included in the specification.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Roadmap and generated ticket mirror show the new work and dependencies without reopening completed delivery acceptance.</li>
</ul>
<h5 id="issue-159-dependencies">Dependencies</h5>
<p>None. This ticket can start immediately.</p>
<h5 id="issue-159-out-of-scope">Out of scope</h5>
<p>Qualifying every cloud/topology or inventing customer requirements; implementing a new distributed scheduler, OCR or multi-region platform.</p>
<h5 id="issue-159-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/reference_architectures.md">reference_architectures.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/evidence/m4/README.md">README.md</a></li>
</ul>

#### [#160 — \[M5\] Assemble and freeze a reproducible release candidate for the selected workload](https://github.com/melliott18/CogniStore/issues/160)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:delivery`, `area:indexing`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-160-goal">Goal</h5>
<p>Produce one installable candidate whose exact code, dependencies, providers and configuration can be qualified and promoted.</p>
<h5 id="issue-160-scope">Scope</h5>
<ul>
<li>Build from a clean main revision containing both fixes; version the package/image and record immutable source and image identities.</li>
<li>Capture resolved dependencies, runtime extras, migration versions, sanitized configuration identity and component inventory/SBOM so later dependency resolution cannot silently change the qualified artifact.</li>
<li>Wire and document only the product integrations selected by the target ticket. If full Ask is required, assemble the actual keyword/vector/answer providers rather than shipping metadata-only Ask or sample models.</li>
<li>Run clean-install and installed-artifact smoke plus security and dependency checks.</li>
</ul>
<h5 id="issue-160-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A release manifest binds source SHA, image digest, package/dependency identities, migrations, configuration revision, provider/model identities and build instructions.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A clean environment installs the exact artifact and executes the selected storage/query/action workflows; required Azure/embedding extras and GCS spool capacity are present when selected.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> The fixes' regression tests and applicable hosted gates pass on this exact candidate; security scan findings have documented disposition and no unresolved release blockers.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Production search/Ask integration is verified when in scope; otherwise its exclusion and metadata-only behavior are explicit in product/operator documentation.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A change-control rule identifies which code/config/dependency changes invalidate prior evidence and require requalification.</li>
</ul>
<h5 id="issue-160-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/156">#156</a> — [M5] [P1] Prevent case-insensitive POSIX tenant namespace aliases from bypassing isolation and legal holds</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/157">#157</a> — [M5] [P1] Prevent an older DELETE from removing a concurrent replacement PUT catalog record</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/158">#158</a> — [M5] Restore hosted CI and qualify the supported runtime matrix</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — [M5] Define the first production deployment, workload, and pilot acceptance gates</li>
</ul>
<h5 id="issue-160-out-of-scope">Out of scope</h5>
<p>General model benchmarking, unrelated new features, publishing a general-availability release or enabling customer traffic.</p>
<h5 id="issue-160-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/rest_api.md">rest_api.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/setup_guide.md">setup_guide.md</a></li>
</ul>

#### [#162 — \[M5\] Audit system safety, security, and data consistency for the release candidate](https://github.com/melliott18/CogniStore/issues/162)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:control-plane`, `area:orchestration`, `area:security`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-162-goal">Goal</h5>
<p>Complete a bounded full-system audit across the selected product's trust boundaries and data mutation paths, with evidence and remediation rather than a checklist-only signoff.</p>
<h5 id="issue-162-scope">Scope</h5>
<ul>
<li>Inventory entry points, privileged services, storage/catalog/queue ownership, tenant boundaries and supported deployment assumptions.</li>
<li>Review JWT/RBAC, tenant aliases and scoped search/citations, trusted broker identity assertions, legal holds, audit integrity/checkpoints, secrets, redaction and unsafe configuration.</li>
<li>Exercise overlapping PUT/DELETE/move/scan/repair/cleanup operations, crash windows, generation fences, storage/catalog publication failures and authorization changes during queued work.</li>
<li>Review dependency/artifact security and production configuration; link new defects to separate remediation tickets with owners and release-blocking disposition.</li>
</ul>
<h5 id="issue-162-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A versioned audit matrix maps every scoped trust boundary and mutation family to code review, adversarial tests, staging checks and retained sanitized evidence.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Both known defects are reproduced on the affected baseline and verified fixed in the candidate; case-insensitive behavior is exercised on a real applicable filesystem or an explicitly enforced unsupported-configuration boundary.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Concurrency and failure tests cover SQLite and the selected production catalog/backend, including multiple processes/replicas where supported; no unexplained data divergence, cross-tenant access or hold bypass remains.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Findings have severity, trigger, evidence, owner and disposition; all release-blocking findings are remediated and retested before audit signoff.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Final audit conclusion names exact candidate/config/environment identities and residual operating limits; a code change after signoff triggers scoped re-audit.</li>
</ul>
<h5 id="issue-162-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — [M5] Define the first production deployment, workload, and pilot acceptance gates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/160">#160</a> — [M5] Assemble and freeze a reproducible release candidate for the selected workload</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/161">#161</a> — [M5] Deploy isolated staging with the selected production security and infrastructure configuration</li>
</ul>
<p>Source/configuration review can start as soon as the target is selected and can run alongside fixes and staging assembly. Listed dependencies are required for final candidate/staging signoff, not a prohibition on early review.</p>
<h5 id="issue-162-out-of-scope">Out of scope</h5>
<p>Universal security certification, all possible cloud/topology combinations, replacing live recovery/load/UAT tickets, or closing discovered blockers by merely filing them.</p>
<h5 id="issue-162-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/audit_coverage.md">audit_coverage.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/operator_security.md">operator_security.md</a></li>
</ul>

#### [#161 — \[M5\] Deploy isolated staging with the selected production security and infrastructure configuration](https://github.com/melliott18/CogniStore/issues/161)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:delivery`, `area:security`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-161-goal">Goal</h5>
<p>Provide a production-configured environment for qualification of the exact candidate, using synthetic data and the actual selected services.</p>
<h5 id="issue-161-scope">Scope</h5>
<ul>
<li>Provision the selected topology using the reviewed deployment configuration, including actual target storage, PostgreSQL/pgvector and persistent JetStream where selected.</li>
<li>Configure real OIDC/JWT, RBAC and tenant policies, verified TLS, secret delivery, encrypted persistent/temporary state and backups, least-privilege service access and enforced network policy.</li>
<li>Configure operator telemetry, alert destinations and private monitoring access without exposing aggregate tenant metrics or weakening tenant isolation.</li>
<li>Record environment identity, cost bounds, ownership and teardown/recovery procedure.</li>
</ul>
<h5 id="issue-161-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> The release manifest's image/configuration identities are running; topology and provisioned resources are inventoried with sanitized evidence.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Authenticated storage and queued-job smoke succeeds; missing/wrong credentials, plaintext or untrusted certificates and denied network paths fail as intended.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> At-rest encryption and backup configuration are verified through platform evidence, not solely application attestations; keys and recovery access have owners.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Tenant-safe telemetry is observable and an end-to-end test notification reaches the designated operator.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Environment is isolated from production/customer data with documented spend bounds, access controls and teardown instructions; passing health endpoints alone does not close the ticket.</li>
</ul>
<h5 id="issue-161-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — [M5] Define the first production deployment, workload, and pilot acceptance gates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/160">#160</a> — [M5] Assemble and freeze a reproducible release candidate for the selected workload</li>
</ul>
<h5 id="issue-161-out-of-scope">Out of scope</h5>
<p>Customer onboarding, multi-cloud rollout or representing mocked Terraform plans as an actual deployment.</p>
<h5 id="issue-161-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/kubernetes.md">kubernetes.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/terraform.md">terraform.md</a></li>
</ul>

#### [#163 — \[M5\] Run manual end-to-end acceptance across CLI, API, SDK, search, and administration](https://github.com/melliott18/CogniStore/issues/163)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:api`, `area:indexing`, `area:ui`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-163-goal">Goal</h5>
<p>Demonstrate that real users/operators can complete the selected workflows and understand failures on the candidate in staging.</p>
<h5 id="issue-163-scope">Scope</h5>
<ul>
<li>Create a repeatable manual test matrix with expected results, seeded synthetic corpus, identities and evidence capture.</li>
<li>Exercise CLI/API/SDK upload, scan, exact-byte download, range reads, durable jobs, policy preview/execution and consistency/repair workflows.</li>
<li>Use an actual browser for selected search/Ask and admin flows, including citations/downloads, roles, confirmations, job state, audit evidence, errors and empty results.</li>
<li>Include negative identity/tenant/hold scenarios and product boundaries selected in the target ticket.</li>
</ul>
<h5 id="issue-163-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Every in-scope scenario records tester, candidate/config revision, inputs, expected/actual result and sanitized screenshot/log evidence; actual browser interaction is included.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Object bytes/checksums and catalog placement agree; policy/repair previews cause no mutation, and queued responses are followed through terminal outcomes and actual storage effects.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Reader/writer/operator/auditor and at least two tenants demonstrate permitted and denied operations, including direct known IDs, search/citations, expired/wrong-audience tokens and legal-hold protection.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Required production providers are tested with relevant/no-match/provider-unavailable queries, grounded citations and downloads; sample models do not substitute for production quality checks.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Unsupported formats, extraction limits and PII/search interaction are verified and documented; all release-blocking findings are fixed and affected scenarios rerun.</li>
</ul>
<h5 id="issue-163-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — [M5] Define the first production deployment, workload, and pilot acceptance gates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/161">#161</a> — [M5] Deploy isolated staging with the selected production security and infrastructure configuration</li>
</ul>
<h5 id="issue-163-out-of-scope">Out of scope</h5>
<p>Adding every missing feature, testing only HTML shell responses, or certifying model quality from deterministic demonstration queries.</p>
<h5 id="issue-163-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/admin_ui.md">admin_ui.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/content_search_sample.md">content_search_sample.md</a></li>
</ul>

#### [#164 — \[M5\] Qualify failure recovery, coherent restore, credential rotation, and upgrade rollback](https://github.com/melliott18/CogniStore/issues/164)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:control-plane`, `area:delivery`, `area:orchestration`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-164-goal">Goal</h5>
<p>Show that operators can recover the selected deployment with data, governance and job state intact within defined recovery targets.</p>
<h5 id="issue-164-scope">Scope</h5>
<ul>
<li>Interrupt workers during transfers and catalog publication, and interrupt database, broker and storage connectivity; verify retry/DLQ/redrive and uncertain-submission reconciliation.</li>
<li>Rotate credentials and TLS trust material under work, revoke application grants and inspect queued-work revalidation.</li>
<li>Restore a coherent fenced recovery set into isolation: all tenant catalogs, object versions/sidecars, encryption-key access, main/DLQ streams and consumer state, configuration and optional scheduler state.</li>
<li>Upgrade and roll back the candidate through the supported application/schema procedure, including pending moves and generation-token changes.</li>
</ul>
<h5 id="issue-164-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Recorded drills show source preservation/integrity, durable job identity and safe recovery or explicit quarantine for interrupted moves; no silent loss, unsafe cleanup or duplicate effects.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A full restore includes pending nonterminal work and all tenants; hashes, catalog placement, holds, audit continuity/checkpoints and queue/scheduler state reconcile before writers resume.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Measured recovery point/time meet the target ticket's RPO/RTO; key/credential availability and restored-generation mismatch handling are demonstrated.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Credential/CA rotation and revoked RBAC/tenant grants behave as documented; token expiry is not misrepresented as automatic queued-job cancellation.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Upgrade/rollback preserves or safely restores data/schema compatibility, with measured outage and tested operator runbooks; unresolved blockers are fixed and retested.</li>
</ul>
<h5 id="issue-164-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — [M5] Define the first production deployment, workload, and pilot acceptance gates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/161">#161</a> — [M5] Deploy isolated staging with the selected production security and infrastructure configuration</li>
</ul>
<h5 id="issue-164-out-of-scope">Out of scope</h5>
<p>Physical power-loss certification or cross-region DR unless explicitly selected; claiming a quiesced local SQLite drill qualifies production recovery.</p>
<h5 id="issue-164-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/operator_lifecycle.md">operator_lifecycle.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/operator_incidents.md">operator_incidents.md</a></li>
</ul>

#### [#165 — \[M5\] Qualify realistic load, sustained operation, capacity limits, and alert delivery](https://github.com/melliott18/CogniStore/issues/165)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:observability`, `area:orchestration`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-165-goal">Goal</h5>
<p>Measure whether the intended deployment meets its workload/SLO targets and fails visibly and safely at capacity.</p>
<h5 id="issue-165-scope">Scope</h5>
<ul>
<li>Run the workload distribution, concurrency, dataset size and soak duration selected before testing, including hot spots and mixed storage/search/policy work.</li>
<li>Measure latency distributions, throughput, errors, backlog/age, resource growth, connection counts, temporary storage, actual cost inputs and scaling behavior.</li>
<li>Exercise saturation/admission failure, bounded retries, backend throttling and recovery; test real notification routes and runbook execution.</li>
<li>Verify operating headroom for selected GCS spool, SQLite scheduler and keyword-index ownership limits.</li>
</ul>
<h5 id="issue-165-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> A retained report identifies exact candidate/config/environment, workload, duration, metrics, pass/fail thresholds and repeatable commands.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Latency/throughput/error and recovery targets pass, with bounded resource/backlog behavior and documented capacity headroom over the required workload.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Object/checksum/catalog/audit consistency is verified after load and fault periods; performance improvements do not weaken integrity or isolation.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Saturation or rejected submissions produce actionable alerts before silent service loss; alert thresholds are reachable relative to hard queue/storage limits.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Real notifications reach the named operator with runbooks and clear on recovery; tenant isolation remains enabled and private telemetry supports diagnosis.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Scaling and cost/capacity recommendations are based on measured behavior, with blockers resolved and affected tests rerun.</li>
</ul>
<h5 id="issue-165-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/159">#159</a> — [M5] Define the first production deployment, workload, and pilot acceptance gates</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/161">#161</a> — [M5] Deploy isolated staging with the selected production security and infrastructure configuration</li>
</ul>
<h5 id="issue-165-out-of-scope">Out of scope</h5>
<p>Treating historical million-object movement or synthetic Prometheus rule tests as proof of current production SLOs; optimizing excluded workloads.</p>
<h5 id="issue-165-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/operational_slos.md">operational_slos.md</a></li>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/scale_qualification.md">scale_qualification.md</a></li>
</ul>

#### [#166 — \[M5\] Run a gated pilot and decide expansion from measured results](https://github.com/melliott18/CogniStore/issues/166)

- **Kind:** Original delivery ticket
- **Status:** Open
- **Milestone:** M5 – Release readiness and controlled pilot
- **Labels:** `area:delivery`, `area:docs`, `roadmap`, `type:chore`
- **Last updated:** 2026-09-20

<p>Parent epic: <a href="https://github.com/melliott18/CogniStore/issues/155">#155</a>
Milestone: M5 – Release readiness and controlled pilot</p>
<h5 id="issue-166-goal">Goal</h5>
<p>Use a bounded real-world pilot to validate usefulness and operations after release blockers and qualification gates are closed.</p>
<h5 id="issue-166-scope">Scope</h5>
<ul>
<li>Review the candidate/evidence bundle against the target's entry criteria and record a named go/no-go decision before onboarding pilot data.</li>
<li>Enroll only the agreed cohort and workload, with data permissions, backups, on-call ownership, telemetry, support path and rollback/stop procedures.</li>
<li>Run for the agreed duration/volume, reviewing incidents, integrity, recovery, performance, product usefulness and operational effort.</li>
<li>Publish an expand/fix/stop decision and prioritize the next roadmap from actual findings and user needs.</li>
</ul>
<h5 id="issue-166-acceptance-criteria">Acceptance criteria</h5>
<ul class="contains-task-list">
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Entry approval identifies the exact qualified image/configuration and links passing audit, UAT, recovery and load evidence; no unresolved release blockers remain.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Pilot cohort/data bounds, operating owners, review cadence and stop/rollback triggers match the target specification and are active before first use.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> The agreed pilot duration/workload completes with retained integrity, SLO, incident and user-task evidence; any material candidate change receives scoped requalification.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> Exit review records whether predefined success criteria passed and makes an explicit expand/fix/stop decision with responsible owner.</li>
<li class="task-list-item"><input class="task-list-item-checkbox" disabled="disabled" type="checkbox"> New defects/needs become linked, prioritized tickets; roadmap, operator documentation and generated ticket mirror reflect the decision. A stopped/failed pilot does not automatically close the epic as successful.</li>
</ul>
<h5 id="issue-166-dependencies">Dependencies</h5>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/issues/162">#162</a> — [M5] Audit system safety, security, and data consistency for the release candidate</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/163">#163</a> — [M5] Run manual end-to-end acceptance across CLI, API, SDK, search, and administration</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/164">#164</a> — [M5] Qualify failure recovery, coherent restore, credential rotation, and upgrade rollback</li>
<li><a href="https://github.com/melliott18/CogniStore/issues/165">#165</a> — [M5] Qualify realistic load, sustained operation, capacity limits, and alert delivery</li>
</ul>
<h5 id="issue-166-out-of-scope">Out of scope</h5>
<p>Unbounded rollout, general availability solely because tickets are closed, or onboarding important data before entry gates pass.</p>
<h5 id="issue-166-evidence-and-source">Evidence and source</h5>
<p>Retain sanitized evidence in the repository or durable linked CI/artifact storage, bound to source, image, configuration and environment identities. Local ignored paths alone are insufficient.</p>
<ul>
<li><a href="https://github.com/melliott18/CogniStore/blob/2cce6ff4d43fd287ad008197f1e17f168c3580c4/docs/operator_handbook.md">operator_handbook.md</a></li>
</ul>
