"""Offline execution-success baseline over frozen rule-selected placements.

This module never supplies a runtime placement policy. The supervised target is
move execution, not the optimal destination or a counterfactual move outcome.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .audit import canonical_audit_timestamp
from .policy_dataset import validate_policy_dataset
from .policy_stump import fit_stump, predict_stump, validate_stump

FEATURE_NAMES = [
    "log1p_size_bytes",
    "mime_fresh",
    "access_fresh",
    "access_partial",
    "log1p_observed_events",
    "recency_known",
    "log1p_recency_seconds",
]
TARGET = {"name": "move_succeeded", "version": 1}
_DEFAULTS = {
    "min_leaf_samples": 2,
    "min_class_count": 5,
    "max_class_ratio": 20.0,
    "success_threshold": 0.5,
    "promotion_min_balanced_accuracy_gain": 0.05,
}


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def code_version() -> str:
    """Identify installed source too, including edits not represented by HEAD."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted((root / "cognistore").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        revision = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = "unavailable"
    return f"git:{revision};python-source-sha256:{digest.hexdigest()}"


def _config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("training configuration must be an object")
    required = {"schema_version", "train_before", "validation_before"}
    if required - value.keys() or value.keys() - required - _DEFAULTS.keys():
        raise ValueError("training configuration has missing or unknown fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported training configuration version")
    result = {**_DEFAULTS, **value}
    for name in ("train_before", "validation_before"):
        if not isinstance(result[name], str):
            raise ValueError(f"{name} must be a timezone-aware timestamp")
        result[name] = canonical_audit_timestamp(result[name])
    if result["train_before"] >= result["validation_before"]:
        raise ValueError("train_before must precede validation_before")
    for name in ("min_leaf_samples", "min_class_count"):
        if type(result[name]) is not int or result[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name in ("max_class_ratio", "success_threshold", "promotion_min_balanced_accuracy_gain"):
        number = result[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"{name} must be finite")
        try:
            converted = float(number)
        except OverflowError as exc:
            raise ValueError(f"{name} must be finite") from exc
        if not math.isfinite(converted):
            raise ValueError(f"{name} must be finite")
        result[name] = converted
    if result["max_class_ratio"] < 1:
        raise ValueError("max_class_ratio must be at least 1")
    if not 0 < result["success_threshold"] < 1:
        raise ValueError("success_threshold must be strictly between 0 and 1")
    if not 0 <= result["promotion_min_balanced_accuracy_gain"] <= 1:
        raise ValueError("promotion gain must be between 0 and 1")
    return result


def _nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"baseline requires numeric {name}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"baseline requires finite {name}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"baseline requires nonnegative finite {name}")
    return number


def _vector(row: dict[str, Any], *, size_factor: float = 1.0) -> list[float]:
    snapshot = row["snapshot"]
    size = _nonnegative(snapshot["object"].get("size"), "object size")
    features = snapshot["features"]
    access = features.get("access")
    fresh = isinstance(access, dict) and access.get("freshness") == "fresh"
    events = recency = 0.0
    recency_known = partial = False
    if isinstance(access, dict):
        if type(access.get("partial")) is not bool:
            raise ValueError("baseline requires access partial coverage flag")
        partial = access["partial"]
        if fresh:
            events = _nonnegative(access.get("observed_events"), "observed access events")
            recency_known = access.get("recency_seconds") is not None
            if recency_known:
                recency = _nonnegative(access["recency_seconds"], "access recency")
    # No identifiers, policy outputs, destinations, evidence, or labels enter X.
    return [
        math.log1p(_nonnegative(size * size_factor, "perturbed size")),
        float(features["mime"].get("state") == "fresh"),
        float(fresh),
        float(partial),
        math.log1p(events),
        float(recency_known),
        math.log1p(recency),
    ]


