# Audit event coverage

This matrix defines the security and lifecycle evidence required at CogniStore's
application boundaries. All catalog events use the operation's correlation ID,
a trusted actor, and the bound tenant's audit chain. Job and move events also
carry their durable IDs. A move or scan outcome points back to its initiating
event; worker attempt events share job/correlation IDs, and failure-driven
retry/dead-letter outcomes point to their failure event.

The linked tests execute producers and check persisted evidence, attribution,
causation, or transaction/settlement ordering. A row does not claim that every
storage SDK call or raw database mutation is separately audited. Operational
procedures and threat boundaries are in [Operational audit events](audit_events.md).

## API and authorization

| Required action | Required evidence | Automated evidence |
| --- | --- | --- |
| Authenticate and authorize a protected API operation; allow a read or deny an operation | Unsampled `authorization.decision`, with operation, decision, verified actor and request correlation. A denied request cannot reach its backend. | [test_read_allow_and_denial_coverage_is_complete](../tests/unit/test_authorization.py#L188); [test_every_endpoint_enforces_role_matrix](../tests/unit/test_api_authorization.py#L95); [test_denials_precede_existence_validation_and_body_reads](../tests/unit/test_api_authorization.py#L172) |
| Submit a catalog scan or policy run | `job.queued` before publication; job ID and request correlation survive into worker status. Submission failure is `job.submission_failed`. | [test_async_actions_return_202_and_worker_updates_status](../tests/unit/test_rest_api.py#L1149); [test_api_queue_failure_has_correlated_submission_evidence](../tests/unit/test_audit_coverage.py#L242) |
| Read/list retained audit records | `authorization.decision` and `audit.access` before disclosure, in the caller's tenant. A foreign event ID is unavailable. | [test_audit_list_get_are_tenant_scoped_and_self_audited](../tests/unit/test_api_audit.py#L52) |
| Export audit evidence | `audit.export`; bounded tenant-scoped pages share a fixed checkpoint and include proofs for independent verification. | [test_export_pagination_has_fixed_checkpoint_and_verifies_independently](../tests/unit/test_api_audit.py#L88) |
| Verify a complete exported archive offline | Every page, link, payload, and retention tombstone must match the independently retained checkpoint; missing or altered evidence fails verification. | [Offline export verification tests](../tests/unit/test_audit_export_verification.py) |
| Verify audit integrity | `audit.verification` with the result and caller correlation; supplied checkpoint mismatches remain failures. | [test_checkpoint_mismatch_is_returned_and_failure_is_audited](../tests/unit/test_api_audit.py#L214) |
| Deny audit access, or fail to persist a disclosure event | Authorization denial is retained; unavailable self-audit storage prevents disclosure. | [test_reader_denials_are_audited_before_disclosure](../tests/unit/test_api_audit.py#L157); [test_self_audit_failure_closes_disclosure](../tests/unit/test_api_audit.py#L186) |

## Policy and worker

| Required action | Required evidence | Automated evidence |
| --- | --- | --- |
| Evaluate an executing policy, including staying or rejecting a proposal | `policy.decision` with structured reason and feature evidence. Completed evaluations survive a later batch/preflight failure. | [test_batch_preflight_failure_retains_every_reason_without_moving](../tests/unit/test_policy_reason_runner.py#L41); [test_rejected_proposals_have_structured_codes](../tests/unit/test_policy_reason_runner.py#L238) |
| Execute a policy-selected move | One causal path from `policy.decision` through `move.prepared`, `move.transitioned`, and `move.completed`, preserving actor, object, job, and correlation. | [test_policy_storage_move_has_complete_correlated_causal_path](../tests/unit/test_audit_coverage.py#L171); [test_policy_handler_correlates_worker_decisions_and_moves](../tests/unit/test_job_handlers.py#L536) |
| Authorize a worker delivery, including retry/redrive | `authorization.decision` before leases and handler side effects; current grants are checked again rather than trusting an old grant. | [test_worker_checks_policy_and_persists_decision_outside_event_loop](../tests/unit/test_worker_authorization.py#L116); [test_retry_and_redrive_recheck_revoked_file_bindings](../tests/unit/test_worker_authorization.py#L234) |
| Start and successfully finish an executing job | `job.started` before the handler; `job.succeeded` before ACK. This includes CLI/scheduled jobs without API status metadata when an audit catalog is configured. A coordinator-skipped terminal replay retains its original outcome instead of inventing success. | [test_worker_terminal_replay_does_not_invent_success](../tests/unit/test_audit_coverage.py#L275); [test_worker_success_is_audited_without_api_status_metadata_before_ack](../tests/unit/test_audit_coverage.py#L130) |
| Retry a failed job | `job.failure` then causally linked `job.retry_scheduled`, durable before NACK and after restart. Error classifications exclude raw exception messages and payloads. | [test_failure_and_retry_audits_are_durable_before_nack_and_restart](../tests/unit/test_worker_runtime.py#L757) |
| Interrupt a running job for worker shutdown | `job.retry_scheduled` with the fixed cancellation reason before NACK; no success is recorded. | [test_worker_cancellation_is_audited_before_nack](../tests/unit/test_audit_coverage.py#L202) |
| Dead-letter a terminal/exhausted job | `job.failure` and linked `job.dead_lettered`, with broker receipt metadata, before source ACK. | [test_dead_letter_audit_is_durable_before_source_ack](../tests/unit/test_worker_runtime.py#L878) |
| Lose the worker audit store | Handler start/settlement fails closed; the source remains unsettled. | [test_audit_failure_leaves_source_unsettled_and_fails_worker_closed](../tests/unit/test_worker_runtime.py#L945) |

## Storage and catalog observations

| Required action | Required evidence | Automated evidence |
| --- | --- | --- |
| API object write/delete; CLI put/get | `storage.operation` started before I/O, followed by a causally linked succeeded/failed outcome. Identity and object coordinates are retained; failure text/content is excluded. | [test_api_write_and_delete_have_correlated_intent_and_outcome](../tests/unit/test_storage_audit.py#L27); [test_cli_put_and_get_are_audited](../tests/unit/test_storage_audit.py#L92); [test_storage_failure_records_safe_error_and_verified_actor](../tests/unit/test_storage_audit.py#L50) |
| Fail to persist storage intent | No storage mutation begins. A terminal audit failure after I/O leaves an unresolved intent for investigation. | [test_unavailable_intent_audit_prevents_storage_mutation](../tests/unit/test_storage_audit.py#L76) |
| Scan storage into a catalog, inline or in a worker | `catalog.scan_started` before reading/publishing observations; `catalog.scan_completed` reports the observed count, or `catalog.scan_failed` records a safe exception class. Events share actor/job/correlation; scan scope is in details. | [test_scan_emits_correlated_intent_and_outcome_in_own_tenant](../tests/unit/test_audit_coverage.py#L34); [test_scan_failure_is_correlated_and_excludes_exception_content](../tests/unit/test_audit_coverage.py#L58); [test_worker_scan_preserves_verified_actor_job_and_request](../tests/unit/test_audit_coverage.py#L104) |
| Retry/fail a physical move | Durable move sequence and causation retain `move.retry` and `move.failed` alongside transitions; failed audit append rolls back catalog state changes. | [test_retries_and_terminal_transition_advance_one_linear_move_head](../tests/unit/test_audit_events.py#L693); [test_move_creation_rolls_back_when_atomic_audit_append_conflicts](../tests/unit/test_audit_events.py#L592) |
| Preview a policy or scan | No mutation or lifecycle events. Authenticated API authorization remains independently audited. | [test_dry_run_does_not_persist_and_retry_freezes_original_reason](../tests/unit/test_policy_reason_runner.py#L82); [test_scan_audit_failure_prevents_storage_access_and_dry_run_never_audits](../tests/unit/test_audit_coverage.py#L78) |

## Governance and administration

| Required action | Required evidence | Automated evidence |
| --- | --- | --- |
| Assign/clear importance and reevaluate placement | `importance.changed` and `policy.decision`, retaining the committed revision and operation correlation. Tag and audit writes are atomic. | [test_api_tag_change_audits_and_reevaluates_without_moving](../tests/unit/test_placement_controls_surfaces.py#L27); [test_importance_and_audit_are_atomic_on_write_failure](../tests/unit/test_catalog_placement_controls.py#L32) |
| Configure a budget, reserve capacity, or use an override | `budget.configured` and `budget.reserved`; immutable definitions, admission evidence, and override actor survive recovery. | [test_budget_configuration_is_immutable_and_persistent](../tests/unit/test_budget_catalog.py#L37); [test_override_requires_actor_and_remains_audited_on_recovery](../tests/unit/test_budget_catalog.py#L142) |
| Reject a move for data locality, or use an approved exception | `locality.decision` precedes blocked storage I/O; an explicitly selected exception retains `locality.exception` evidence with the authenticated actor and approval. | [test_tenant_denial_precedes_any_storage_write_and_is_audited](../tests/unit/test_locality_mover.py#L126); [test_explicit_approved_exception_moves_and_records_the_approval](../tests/unit/test_locality_mover.py#L300) |
| Run a manual move | `manual.action` precedes the move lifecycle with operator identity and a continuous causal chain. | [test_direct_move_records_manual_and_terminal_move_chain](../tests/unit/test_cli_safety.py#L191) |
| Scan/resume/export a consistency report | Separate report journal: `consistency.started`, `consistency.resumed`, `consistency.checkpoint`, `consistency.completed`/`consistency.failed`, and `consistency.exported`. Tenant and source binding are checked without changing the source catalog. | [test_fixtures_classify_ticket_discrepancies_and_export_audited_report](../tests/unit/test_consistency.py#L106); [test_interrupted_storage_inventory_resumes_from_last_committed_page](../tests/unit/test_consistency.py#L397); [test_consistency_scan_and_export_extend_one_chain_concurrently](../tests/unit/test_consistency_audit_integrity.py#L160) |
| Verify/export consistency audit evidence | Report v2 has protected append-only records, a hash chain/head, per-event export proofs, and an optional external checkpoint. Damaged evidence stops read/resume/export. | [test_consistency_evidence_denies_direct_update_delete_replace](../tests/unit/test_consistency_audit_integrity.py#L51); [test_consistency_verification_detects_privileged_tampering_before_export](../tests/unit/test_consistency_audit_integrity.py#L60); [test_consistency_export_proves_scope_causation_and_verifiable_checkpoint](../tests/unit/test_consistency_audit_integrity.py#L82); [test_consistency_resume_refuses_damaged_evidence_before_source_access](../tests/unit/test_consistency_audit_integrity.py#L129) |
| Retain/prune catalog evidence | Authorized pruning records `audit.retention`, preserves immutable tombstone and chain evidence, and keeps replay idempotent. | [test_retention_preserves_chain_checkpoint_and_replay](../tests/unit/test_audit_integrity.py#L115) |
| Mutate retained catalog evidence through raw SQL | UPDATE/DELETE/REPLACE are denied; privileged removal of controls still leaves detectable altered/missing records unless the full history is rewritten. An external checkpoint detects rewritten prefixes. | [test_raw_sqlite_mutations_and_replace_are_denied](../tests/unit/test_audit_integrity.py#L173); [test_detects_privileged_corruption](../tests/unit/test_audit_integrity.py#L47); [test_checkpoint_detects_rewritten_local_history](../tests/unit/test_audit_integrity.py#L94) |

## Separate evidence and explicit boundaries

- A broker dead-letter redrive carries a broker receipt/audit chain, not an entry
  in the catalog integrity chain. See
  [test_dead_letter_publish_lookup_and_redrive_preserve_logical_job](../tests/unit/test_nats_queue.py#L335).
- Fenced scheduler recovery records its own recovery journal. It is a separate
  evidence store, not a claim that scheduler rows are covered by catalog hashes.
  See [test_fenced_stale_run_recovery_is_audited_idempotent_and_restart_safe](../tests/unit/test_scheduler.py#L1210).
- Consistency report v1 did not contain integrity proofs. It must be replaced by
  a new scan before using the v2 read/resume/export workflow; the new verifier
  does not certify preexisting unprotected content. See
  [test_unprotected_legacy_consistency_reports_require_a_new_scan](../tests/unit/test_consistency_audit_integrity.py#L151).
- Consistency proofs protect audit records, not report findings or scanner state.
  Retain the complete report and its checkpoint independently when preserving
  evidence. Catalog retention does not prune this separate report journal.
- Direct library calls and raw storage drivers are trusted internal components;
  use the API or audited workflows above for actor-attributed end-user activity.
  Anonymous local/development access has no verified principal. In-memory
  catalogs and workers configured without an audit catalog cannot provide durable
  evidence. Production operators must configure persistent catalogs.
- Authentication and tenant-resolution failures cannot be assigned to a
  verified tenant. Their denial events are retained in the operator/default
  catalog, separate from tenant histories.
- Read-only health/metrics endpoints, deployment configuration edits,
  provider-side Vault/KMS administration, and external cloud/storage changes are
  outside the tenant catalog action boundary. Preserve service/provider logs for
  those activities; this matrix does not claim that they emit tenant audit records.
