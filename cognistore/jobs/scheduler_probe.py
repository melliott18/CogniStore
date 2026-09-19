"""Atomic scheduler cycle status and a dependency-free Kubernetes exec probe."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path


def write_status(path: Path, *, ready: bool) -> None:
    """Publish a completed cycle without exposing a partially written file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump({"completed_at": time.time(), "ready": ready}, output)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_status(path: Path, *, max_age: float, readiness: bool = False) -> bool:
    """A failed publication cycle stays live, but cannot become ready."""
    if not math.isfinite(max_age) or max_age <= 0:
        return False
    try:
        with path.open("rb") as source:
            document = json.loads(source.read(4096))
        completed_at = document["completed_at"]
        ready = document["ready"]
        if (
            isinstance(completed_at, bool)
            or not isinstance(completed_at, (float, int))
            or not math.isfinite(completed_at)
            or not isinstance(ready, bool)
        ):
            return False
        age = time.time() - completed_at
        return 0 <= age <= max_age and (ready or not readiness)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-file", type=Path, required=True)
    parser.add_argument("--max-age", type=float, default=30.0)
    parser.add_argument("--readiness", action="store_true")
    args = parser.parse_args(argv)
    return 0 if check_status(
        args.health_file, max_age=args.max_age, readiness=args.readiness
    ) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