def _class_check(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(row["label"]["value"] for row in rows)
    minority, majority = min(counts[0], counts[1]), max(counts[0], counts[1])
    ratio = majority / minority if minority else None
    return {
        "counts": {"0": counts[0], "1": counts[1]},
        "majority_to_minority_ratio": ratio,
        "sufficient": minority >= config["min_class_count"]
        and ratio is not None
        and ratio <= config["max_class_ratio"],
    }


def _window(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "decision_start": min(row["snapshot"]["decision_at"] for row in rows),
        "decision_end": max(row["snapshot"]["decision_at"] for row in rows),
        "label_window_end": max(row["label"]["window_end"] for row in rows),
        "evidence_recorded_end": max(
            outcome["recorded_at"] for row in rows for outcome in row["outcomes"]
        ),
    }


def _prepare(dataset: object, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    issues = validate_policy_dataset(dataset, require_labels=False)
    if issues:
        raise ValueError(
            "invalid policy dataset: " + ", ".join(sorted({i["code"] for i in issues}))
        )
    data = json.loads(_json(dataset))
    # Canonicalize all compared timestamps; lexical comparison of mixed offsets
    # or fractional-second precision is not chronological comparison.
    cutoff = canonical_audit_timestamp(data["manifest"]["as_of"])
    if config["validation_before"] >= cutoff:
        raise ValueError("validation_before must precede the dataset evidence cutoff")
    splits: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    purged: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    identities: dict[tuple[str, str], str] = {}
    for row in sorted(data["rows"], key=lambda item: item["decision_id"]):
        if row["label"]["status"] != "observed":
            excluded[row["label"]["status"]] += 1
            continue
        snapshot = row["snapshot"]
        if snapshot["decision"]["outcome"] != "selected":
            raise ValueError("observed labels must describe selected moves")
        for owner, name in ((snapshot, "decision_at"), (row["label"], "window_end")):
            owner[name] = canonical_audit_timestamp(owner[name])
        for outcome in row["outcomes"]:
            outcome["recorded_at"] = canonical_audit_timestamp(outcome["recorded_at"])
        instant = snapshot["decision_at"]
        split = (
            "train"
            if instant < config["train_before"]
            else ("validation" if instant < config["validation_before"] else "test")
        )
        boundary = config["train_before"] if split == "train" else config["validation_before"]
        if split != "test" and (
            row["label"]["window_end"] >= boundary
            or any(outcome["recorded_at"] >= boundary for outcome in row["outcomes"])
        ):
            purged[split] += 1
            continue
        # A single executed move or evidence event cannot be several independent
        # examples, including when it appears twice in the same partition.
        keys = {("evidence", outcome["event_id"]) for outcome in row["outcomes"]}
        keys.update(
            ("move", outcome["move_id"]) for outcome in row["outcomes"] if outcome.get("move_id")
        )
        if row.get("move_id"):
            keys.add(("move", row["move_id"]))
        for key in keys:
            if key in identities:
                raise ValueError("duplicate move/evidence leakage across retained examples")
            identities[key] = split
        _vector(row)
        splits[split].append(row)
    for name, rows in splits.items():
        rows.sort(key=lambda row: (row["snapshot"]["decision_at"], row["decision_id"]))
        if not rows:
            raise ValueError(f"{name} split has no mature observed examples after temporal purge")
    classes = {name: _class_check(rows, config) for name, rows in splits.items()}
    if not classes["train"]["sufficient"]:
        raise ValueError(
            "training class-imbalance check failed: insufficient classes or excessive ratio"
        )
    checks = {
        "dataset_validation": "passed",
        "feature_allowlist": FEATURE_NAMES,
        "temporal_leakage": "passed",
        "duplicate_move_evidence": "passed",
        "purged_rows": {name: purged[name] for name in splits},
        "excluded_rows": dict(sorted(excluded.items())),
        "class_imbalance": classes,
        "object_overlap": {"status": "unavailable", "reason": "export excludes object identities"},
        "windows": {name: _window(rows) for name, rows in splits.items()},
    }
    return splits, checks


def _code(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("code_version must be nonempty")
    return value


def train_baseline(dataset: object, config: object, *, code_version: str) -> dict[str, Any]:
    """Fit only the early time partition; preserve everything needed to repeat it."""
    normalized = _config(config)
    splits, checks = _prepare(dataset, normalized)
    train = splits["train"]
    stump = fit_stump(
        [_vector(row) for row in train],
        [row["label"]["value"] for row in train],
        min_leaf_samples=normalized["min_leaf_samples"],
    )
    artifact = {
        "schema_version": 1,
        "kind": "offline_move_success_gate",
        "target": TARGET,
        "feature_schema_version": 1,
        "feature_names": FEATURE_NAMES,
        "training_config": normalized,
        "config_sha256": _digest(normalized),
        "dataset_sha256": _digest(dataset),
        "code_version": _code(code_version),
        "checks": checks,
        "stump": stump,
    }
    return json.loads(_json({**artifact, "model_version": "sha256:" + _digest(artifact)}))


def _metrics(rows: list[dict[str, Any]], predictions: list[bool]) -> dict[str, Any]:
    cells = Counter((row["label"]["value"], int(pred)) for row, pred in zip(rows, predictions))
    tn, fp, fn, tp = (cells[0, 0], cells[0, 1], cells[1, 0], cells[1, 1])
    recall = tp / (tp + fn) if tp + fn else None
    specificity = tn / (tn + fp) if tn + fp else None
    selected = [row for row, prediction in zip(rows, predictions) if prediction]
    return {
        "rows": len(rows),
        "accuracy": (tn + tp) / len(rows),
        "balanced_accuracy": (recall + specificity) / 2
        if recall is not None and specificity is not None
        else None,
        "success_precision": tp / (tp + fp) if tp + fp else None,
        "success_recall": recall,
        "confusion_matrix": {
            "true_negative": tn,
            "false_positive": fp,
            "false_negative": fn,
            "true_positive": tp,
        },
        "movement": {
            "proposed_moves": len(selected),
            "suppressed_moves": len(rows) - len(selected),
            "planned_bytes": sum(row["snapshot"]["object"]["size"] for row in selected),
            "cost_unit": "planned_bytes_proxy",
            "actual_cost_usd": None,
        },
        "constraints": {
            "allowed_tier_violations": sum(
                row["snapshot"]["decision"]["destination_tier"]
                not in row["snapshot"]["allowed_tiers"]
                for row in selected
            ),
            "residency_cooldown_locality_violations": None,
        },
        "stability": {
            "move_rate": len(selected) / len(rows),
            "observed_tier_reversals": None,
            "reason": "snapshot v1 lacks object identities and complete stability state",
        },
    }


def _comparison(rows: list[dict[str, Any]], model: dict[str, Any]) -> dict[str, Any]:
    threshold = model["training_config"]["success_threshold"]
    predictions = [predict_stump(model["stump"], _vector(row)) >= threshold for row in rows]
    learned = _metrics(rows, predictions)
    learned["stability"]["size_noise_gate_flip_rate"] = sum(
        (predict_stump(model["stump"], _vector(row, size_factor=factor)) >= threshold) != prediction
        for row, prediction in zip(rows, predictions)
        for factor in (0.95, 1.05)
    ) / (2 * len(rows))
    learned["stability"]["noise_protocol"] = "fixed rule proposal; size x0.95 and x1.05"
    recorded = _metrics(rows, [True] * len(rows))
    recorded["stability"]["size_noise_gate_flip_rate"] = None
    return {
        "learned_gate": learned,
        "recorded_rules": recorded,
        "suppress_all": _metrics(rows, [False] * len(rows)),
    }


def evaluate_baseline(dataset: object, model: object, *, code_version: str) -> dict[str, Any]:
    """Evaluate unchanged train configuration on validation and final time holdout."""
    if not isinstance(model, dict) or not isinstance(model.get("stump"), dict):
        raise ValueError("baseline model must be a versioned JSON artifact")
    validate_stump(model["stump"])
    if model.get("dataset_sha256") != _digest(dataset):
        raise ValueError("model dataset fingerprint does not match this snapshot")
    # Reproduce the training partition to verify the entire artifact, including
    # configuration, provenance, checks, and fitted parameters. Held-out labels
    # never enter the fit; a hash alone would not catch a rehashed altered model.
    expected = train_baseline(
        dataset, model.get("training_config"), code_version=model.get("code_version", "")
    )
    if _json(expected) != _json(model):
        raise ValueError("model artifact is incompatible or not reproducible")
    config = model["training_config"]
    splits, checks = _prepare(dataset, config)
    metrics = {name: _comparison(splits[name], model) for name in ("validation", "test")}
    by_policy = {}
    for name in ("validation", "test"):
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in splits[name]:
            policy = row["snapshot"]["policy"]
            identity = _digest(
                {
                    key: policy.get(key)
                    for key in ("name", "version", "implementation", "config", "model")
                }
            )
            groups.setdefault(identity, []).append(row)
        by_policy[name] = [
            {
                "policy_sha256": identity,
                "name": rows[0]["snapshot"]["policy"]["name"],
                "version": rows[0]["snapshot"]["policy"]["version"],
                "metrics": _comparison(rows, model),
            }
            for identity, rows in sorted(groups.items())
        ]
    gates = {}
    for name, comparison in metrics.items():
        learned, rules = comparison["learned_gate"], comparison["recorded_rules"]
        balanced, rule_balanced = learned["balanced_accuracy"], rules["balanced_accuracy"]
        gates[name] = {
            "class_coverage": checks["class_imbalance"][name]["sufficient"],
            "balanced_accuracy_gain": balanced is not None
            and rule_balanced is not None
            and balanced - rule_balanced >= config["promotion_min_balanced_accuracy_gain"],
            "accuracy_no_regression": learned["accuracy"] >= rules["accuracy"],
            "movement_bytes_no_increase": learned["movement"]["planned_bytes"]
            <= rules["movement"]["planned_bytes"],
            "allowed_tier_violations_zero": learned["constraints"]["allowed_tier_violations"] == 0,
        }
    numeric_pass = all(all(part.values()) for part in gates.values())
    report = {
        "schema_version": 1,
        "model_version": model["model_version"],
        "code_version": _code(code_version),
        "training_code_version": model["code_version"],
        "dataset_sha256": model["dataset_sha256"],
        "config_sha256": model["config_sha256"],
        "training_config": config,
        "target": TARGET,
        "checks": checks,
        "metrics": metrics,
        "by_recorded_policy": by_policy,
        "promotion": {
            "status": "offline_candidate" if numeric_pass else "rejected",
            "gates": gates,
            "production_eligible": False,
            "automatic_promotion": False,
            "required_evidence": [
                "independent placement-quality labels and counterfactual validation",
                "complete residency, cooldown, hysteresis and locality constraint evidence",
                "object-level time-series flapping evaluation and measured movement cost",
                "manual review on a representative untouched dataset",
            ],
            "fallback": "existing configured rule policy through PolicyRunner guardrails",
        },
    }
    return json.loads(_json({**report, "report_version": "sha256:" + _digest(report)}))
