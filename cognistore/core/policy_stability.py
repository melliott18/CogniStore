"""Numerical entry/exit boundaries shared by the built-in placement policies."""

from __future__ import annotations

import math
from numbers import Real
from typing import Collection


def validate_size_hysteresis_bytes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError("size_hysteresis_bytes must be an integer between 0 and 9223372036854775807")
    return value


def validate_similarity_hysteresis(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("similarity_hysteresis must be a finite non-negative number")
    # Check arbitrarily large integers before float conversion can overflow.
    if isinstance(value, int) and not 0 <= value <= 2:
        raise ValueError("similarity_hysteresis must be between 0 and 2")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError("similarity_hysteresis must be between 0 and 2") from exc
    if not math.isfinite(converted) or not 0 <= converted <= 2:
        raise ValueError("similarity_hysteresis must be between 0 and 2")
    return converted


class NumericalHysteresis:
    """Validated mutable settings for the runner's per-object policy copies."""

    @property
    def size_hysteresis_bytes(self) -> int:
        return self._size_hysteresis_bytes

    @size_hysteresis_bytes.setter
    def size_hysteresis_bytes(self, value: int) -> None:
        self._size_hysteresis_bytes = validate_size_hysteresis_bytes(value)

    @property
    def similarity_hysteresis(self) -> float:
        return self._similarity_hysteresis

    @similarity_hysteresis.setter
    def similarity_hysteresis(self, value: float) -> None:
        self._similarity_hysteresis = validate_similarity_hysteresis(value)


def size_boundary(
    *,
    threshold: int,
    band: int,
    current_tier: str,
    size: int,
    allowed_tiers: Collection[str],
) -> tuple[int, dict[str, object] | None]:
    """Keep hot through T+B and warm until T-B; other tiers use ordinary T.

    Initial placement from another tier has no hot/warm boundary to retain.
    Boundaries deliberately remain unbounded: T-B below zero makes entry into
    hot unreachable for non-negative object sizes.
    """

    effective = threshold + (
        band if current_tier == "hot" else -band if current_tier == "warm" else 0
    )
    if not band:
        return effective, None
    baseline_destination = "hot" if size <= threshold else "warm"
    effective_destination = "hot" if size <= effective else "warm"
    suppressed = (
        baseline_destination != current_tier
        and baseline_destination in allowed_tiers
        and effective_destination == current_tier
    )
    return effective, {
        "checks": [{
            "kind": "size",
            "configured_band": band,
            "baseline_threshold": threshold,
            "effective_threshold": effective,
            "value": size,
            "baseline_destination": baseline_destination,
            "effective_destination": effective_destination,
        }],
        "suppressed": suppressed,
    }
