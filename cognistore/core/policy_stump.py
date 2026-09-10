"""Deterministic, class-balanced binary decision stumps for offline experiments.

Each class has equal total training weight. Splits minimize weighted Gini
impurity, and predictions are the weighted success fraction in a leaf. These
scores describe a balanced training population, not calibrated outcome odds.
The artifact contains only versioned JSON values and raw aggregate counts.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, cast

STUMP_ALGORITHM = "balanced_decision_stump"
STUMP_VERSION = 1
_COMMON_KEYS = {
    "algorithm",
    "version",
    "class_weight",
    "feature_count",
    "min_leaf_samples",
    "training_counts",
    "feature_index",
    "threshold",
}


def _number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("features and thresholds must be finite numbers")
    try:
        result = float(cast("int | float", value))
    except (OverflowError, ValueError) as exc:
        raise ValueError("features and thresholds must be finite numbers") from exc
    if not math.isfinite(result):
        raise ValueError("features and thresholds must be finite numbers")
    return result


def _counts(value: object, *, both_classes: bool = False) -> tuple[int, int]:
    if not isinstance(value, dict) or set(value) != {"0", "1"}:
        raise ValueError("class counts must contain exactly classes 0 and 1")
    counts = (value["0"], value["1"])
    minimum = 1 if both_classes else 0
    if any(type(count) is not int or count < minimum for count in counts) or not sum(counts):
        raise ValueError("class counts must be nonnegative integers with nonempty support")
    return counts


def _probability(negative: int, positive: int, total_negative: int, total_positive: int) -> float:
    # Multiplying through by the common denominator avoids rounding class weights.
    weighted_negative = negative * total_positive
    weighted_positive = positive * total_negative
    return weighted_positive / (weighted_negative + weighted_positive)


def _leaf(negative: int, positive: int, total_negative: int, total_positive: int) -> dict[str, Any]:
    return {
        "counts": {"0": negative, "1": positive},
        "success_probability": _probability(negative, positive, total_negative, total_positive),
    }


def _impurity(negative: int, positive: int, total_negative: int, total_positive: int) -> Fraction:
    weighted_negative = negative * total_positive
    weighted_positive = positive * total_negative
    return Fraction(
        2 * weighted_negative * weighted_positive,
        weighted_negative + weighted_positive,
    )


def _midpoint(lower: float, upper: float) -> float:
    # Halving first avoids overflow for finite values at opposite extremes.
    # Adjacent floats can round upward; the lower endpoint preserves the split.
    midpoint = lower / 2.0 + upper / 2.0
    return midpoint if lower <= midpoint < upper else lower


def fit_stump(
    matrix: list[list[float]], labels: list[int], *, min_leaf_samples: int = 2
) -> dict[str, Any]:
    """Fit one split, or a constant leaf when no permitted split improves Gini.

    Training requires a nonempty, finite numeric matrix and both binary classes.
    Minimum leaf size uses raw row counts. Equal gains prefer the lower feature
    index, then the lower threshold; row order never changes the artifact.
    """
    if type(min_leaf_samples) is not int or min_leaf_samples < 1:
        raise ValueError("min_leaf_samples must be a positive integer")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("matrix must be a nonempty list of feature rows")
    if not isinstance(labels, list) or len(labels) != len(matrix):
        raise ValueError("labels must contain one binary integer per feature row")
    if any(type(label) is not int or label not in (0, 1) for label in labels):
        raise ValueError("labels must contain only binary integers 0 and 1")
    if set(labels) != {0, 1}:
        raise ValueError("training requires both binary classes")
    if not isinstance(matrix[0], list) or not matrix[0]:
        raise ValueError("feature rows must be nonempty lists of equal width")
    feature_count = len(matrix[0])
    rows = []
    for row in matrix:
        if not isinstance(row, list) or len(row) != feature_count:
            raise ValueError("feature rows must be nonempty lists of equal width")
        rows.append([_number(value) for value in row])

    total_positive = sum(labels)
    total_negative = len(labels) - total_positive
    model: dict[str, Any] = {
        "algorithm": STUMP_ALGORITHM,
        "version": STUMP_VERSION,
        "class_weight": "balanced",
        "feature_count": feature_count,
        "min_leaf_samples": min_leaf_samples,
        "training_counts": {"0": total_negative, "1": total_positive},
        "feature_index": None,
        "threshold": None,
        "leaf": _leaf(total_negative, total_positive, total_negative, total_positive),
    }
    best_impurity = Fraction(total_negative * total_positive)
    best_split: tuple[int, float, int, int] | None = None
    for feature_index in range(feature_count):
        ordered = sorted((row[feature_index], label) for row, label in zip(rows, labels))
        left_negative = left_positive = 0
        for index, (value, label) in enumerate(ordered[:-1]):
            left_positive += label
            left_negative += 1 - label
            if (
                value == ordered[index + 1][0]
                or index + 1 < min_leaf_samples
                or len(rows) - index - 1 < min_leaf_samples
            ):
                continue
            score = _impurity(left_negative, left_positive, total_negative, total_positive)
            score += _impurity(
                total_negative - left_negative,
                total_positive - left_positive,
                total_negative,
                total_positive,
            )
            # Exact rational comparison makes true ties independent of rounding.
            if score < best_impurity:
                best_impurity = score
                best_split = (
                    feature_index,
                    _midpoint(value, ordered[index + 1][0]),
                    left_negative,
                    left_positive,
                )
    if best_split is not None:
        feature_index, threshold, left_negative, left_positive = best_split
        del model["leaf"]
        model.update(
            feature_index=feature_index,
            threshold=threshold,
            left=_leaf(left_negative, left_positive, total_negative, total_positive),
            right=_leaf(
                total_negative - left_negative,
                total_positive - left_positive,
                total_negative,
                total_positive,
            ),
        )
    return model


def validate_stump(model: dict[str, Any]) -> None:
    """Reject malformed or inconsistent JSON artifacts before using their scores."""
    if not isinstance(model, dict):
        raise ValueError("stump model must be a JSON object")
    if (
        model.get("algorithm") != STUMP_ALGORITHM
        or type(model.get("version")) is not int
        or model["version"] != STUMP_VERSION
        or model.get("class_weight") != "balanced"
    ):
        raise ValueError("unsupported stump algorithm, version, or class weighting")
    for key in ("feature_count", "min_leaf_samples"):
        if type(model.get(key)) is not int or model[key] < 1:
            raise ValueError(f"stump {key} must be a positive integer")
    total_negative, total_positive = _counts(model.get("training_counts"), both_classes=True)
    feature_index = model.get("feature_index")
    names: tuple[str, ...]
    if feature_index is None:
        if set(model) != _COMMON_KEYS | {"leaf"} or model["threshold"] is not None:
            raise ValueError("constant stump must have a leaf and null split fields")
        names = ("leaf",)
    else:
        if (
            set(model) != _COMMON_KEYS | {"left", "right"}
            or type(feature_index) is not int
            or not 0 <= feature_index < model["feature_count"]
        ):
            raise ValueError("split stump must have a valid feature index and two leaves")
        _number(model["threshold"])
        names = ("left", "right")
    leaf_negative = leaf_positive = 0
    for name in names:
        leaf = model[name]
        if not isinstance(leaf, dict) or set(leaf) != {"counts", "success_probability"}:
            raise ValueError("stump leaves must contain counts and success_probability")
        negative, positive = _counts(leaf["counts"])
        if feature_index is not None and negative + positive < model["min_leaf_samples"]:
            raise ValueError("stump leaf has fewer than min_leaf_samples rows")
        probability = _number(leaf["success_probability"])
        if probability != _probability(negative, positive, total_negative, total_positive):
            raise ValueError("stump probability does not match class-balanced leaf counts")
        leaf_negative += negative
        leaf_positive += positive
    if (leaf_negative, leaf_positive) != (total_negative, total_positive):
        raise ValueError("stump leaf counts do not match training counts")


def predict_stump(model: dict[str, Any], vector: list[float]) -> float:
    """Predict a class-balanced success score from a validated JSON stump."""
    validate_stump(model)
    if not isinstance(vector, list) or len(vector) != model["feature_count"]:
        raise ValueError("prediction vector must match the stump feature count")
    values = [_number(value) for value in vector]
    feature_index = model["feature_index"]
    if feature_index is None:
        return float(model["leaf"]["success_probability"])
    side = "left" if values[feature_index] <= model["threshold"] else "right"
    return float(model[side]["success_probability"])
