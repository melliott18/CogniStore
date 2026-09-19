"""Reproducible, privacy-filtered offline policy datasets from retained audit history.

Labels describe observed move execution, never placement quality or unobserved
access. All joins use causal audit evidence and a fixed observation horizon.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from cognistore.utils.redaction import redact

from .audit import AuditEvent, AuditEventType, AuditQuery, canonical_audit_timestamp
from .catalog import CatalogStore
from .pii import ClassifiedPIIFinding
from .policy import PIIPolicyRule
from .policy_reasons import reason_from_audit_details, validate_policy_reason
from .policy_snapshot import _placement_estimates_from_dict, snapshot_from_audit_details

POLICY_DATASET_SCHEMA_VERSION = 1
POLICY_OUTCOME_SCHEMA_VERSION = 1
POLICY_LABEL_SCHEMA_VERSION = 1
MAX_DATASET_QUERY_EVENTS = 10_000
DEFAULT_EXCLUDED_FIELDS = (
    "bucket",
    "key",
    "query",
    "content_sha256",
    "hit_content_sha256",
    "evidence_content_sha256",
    "document_text_sha256",
    "passage_text_sha256",
    "hot_name_patterns",
    "warm_name_patterns",
    "cold_name_patterns",
    "snapshot.decision.reason",
    "snapshot.features.**.reason",
)
_MOVE_TYPES = frozenset(
    {
        AuditEventType.MOVE_PREPARED.value,
        AuditEventType.MOVE_TRANSITIONED.value,
        AuditEventType.MOVE_COMPLETED.value,
        AuditEventType.MOVE_FAILED.value,
        AuditEventType.MOVE_RETRY.value,
    }
)
_TERMINAL_TYPES = frozenset({"move.completed", "move.failed"})
_OPTIONAL_FEATURE_NAMES = frozenset({"access", "placement_estimates", "pii"})
_FEATURE_NAMES = frozenset({"schema_version", "mime", "embeddings"}) | _OPTIONAL_FEATURE_NAMES
_LEAK_NAMES = frozenset(
    {
        "label",
        "labels",
        "outcome",
        "outcomes",
        "target",
        "targets",
        "decision",
        "destination_tier",
        "target_tier",
        "move_succeeded",
        "terminal_state",
    }
)


def _copy_json(value: object) -> Any:
    return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=True))


def _instant(value: object) -> str:
    if not isinstance(value, (str, datetime)):
        raise ValueError("timestamp must be timezone-aware ISO 8601 text")
    return canonical_audit_timestamp(value)


def _add_seconds(instant: str, seconds: int) -> str:
    try:
        return _instant(
            datetime.fromisoformat(instant.replace("Z", "+00:00")) + timedelta(seconds=seconds)
        )
    except OverflowError as exc:
        raise ValueError("observation window exceeds timestamp range") from exc


def _exclusive_upper(instant: str) -> str | None:
    try:
        return _instant(
            datetime.fromisoformat(instant.replace("Z", "+00:00")) + timedelta(microseconds=1)
        )
    except OverflowError:
        return None  # datetime.max has no representable later event.


def _version(value: object) -> bool:
    return type(value) is int and value == 1


def _path_matches(pattern: str, path: tuple[str, ...]) -> bool:
    parts = tuple(pattern.split("."))
    if len(parts) == 1:
        return bool(path) and parts[0] in {path[-1], "*"}

    def match(tokens: tuple[str, ...], remaining: tuple[str, ...]) -> bool:
        if not tokens:
            return not remaining
        if tokens[0] == "**":
            return match(tokens[1:], remaining) or bool(remaining and match(tokens, remaining[1:]))
        return bool(
            remaining and tokens[0] in {"*", remaining[0]} and match(tokens[1:], remaining[1:])
        )

    return match(parts, path)


def _exclusions(fields: Sequence[str]) -> tuple[str, ...]:
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise ValueError("exclude_fields must be a sequence of field paths")
    for pattern in fields:
        if (
            not isinstance(pattern, str)
            or not pattern
            or any(
                not part or re.fullmatch(r"[A-Za-z0-9_-]+|\*\*?", part) is None
                for part in pattern.split(".")
            )
        ):
            raise ValueError("excluded fields must be dotted paths with optional wildcards")
    return tuple(sorted(set((*DEFAULT_EXCLUDED_FIELDS, *fields))))


def _filter_fields(value: Any, exclusions: Sequence[str], path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            name: _filter_fields(item, exclusions, (*path, name))
            for name, item in value.items()
            if not any(_path_matches(pattern, (*path, name)) for pattern in exclusions)
        }
    if isinstance(value, list):
        return [
            _filter_fields(item, exclusions, (*path, str(index)))
            for index, item in enumerate(value)
        ]
    return value


def _sampled(identifier: str, seed: str, rate: float) -> bool:
    encoded = json.dumps([seed, identifier], ensure_ascii=True, separators=(",", ":"))
    number = int(hashlib.sha256(encoded.encode("utf-8")).hexdigest(), 16)
    return number < int(rate * (1 << 256))


def _bounded_events(catalog: CatalogStore, query: AuditQuery) -> list[AuditEvent]:
    events = catalog.list_audit_events(query)
    if len(events) >= MAX_DATASET_QUERY_EVENTS:
        raise ValueError("dataset audit query reached 10000 events; narrow the selection window")
    return events


def _root_decision(
    catalog: CatalogStore,
    event: AuditEvent,
    cache: dict[str, AuditEvent | None],
    *,
    as_of: str,
) -> tuple[str, int] | None:
    """Find the nearest causal decision; never cross another decision or a gap."""
    parent_id = event.causation_id
    seen = {event.event_id}
    child_time = event.occurred_at
    for depth in range(1, MAX_DATASET_QUERY_EVENTS + 1):
        if parent_id is None or parent_id in seen:
            return None
        seen.add(parent_id)
        if parent_id not in cache:
            cache[parent_id] = catalog.get_audit_event(parent_id)
        parent = cache[parent_id]
        if parent is None or parent.recorded_at > as_of or parent.occurred_at > child_time:
            return None
        if parent.event_type == AuditEventType.POLICY_DECISION.value:
            return parent.event_id, depth
        if (
            parent.move_id != event.move_id
            or parent.correlation_id != event.correlation_id
            or parent.bucket != event.bucket
            or parent.object_key != event.object_key
        ):
            return None
        parent_id = parent.causation_id
        child_time = parent.occurred_at
    return None


def _label(
    snapshot: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    as_of: str,
    observation_seconds: int,
) -> dict[str, Any]:
    start = _instant(snapshot["decision_at"])
    end = _add_seconds(start, observation_seconds)
    status, value, evidence = "missing", None, None
    if snapshot["decision"]["outcome"] != "selected":
        status = "not_applicable"
    elif as_of < end:
        status = "pending"
    elif outcomes and outcomes[-1]["event_type"] in _TERMINAL_TYPES:
        status = "observed"
        value = int(outcomes[-1]["event_type"] == "move.completed")
        evidence = outcomes[-1]["event_id"]
    return {
        "schema_version": POLICY_LABEL_SCHEMA_VERSION,
        "name": "move_succeeded",
        "version": 1,
        "status": status,
        "value": value,
        "window_start": start,
        "window_end": end,
        "as_of": as_of,
        "evidence_event_id": evidence,
    }


def export_policy_dataset(
    catalog: CatalogStore,
    *,
    as_of: str,
    occurred_after: str | None = None,
    occurred_before: str | None = None,
    sample_rate: float = 1.0,
    seed: str = "0",
    exclude_fields: Sequence[str] = (),
    observation_seconds: int = 86400,
) -> dict[str, Any]:
    """Export a bounded audit selection; no live features or catalog writes.

    Source bounds select audit occurrence time, while label windows start at
    the stored actual decision time. Legacy decisions are counted and skipped.
    Additional exclusions may remove any field, but cannot remove contract
    fields needed to validate the export; such a request fails before output.
    """
    cutoff = _instant(as_of)
    after = None if occurred_after is None else _instant(occurred_after)
    before = None if occurred_before is None else _instant(occurred_before)
    if after is not None and before is not None and after >= before:
        raise ValueError("occurred_after must precede occurred_before")
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, (int, float))
        or not math.isfinite(sample_rate)
        or not 0 < sample_rate <= 1
    ):
        raise ValueError("sample_rate must be a finite number in (0, 1]")
    if not isinstance(seed, str) or not seed or redact(seed) != seed:
        raise ValueError("seed must be nonempty text without credentials")
    if type(observation_seconds) is not int or not 0 < observation_seconds <= 315360000:
        raise ValueError("observation_seconds must be from 1 to 315360000")
    exclusions = _exclusions(exclude_fields)
    upper = _exclusive_upper(cutoff)
    effective_before = before if upper is None else upper if before is None else min(before, upper)
    events = (
        []
        if after and effective_before and after >= effective_before
        else _bounded_events(
            catalog,
            AuditQuery(
                event_types=frozenset({AuditEventType.POLICY_DECISION.value}),
                occurred_after=after,
                occurred_before=effective_before,
                limit=MAX_DATASET_QUERY_EVENTS,
            ),
        )
    )
    rows: list[dict[str, Any]] = []
    legacy_count = candidate_count = eligible_count = 0
    cache: dict[str, AuditEvent | None] = {}
    histories: dict[tuple[str, str, str], list[AuditEvent]] = {}
    for event in events:
        if event.occurred_at > cutoff or event.recorded_at > cutoff:
            continue
        candidate_count += 1
        snapshot = snapshot_from_audit_details(event.details)
        if snapshot is None:
            legacy_count += 1
            continue
        structured_reason = reason_from_audit_details(event.details)
        start = _instant(snapshot["decision_at"])
        if start > cutoff:
            continue
        eligible_count += 1
        if not _sampled(event.event_id, seed, float(sample_rate)):
            continue
        end = _add_seconds(start, observation_seconds)
        outcomes: list[dict[str, Any]] = []
        if snapshot["decision"]["outcome"] == "selected":
            correlation = event.correlation_id
            history_key = (correlation, start, min(end, cutoff))
            if history_key not in histories:
                # Stored secret identities use a reserved pseudonym namespace
                # that AuditQuery intentionally rejects as caller input.
                history = _bounded_events(
                    catalog,
                    AuditQuery(
                        correlation_id=(
                            None if correlation.startswith("[REDACTED:") else correlation
                        ),
                        occurred_after=start,
                        occurred_before=_exclusive_upper(min(end, cutoff)),
                        event_types=_MOVE_TYPES,
                        limit=MAX_DATASET_QUERY_EVENTS,
                    ),
                )
                histories[history_key] = history
                cache.update((item.event_id, item) for item in history)
            cache[event.event_id] = event
            for terminal in histories[history_key]:
                if (
                    terminal.correlation_id != correlation
                    or terminal.bucket != event.bucket
                    or terminal.object_key != event.object_key
                    or terminal.occurred_at < start
                    or terminal.occurred_at > min(end, cutoff)
                    or terminal.recorded_at > cutoff
                    or (event.move_id is not None and terminal.move_id != event.move_id)
                ):
                    continue
                ancestry = _root_decision(catalog, terminal, cache, as_of=cutoff)
                if ancestry is None or ancestry[0] != event.event_id:
                    continue
                outcomes.append(
                    {
                        "schema_version": POLICY_OUTCOME_SCHEMA_VERSION,
                        "event_id": terminal.event_id,
                        "move_id": terminal.move_id,
                        "event_type": terminal.event_type,
                        "occurred_at": terminal.occurred_at,
                        "recorded_at": terminal.recorded_at,
                        "outcome": terminal.outcome,
                        "sequence": ancestry[1],
                    }
                )
        # Audit move sequence disambiguates multiple transitions in one clock tick.
        outcomes.sort(
            key=lambda item: (item["occurred_at"], item["sequence"] or 0, item["event_id"])
        )
        row = {
            "schema_version": 1,
            "decision_id": event.event_id,
            "correlation_id": event.correlation_id,
            "job_id": event.job_id,
            "move_id": event.move_id,
            "snapshot": snapshot,
            "outcomes": outcomes,
            "label": _label(
                snapshot, outcomes, as_of=cutoff, observation_seconds=observation_seconds
            ),
        }
        # Reasons have their own versioned contract beside snapshot v1. Older
        # retained decisions have no such evidence; do not manufacture it.
        if structured_reason is not None:
            row["structured_reason"] = structured_reason
        safe = _filter_fields(redact(row), exclusions)
        if "structured_reason" in safe and safe["structured_reason"] != structured_reason:
            # A partially filtered reason is no longer a valid reason-v1
            # contract. Whole-field exclusion remains available for callers
            # with stricter privacy needs without inventing a lossy schema.
            raise ValueError(
                "partial structured_reason exclusions are unsupported; "
                "exclude structured_reason as a whole"
            )
        if safe.get("snapshot") != snapshot:
            if not isinstance(safe.get("snapshot"), dict):
                raise ValueError("exclusions cannot remove the snapshot contract")
            safe["snapshot"]["replay"] = {"supported": False, "reason": "export_redaction"}
        rows.append(safe)
    dataset = {
        "schema_version": POLICY_DATASET_SCHEMA_VERSION,
        "manifest": {
            "schema_version": 1,
            "as_of": cutoff,
            "observation_seconds": observation_seconds,
            "sampling": {
                "algorithm": "sha256",
                "seed": seed,
                "rate": float(sample_rate),
                "unit": "decision_id",
                "population": "retained_versioned_decisions",
            },
            "selection": {"occurred_after": after, "occurred_before": before},
            "candidate_count": candidate_count,
            "legacy_count": legacy_count,
            "eligible_count": eligible_count,
            "exported_count": len(rows),
            "excluded_fields": list(exclusions),
            "source": "retained_audit_events",
            "access_sampling": "per_snapshot; observed operations only",
            "label_definition": "last causal move state within the closed observation window",
        },
        "rows": rows,
    }
    issues = validate_policy_dataset(dataset, require_labels=False)
    if issues:
        # Do not echo row contents, which may contain unrecognized personal data.
        raise ValueError(
            "invalid policy dataset: " + ", ".join(sorted({issue["code"] for issue in issues}))
        )
    return dataset


def validate_policy_dataset(
    value: object,
    *,
    require_labels: bool = True,
) -> list[dict[str, str]]:
    """Validate versions, exclusions, label evidence and temporal boundaries.

    Findings contain field paths and static messages, never field values. This
    validates the contract, not authenticity of an untrusted exported document.
    """
    try:
        return _validate_policy_dataset(value, require_labels=require_labels)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        # A validator is also a boundary for hand-edited or untrusted JSON.
        # Unexpected container types must produce a finding, not a traceback.
        return [
            {
                "code": "invalid_schema",
                "path": "",
                "message": "Dataset contains malformed contract fields",
            }
        ]


def _validate_policy_dataset(
    value: object,
    *,
    require_labels: bool,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append({"code": code, "path": path, "message": message})

    def timestamp(item: object, path: str) -> str | None:
        try:
            return _instant(item)
        except ValueError:
            issue("invalid_timestamp", path, "Expected a timezone-aware timestamp")
            return None

    def version(item: object, path: str) -> None:
        if not _version(item):
            issue("unsupported_schema", path, "Expected schema version 1")

    try:
        dataset = _copy_json(value)
    except (TypeError, ValueError, RecursionError, OverflowError):
        issue("invalid_json", "", "Dataset must contain finite JSON values")
        return issues
    if not isinstance(dataset, dict):
        issue("invalid_schema", "", "Dataset must be an object")
        return issues
    version(dataset.get("schema_version"), "schema_version")
    manifest, rows = dataset.get("manifest"), dataset.get("rows")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        issue("invalid_schema", "", "Dataset requires a manifest and rows array")
        return issues
    version(manifest.get("schema_version"), "manifest.schema_version")
    cutoff = timestamp(manifest.get("as_of"), "manifest.as_of")
    seconds = manifest.get("observation_seconds")
    if type(seconds) is not int or not 0 < seconds <= 315360000:
        issue("invalid_schema", "manifest.observation_seconds", "Invalid observation horizon")
        seconds = None
    for count in ("candidate_count", "eligible_count", "legacy_count", "exported_count"):
        if type(manifest.get(count)) is not int or manifest[count] < 0:
            issue("invalid_manifest", "manifest." + count, "Expected a nonnegative count")
    if manifest.get("exported_count") != len(rows):
        issue("invalid_manifest", "manifest.exported_count", "Count does not match rows")
    sampling = manifest.get("sampling")
    if (
        not isinstance(sampling, dict)
        or sampling.get("algorithm") != "sha256"
        or not isinstance(sampling.get("seed"), str)
        or not sampling["seed"]
        or isinstance(sampling.get("rate"), bool)
        or not isinstance(sampling.get("rate"), (int, float))
        or not 0 < sampling["rate"] <= 1
    ):
        issue("invalid_manifest", "manifest.sampling", "Invalid deterministic sampling contract")
        sampling = None
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        issue("invalid_manifest", "manifest.selection", "Missing source selection bounds")
    else:
        bounds = [
            None
            if selection.get(name) is None
            else timestamp(selection[name], "manifest.selection." + name)
            for name in ("occurred_after", "occurred_before")
        ]
        if bounds[0] is not None and bounds[1] is not None and bounds[0] >= bounds[1]:
            issue("invalid_manifest", "manifest.selection", "Selection bounds are reversed")
    try:
        declared = manifest.get("excluded_fields")
        if not isinstance(declared, list):
            raise ValueError("missing exclusions")
        exclusions = _exclusions(declared)
        if set(exclusions) != set(declared):
            raise ValueError("missing default exclusions")
    except ValueError:
        issue("invalid_manifest", "manifest.excluded_fields", "Invalid privacy exclusions")
        exclusions = DEFAULT_EXCLUDED_FIELDS

    def walk(item: Any, path: tuple[str, ...], row_path: str, *, features: bool = False) -> None:
        if isinstance(item, dict):
            for name, child in item.items():
                child_path = (*path, name)
                at = row_path + "." + ".".join(child_path)
                if any(_path_matches(pattern, child_path) for pattern in exclusions):
                    issue("sensitive_field", at, "Configured sensitive field is present")
                normalized = name.lower().replace("-", "_")
                if features and (normalized in _LEAK_NAMES or normalized.startswith("future_")):
                    issue("label_leakage", at, "Post-decision or label data appears in features")
                walk(
                    child,
                    child_path,
                    row_path,
                    features=features or child_path == ("snapshot", "features"),
                )
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, (*path, str(index)), row_path, features=features)

    def shape(
        item: object,
        names: set[str] | frozenset[str],
        at: tuple[str, ...],
        row_path: str,
    ) -> bool:
        if not isinstance(item, dict):
            issue("invalid_schema", row_path + "." + ".".join(at), "Expected an object")
            return False
        for name in names.difference(item):
            field_path = (*at, name)
            if not any(_path_matches(pattern, field_path) for pattern in exclusions):
                issue(
                    "invalid_schema",
                    row_path + "." + ".".join(field_path),
                    "Required snapshot field is missing",
                )
        for name in item.keys() - names:
            issue(
                "unknown_field",
                row_path + "." + ".".join((*at, name)),
                "Unknown snapshot field requires a schema version change",
            )
        return True

    def feature_times(item: Any, at: str, decision_at: str | None) -> None:
        if isinstance(item, dict):
            for name, child in item.items():
                if (name.endswith("_at") or name == "as_of") and child is not None:
                    instant = timestamp(child, at + "." + name)
                    if instant and decision_at and instant > decision_at:
                        issue(
                            "temporal_leakage",
                            at + "." + name,
                            "Feature provenance is after decision time",
                        )
                feature_times(child, at + "." + name, decision_at)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                feature_times(child, at + "." + str(index), decision_at)

    def signal(item: Any, at: tuple[str, ...], row_path: str, *, embedding: bool) -> None:
        fields = (
            {"name", "query", "state", "similarity", "provenance"}
            if embedding
            else {"state", "value", "provenance"}
        )
        if not shape(item, fields, at, row_path):
            return
        state = item.get("state")
        if state not in {"fresh", "stale", "missing", "unavailable"}:
            issue("invalid_schema", row_path + "." + ".".join(at), "Invalid feature state")
        feature_value = item.get("similarity" if embedding else "value")
        if (
            state == "fresh"
            and ("similarity" if embedding else "value") in item
            and feature_value is None
        ):
            issue(
                "invalid_schema",
                row_path + "." + ".".join(at),
                "Fresh features require an observed value",
            )
        if (
            embedding
            and feature_value is not None
            and (
                isinstance(feature_value, bool)
                or not isinstance(feature_value, (int, float))
                or not -1 <= feature_value <= 1
            )
        ):
            issue("invalid_schema", row_path + "." + ".".join(at), "Invalid similarity")
        if not embedding and feature_value is not None and not isinstance(feature_value, str):
            issue("invalid_schema", row_path + "." + ".".join(at), "Invalid MIME value")
        if state != "fresh" and feature_value is not None:
            issue(
                "invalid_schema",
                row_path + "." + ".".join(at),
                "Non-fresh features cannot contain a value",
            )
        provenance = item.get("provenance")
        if shape(
            provenance,
            {"source", "source_version", "content_sha256", "details"},
            (*at, "provenance"),
            row_path,
        ):
            if (
                ("source" in provenance and not isinstance(provenance["source"], str))
                or type(provenance.get("source_version")) is not int
                or provenance["source_version"] < 1
                or not isinstance(provenance.get("details"), dict)
            ):
                issue(
                    "invalid_schema",
                    row_path + "." + ".".join(at),
                    "Feature provenance requires source, version and details",
                )

    seen: set[str] = set()
    for index, row in enumerate(rows):
        path = f"rows.{index}"
        if not isinstance(row, dict):
            issue("invalid_schema", path, "Row must be an object")
            continue
        version(row.get("schema_version"), path + ".schema_version")
        walk(row, (), path)
        if redact(row) != row:
            issue("sensitive_value", path, "Row contains an unredacted credential pattern")
        structured_reason = None
        if "structured_reason" in row:
            try:
                structured_reason = validate_policy_reason(row["structured_reason"])
            except ValueError:
                issue(
                    "invalid_structured_reason",
                    path + ".structured_reason",
                    "Structured reason must satisfy its complete versioned contract",
                )
        identifier = row.get("decision_id")
        if not isinstance(identifier, str) or not identifier:
            issue("invalid_schema", path + ".decision_id", "Missing decision identity")
        elif identifier in seen:
            issue("duplicate_decision", path + ".decision_id", "Decision occurs more than once")
        else:
            seen.add(identifier)
            if sampling is not None and not _sampled(
                identifier, sampling["seed"], sampling["rate"]
            ):
                issue("invalid_sampling", path, "Decision is outside declared deterministic sample")
        snapshot, outcomes, label = row.get("snapshot"), row.get("outcomes"), row.get("label")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(outcomes, list)
            or not isinstance(label, dict)
        ):
            issue("invalid_schema", path, "Row requires snapshot, outcomes and label")
            continue
        version(snapshot.get("schema_version"), path + ".snapshot.schema_version")
        shape(
            snapshot,
            {
                "schema_version",
                "decision_at",
                "object",
                "features",
                "policy",
                "allowed_tiers",
                "decision",
                "provenance",
                "replay",
            },
            ("snapshot",),
            path,
        )
        shape(
            snapshot.get("object"),
            {"bucket", "key", "size", "tier", "pool_id"},
            ("snapshot", "object"),
            path,
        )
        record = snapshot.get("object")
        if isinstance(record, dict):
            if "size" in record and (type(record["size"]) is not int or record["size"] < 0):
                issue("invalid_schema", path + ".snapshot.object.size", "Invalid object size")
            for field in ("bucket", "key", "tier", "pool_id"):
                if field in record and not (field == "pool_id" and record[field] is None):
                    if not isinstance(record[field], str) or not record[field]:
                        issue(
                            "invalid_schema",
                            path + ".snapshot.object." + field,
                            "Object coordinates must be nonempty text",
                        )
        allowed = snapshot.get("allowed_tiers")
        if not isinstance(allowed, list) or any(
            not isinstance(tier, str) or not tier for tier in allowed
        ):
            issue("invalid_schema", path + ".snapshot.allowed_tiers", "Expected allowed tier names")
        policy = snapshot.get("policy")
        if shape(
            policy,
            {"name", "version", "implementation", "config", "model"},
            ("snapshot", "policy"),
            path,
        ):
            assert isinstance(policy, dict)
            if any(
                not isinstance(policy.get(name), str) or not policy[name]
                for name in ("name", "version", "implementation")
            ) or not isinstance(policy.get("config"), dict):
                issue("invalid_schema", path + ".snapshot.policy", "Invalid versioned policy")
            implementation = policy.get("implementation")
            config_fields = {"size_threshold", "allowed_tiers"}
            if implementation == "llm-threshold":
                config_fields = {"threshold", "allowed_tiers", "provider_allowed_tiers"}
            elif implementation == "content":
                config_fields.update(
                    {
                        "hot_name_patterns",
                        "warm_name_patterns",
                        "cold_name_patterns",
                        "hot_mime_prefixes",
                        "warm_mime_prefixes",
                        "cold_mime_prefixes",
                        "embedding_rules",
                    }
                )
                if isinstance(policy.get("config"), dict) and "pii_rules" in policy["config"]:
                    config_fields.add("pii_rules")
            elif implementation == "unsupported":
                config_fields = set()
            elif implementation != "simple":
                issue(
                    "unsupported_schema", path + ".snapshot.policy", "Unknown policy implementation"
                )
            shape(policy.get("config"), config_fields, ("snapshot", "policy", "config"), path)
            config = policy.get("config")
            if isinstance(config, dict):
                for field, configured in config.items():
                    if field in {"size_threshold", "threshold"} and type(configured) is not int:
                        issue(
                            "invalid_schema",
                            path + ".snapshot.policy.config." + field,
                            "Threshold must be an integer",
                        )
                    elif field not in {"size_threshold", "threshold", "embedding_rules", "pii_rules"} and (
                        not isinstance(configured, list)
                        or any(not isinstance(item, str) for item in configured)
                    ):
                        issue(
                            "invalid_schema",
                            path + ".snapshot.policy.config." + field,
                            "Policy rules must contain text",
                        )
                if "pii_rules" in config:
                    rules = config["pii_rules"]
                    if not isinstance(rules, list) or len(rules) > 100:
                        issue("invalid_schema", path + ".snapshot.policy.config.pii_rules",
                              "PII rules must be a bounded list")
                    else:
                        for offset, rule in enumerate(rules):
                            at = ("snapshot", "policy", "config", "pii_rules", str(offset))
                            if shape(rule, {"finding_type", "destination_tier", "minimum_confidence"},
                                     at, path):
                                try:
                                    PIIPolicyRule.from_mapping({
                                        "finding_type": "EMAIL_ADDRESS",
                                        "destination_tier": "hot",
                                        "minimum_confidence": 0.5,
                                        **rule,
                                    })
                                except ValueError:
                                    issue("invalid_schema", path + "." + ".".join(at),
                                          "Invalid PII policy rule")
            if policy.get("model") is not None:
                shape(
                    policy["model"], {"identity", "version"}, ("snapshot", "policy", "model"), path
                )
        provenance = snapshot.get("provenance")
        if shape(provenance, {"source", "schema_version"}, ("snapshot", "provenance"), path):
            assert isinstance(provenance, dict)
            version(provenance.get("schema_version"), path + ".snapshot.provenance.schema_version")
        replay = snapshot.get("replay")
        if shape(replay, {"supported", "reason"}, ("snapshot", "replay"), path):
            assert isinstance(replay, dict)
            if type(replay.get("supported")) is not bool or replay.get("supported"):
                issue(
                    "invalid_schema",
                    path + ".snapshot.replay",
                    "Privacy-filtered exports cannot claim complete replay inputs",
                )
        start = timestamp(snapshot.get("decision_at"), path + ".snapshot.decision_at")
        if start and cutoff and start > cutoff:
            issue("temporal_leakage", path + ".snapshot", "Decision is after export cutoff")
        features = snapshot.get("features")
        if not isinstance(features, dict):
            issue("invalid_schema", path + ".snapshot.features", "Missing feature snapshot")
        else:
            shape(
                features,
                (_FEATURE_NAMES - _OPTIONAL_FEATURE_NAMES)
                | (_OPTIONAL_FEATURE_NAMES & features.keys()),
                ("snapshot", "features"),
                path,
            )
            version(features.get("schema_version"), path + ".snapshot.features.schema_version")
            signal(features.get("mime"), ("snapshot", "features", "mime"), path, embedding=False)
            embeddings = features.get("embeddings")
            if not isinstance(embeddings, list):
                issue("invalid_schema", path + ".snapshot.features.embeddings", "Expected an array")
            else:
                for offset, embedding in enumerate(embeddings):
                    signal(
                        embedding,
                        ("snapshot", "features", "embeddings", str(offset)),
                        path,
                        embedding=True,
                    )
            feature_times(features, path + ".snapshot.features", start)
            if set(features).difference(_FEATURE_NAMES):
                issue("unknown_feature", path + ".snapshot.features", "Unknown feature fields")
            if "pii" in features:
                pii = features["pii"]
                pii_at = ("snapshot", "features", "pii")
                if shape(pii, {"state", "findings", "provenance"}, pii_at, path):
                    if pii.get("state") not in {"fresh", "stale", "missing", "unavailable"}:
                        issue("invalid_schema", path + ".snapshot.features.pii", "Invalid PII state")
                    findings = pii.get("findings")
                    if not isinstance(findings, list) or len(findings) > 10_000:
                        issue("invalid_schema", path + ".snapshot.features.pii", "Invalid PII findings")
                    else:
                        if pii.get("state") != "fresh" and findings:
                            issue("invalid_schema", path + ".snapshot.features.pii",
                                  "Non-fresh PII features cannot contain findings")
                        for offset, finding in enumerate(findings):
                            finding_at = (*pii_at, "findings", str(offset))
                            if shape(finding, {"type", "confidence", "provenance", "detector", "detector_version"},
                                     finding_at, path):
                                try:
                                    ClassifiedPIIFinding(**{
                                        "type": "EMAIL_ADDRESS", "confidence": 0.5,
                                        "provenance": "rule", "detector": "redacted",
                                        "detector_version": "1.0", **finding,
                                    })
                                except (TypeError, ValueError):
                                    issue("invalid_schema", path + "." + ".".join(finding_at),
                                          "Invalid normalized PII finding")
                    shape(pii.get("provenance"),
                          {"source", "source_version", "content_sha256", "details"},
                          (*pii_at, "provenance"), path)
            if "placement_estimates" in features:
                try:
                    estimates = _placement_estimates_from_dict(features["placement_estimates"])
                    if isinstance(record, dict) and any(
                        field in record and actual != record[field]
                        for field, actual in (
                            ("size", estimates.current.workload.stored_bytes),
                            ("tier", estimates.current.tier),
                            ("pool_id", estimates.current.pool_id),
                        )
                    ):
                        raise ValueError("placement estimates must identify the snapshot object")
                except ValueError as exc:
                    issue(
                        "invalid_schema", path + ".snapshot.features.placement_estimates", str(exc)
                    )
            access = features.get("access")
            if access is not None:
                if not isinstance(access, dict):
                    issue(
                        "invalid_schema", path + ".snapshot.features.access", "Invalid access data"
                    )
                else:
                    version(access.get("schema_version"), path + ".snapshot.features.access")
                    access_as_of = timestamp(
                        access.get("as_of"), path + ".snapshot.features.access"
                    )
                    if start and access_as_of and access_as_of > start:
                        issue(
                            "temporal_leakage",
                            path + ".snapshot.features.access",
                            "Access snapshot contains future observations",
                        )
                    for name in ("last_access_at", "observed_since"):
                        if access.get(name) is not None:
                            time = timestamp(
                                access[name], path + ".snapshot.features.access." + name
                            )
                            if time and access_as_of and time > access_as_of:
                                issue(
                                    "temporal_leakage",
                                    path + ".snapshot.features.access." + name,
                                    "Access evidence is after its snapshot cutoff",
                                )
        decision = snapshot.get("decision")
        shape(
            decision,
            {"action", "destination_tier", "reason", "outcome"},
            ("snapshot", "decision"),
            path,
        )
        if not isinstance(decision, dict) or decision.get("outcome") not in {
            "selected",
            "stayed",
            "rejected",
        }:
            issue("invalid_schema", path + ".snapshot.decision", "Invalid decision outcome")
            continue
        action, destination = decision.get("action"), decision.get("destination_tier")
        if structured_reason is not None:
            if isinstance(policy, dict) and any(
                name in policy and structured_reason["policy"][name] != policy[name]
                for name in ("name", "version", "model")
            ):
                issue(
                    "invalid_structured_reason",
                    path + ".structured_reason.policy",
                    "Reason policy identity differs from its feature snapshot",
                )
            # Snapshot v1 classifies every non-move action as stayed. Reason
            # v1 can explicitly reject an invalid action without rewriting
            # that historical snapshot contract.
            expected_dispositions = (
                {"move"} if decision["outcome"] == "selected"
                else {"rejected"} if decision["outcome"] == "rejected"
                or action not in ("stay", "move")
                else {"stay", "suppressed"}
            )
            if structured_reason["disposition"] not in expected_dispositions:
                issue(
                    "invalid_structured_reason",
                    path + ".structured_reason.disposition",
                    "Reason disposition differs from its recorded decision",
                )
        if (
            not isinstance(action, str)
            or not action
            or (destination is not None and not isinstance(destination, str))
        ):
            issue("invalid_schema", path + ".snapshot.decision", "Invalid action or destination")
        elif (
            isinstance(record, dict)
            and "tier" in record
            and isinstance(allowed, list)
            and "destination_tier" in decision
        ):
            actionable = (
                action == "move"
                and bool(destination)
                and destination != record["tier"]
                and destination in allowed
            )
            expected_outcome = (
                "selected" if actionable else "rejected" if action == "move" else "stayed"
            )
            if decision["outcome"] != expected_outcome:
                issue(
                    "invalid_schema",
                    path + ".snapshot.decision",
                    "Outcome conflicts with action and allowed tiers",
                )
        version(label.get("schema_version"), path + ".label.schema_version")
        if label.get("value") is not None and type(label["value"]) is not int:
            issue("invalid_label", path + ".label.value", "Label value must be integer or null")
        if label.get("name") != "move_succeeded" or not _version(label.get("version")):
            issue("unsupported_label", path + ".label", "Unknown label definition/version")
        window_start = timestamp(label.get("window_start"), path + ".label.window_start")
        window_end = timestamp(label.get("window_end"), path + ".label.window_end")
        label_as_of = timestamp(label.get("as_of"), path + ".label.as_of")
        if start and window_start and start != window_start:
            issue(
                "invalid_window", path + ".label.window_start", "Window must start at decision time"
            )
        if window_end and window_start and window_end <= window_start:
            issue("invalid_window", path + ".label.window_end", "Window must follow decision time")
        if start and seconds and window_end:
            try:
                expected_end = _add_seconds(start, seconds)
            except ValueError:
                expected_end = None
            if expected_end != window_end:
                issue("invalid_window", path + ".label.window_end", "Horizon differs from manifest")
        if label_as_of != cutoff:
            issue("invalid_window", path + ".label.as_of", "Label cutoff differs from manifest")
        valid_outcomes: list[dict[str, Any]] = []
        evidence_ids: set[str] = set()
        for offset, outcome in enumerate(outcomes):
            opath = f"{path}.outcomes.{offset}"
            if not isinstance(outcome, dict):
                issue("invalid_schema", opath, "Outcome must be an object")
                continue
            version(outcome.get("schema_version"), opath + ".schema_version")
            occurred = timestamp(outcome.get("occurred_at"), opath + ".occurred_at")
            recorded = timestamp(outcome.get("recorded_at"), opath + ".recorded_at")
            if (
                (occurred and start and occurred < start)
                or (occurred and window_end and occurred > window_end)
                or (occurred and cutoff and occurred > cutoff)
                or (recorded and cutoff and recorded > cutoff)
                or (occurred and recorded and recorded < occurred)
            ):
                issue("temporal_leakage", opath, "Outcome evidence violates observation bounds")
            event_id = outcome.get("event_id")
            if not isinstance(event_id, str) or not event_id or event_id in evidence_ids:
                issue("invalid_evidence", opath + ".event_id", "Missing or duplicate evidence ID")
            else:
                evidence_ids.add(event_id)
            if outcome.get("event_type") not in _MOVE_TYPES or (
                row.get("move_id") is not None and row["move_id"] != outcome.get("move_id")
            ):
                issue("invalid_evidence", opath, "Outcome does not match the selected move")
            expected_event_outcome = {
                "move.failed": "failed",
                "move.completed": "succeeded",
                "move.retry": "retrying",
                "move.prepared": "started",
                "move.transitioned": "succeeded",
            }.get(str(outcome.get("event_type")))
            if outcome.get("outcome") != expected_event_outcome:
                issue("invalid_evidence", opath, "Event type and outcome disagree")
            sequence = outcome.get("sequence")
            if sequence is not None and (type(sequence) is not int or sequence < 1):
                issue("invalid_evidence", opath, "Invalid move sequence")
            if occurred and (sequence is None or type(sequence) is int):
                valid_outcomes.append({**outcome, "occurred_at": occurred})
        if valid_outcomes != sorted(
            valid_outcomes,
            key=lambda item: (
                item["occurred_at"],
                item.get("sequence") or 0,
                str(item.get("event_id")),
            ),
        ):
            issue("invalid_evidence", path + ".outcomes", "Outcome history is out of order")
        if start and seconds and cutoff:
            try:
                expected = _label(
                    snapshot, valid_outcomes, as_of=cutoff, observation_seconds=seconds
                )
            except (ValueError, KeyError):
                expected = None
            if expected is not None:
                for field in ("status", "value", "evidence_event_id"):
                    if label.get(field) != expected[field]:
                        issue(
                            "invalid_label",
                            path + ".label." + field,
                            "Label disagrees with retained outcome evidence",
                        )
                if type(label.get("value")) is bool:
                    issue(
                        "invalid_label",
                        path + ".label.value",
                        "Label value must be integer or null",
                    )
        if label.get("status") == "observed" and label.get("evidence_event_id") not in evidence_ids:
            issue("missing_evidence", path + ".label", "Observed label requires outcome evidence")
        if (
            require_labels
            and decision["outcome"] == "selected"
            and label.get("status") != "observed"
        ):
            issue(
                "missing_label", path + ".label", "Selected decision has no mature observed label"
            )
    return issues
