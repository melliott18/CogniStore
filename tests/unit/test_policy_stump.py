from __future__ import annotations

import copy
import itertools
import json
import math
import random
import sys
from typing import Any

import pytest

from cognistore.core.policy_stump import fit_stump, predict_stump, validate_stump


def test_stump_learns_interpretable_split_and_round_trips_json() -> None:
    model = fit_stump([[1.0], [2.0], [3.0], [4.0]], [0, 0, 1, 1])
    assert model["feature_index"] == 0
    assert model["threshold"] == 2.5
    assert model["training_counts"] == {"0": 2, "1": 2}
    assert model["left"] == {"counts": {"0": 2, "1": 0}, "success_probability": 0.0}
    assert model["right"] == {"counts": {"0": 0, "1": 2}, "success_probability": 1.0}
    restored = json.loads(json.dumps(model, allow_nan=False))
    validate_stump(restored)
    assert predict_stump(restored, [2.5]) == 0.0
    assert predict_stump(restored, [math.nextafter(2.5, math.inf)]) == 1.0


def test_class_balancing_changes_split_and_uses_training_class_weights() -> None:
    # An unweighted stump prefers isolating the final success at 8.0. Giving
    # each class equal total weight instead selects both observed successes.
    model = fit_stump(
        [[float(i)] for i in range(8)] + [[5.5], [8.0]], [0] * 8 + [1, 1], min_leaf_samples=1
    )
    assert model["threshold"] == 5.25
    assert model["left"]["counts"] == {"0": 6, "1": 0}
    assert model["right"]["counts"] == {"0": 2, "1": 2}
    assert predict_stump(model, [8.0]) == 0.8


@pytest.mark.parametrize("min_leaf_samples", [1, 2, 10])
def test_constant_features_produce_balanced_constant(min_leaf_samples: int) -> None:
    model = fit_stump([[7.0]] * 5, [0, 0, 0, 0, 1], min_leaf_samples=min_leaf_samples)
    assert model["feature_index"] is None
    assert model["threshold"] is None
    assert model["leaf"] == {"counts": {"0": 4, "1": 1}, "success_probability": 0.5}
    assert predict_stump(model, [-1e100]) == 0.5


def test_zero_gain_does_not_create_a_split() -> None:
    model = fit_stump([[0.0], [0.0], [1.0], [1.0]], [0, 1, 0, 1])
    assert "leaf" in model
    assert predict_stump(model, [1.0]) == 0.5


def test_minimum_leaf_size_prevents_isolating_a_single_row() -> None:
    matrix = [[0.0], [1.0], [1.0], [1.0]]
    assert fit_stump(matrix, [1, 0, 0, 0], min_leaf_samples=1)["threshold"] == 0.5
    assert "leaf" in fit_stump(matrix, [1, 0, 0, 0], min_leaf_samples=2)


def test_equal_gain_prefers_lowest_feature_then_lowest_threshold() -> None:
    model = fit_stump([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]], [0, 1, 0], min_leaf_samples=1)
    assert model["feature_index"] == 0
    assert model["threshold"] == 0.5


def test_artifact_is_identical_across_row_permutations() -> None:
    pairs = [([0.0, 1.0], 0), ([1.0, 0.0], 1), ([1.0, 0.0], 0), ([2.0, 2.0], 1)]
    artifacts = {
        json.dumps(
            fit_stump([row for row, _ in order], [label for _, label in order]),
            sort_keys=True,
            allow_nan=False,
        )
        for order in itertools.permutations(pairs)
    }
    assert len(artifacts) == 1


@pytest.mark.parametrize(
    ("lower", "upper"),
    [
        (-sys.float_info.max, sys.float_info.max),
        (sys.float_info.max / 2, sys.float_info.max),
        (-sys.float_info.max, -sys.float_info.max / 2),
        (1.0, math.nextafter(1.0, math.inf)),
        (math.nextafter(1.0, math.inf), math.nextafter(math.nextafter(1.0, math.inf), math.inf)),
        (-math.ulp(0.0), 0.0),
        (0.0, math.ulp(0.0)),
    ],
)
def test_midpoint_is_finite_and_preserves_distinct_values(lower: float, upper: float) -> None:
    model = fit_stump([[lower], [upper]], [0, 1], min_leaf_samples=1)
    assert lower <= model["threshold"] < upper
    assert math.isfinite(model["threshold"])
    assert predict_stump(model, [lower]) == 0.0
    assert predict_stump(model, [upper]) == 1.0
    json.dumps(model, allow_nan=False)


@pytest.mark.parametrize(
    ("matrix", "labels", "minimum"),
    [
        ([], [], 1),
        (None, [0, 1], 1),
        (([1.0], [2.0]), [0, 1], 1),
        ([[], []], [0, 1], 1),
        ([[1.0], [2.0, 3.0]], [0, 1], 1),
        ([[1.0], (2.0,)], [0, 1], 1),
        ([[1.0], [2.0]], [0], 1),
        ([[1.0], [2.0]], [1, 1], 1),
        ([[1.0], [2.0]], [0, 0], 1),
        ([[1.0], [2.0]], [0, 2], 1),
        ([[1.0], [2.0]], [False, True], 1),
        ([[1.0], [2.0]], [0.0, 1.0], 1),
        ([[1.0], [2.0]], (0, 1), 1),
        ([[1.0], [2.0]], [0, 1], 0),
        ([[1.0], [2.0]], [0, 1], -1),
        ([[1.0], [2.0]], [0, 1], True),
        ([[1.0], [2.0]], [0, 1], 1.0),
        ([[float("nan")], [2.0]], [0, 1], 1),
        ([[float("inf")], [2.0]], [0, 1], 1),
        ([[float("-inf")], [2.0]], [0, 1], 1),
        ([[True], [2.0]], [0, 1], 1),
        ([["1.0"], [2.0]], [0, 1], 1),
        ([[10**1000], [2.0]], [0, 1], 1),
    ],
)
def test_fit_rejects_invalid_inputs(matrix: Any, labels: Any, minimum: Any) -> None:
    with pytest.raises(ValueError):
        fit_stump(matrix, labels, min_leaf_samples=minimum)


@pytest.mark.parametrize(
    "vector", [None, [], [1.0, 2.0], (1.0,), [True], ["1"], [math.inf], [math.nan], [10**1000]]
)
def test_prediction_rejects_invalid_vectors(vector: Any) -> None:
    model = fit_stump([[0.0], [0.0]], [0, 1])
    with pytest.raises(ValueError):
        predict_stump(model, vector)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("algorithm", "unknown"),
        ("version", 2),
        ("version", True),
        ("class_weight", "none"),
        ("feature_count", 0),
        ("feature_count", True),
        ("min_leaf_samples", 0),
        ("min_leaf_samples", 3),
        ("feature_index", -1),
        ("feature_index", 1),
        ("feature_index", False),
        ("feature_index", 0.0),
        ("feature_index", None),
        ("threshold", None),
        ("threshold", "0.5"),
        ("threshold", math.nan),
        ("training_counts", {"0": 0, "1": 4}),
        ("training_counts", {"0": True, "1": 2}),
        ("training_counts", {"0": 2, "1": 3}),
        ("training_counts", {"0": 2, "1": 2, "2": 1}),
        ("left", None),
        ("left", {"counts": {"0": 2, "1": 0}, "success_probability": 0.9}),
        ("left", {"counts": {"0": 0, "1": 0}, "success_probability": 0.0}),
        ("left", {"counts": {"0": -1, "1": 3}, "success_probability": 1.0}),
        ("left", {"counts": {"0": 2, "1": 0}, "success_probability": False}),
        ("left", {"counts": {"0": 2, "1": 0}, "success_probability": math.inf}),
        ("unexpected", "value"),
    ],
)
def test_prediction_rejects_tampered_models(key: str, value: Any) -> None:
    model = fit_stump([[1.0], [2.0], [3.0], [4.0]], [0, 0, 1, 1])
    model[key] = value
    with pytest.raises(ValueError):
        predict_stump(model, [2.0])


def test_validation_rejects_missing_fields_and_invalid_constant_shape() -> None:
    for matrix in ([[1.0], [1.0]], [[1.0], [2.0]]):
        model = fit_stump(matrix, [0, 1], min_leaf_samples=1)
        for key in model:
            incomplete = copy.deepcopy(model)
            del incomplete[key]
            with pytest.raises(ValueError):
                validate_stump(incomplete)
    model = fit_stump([[1.0], [1.0]], [0, 1])
    model["threshold"] = 1.0
    with pytest.raises(ValueError):
        validate_stump(model)
    with pytest.raises(ValueError):
        validate_stump(None)  # type: ignore[arg-type]


def test_large_imbalanced_fixture_is_deterministic() -> None:
    rng = random.Random(47)
    pairs = [([rng.random(), rng.random()], int(i % 23 == 0)) for i in range(1000)]
    first = fit_stump([row for row, _ in pairs], [label for _, label in pairs])
    rng.shuffle(pairs)
    assert fit_stump([row for row, _ in pairs], [label for _, label in pairs]) == first
    validate_stump(first)
