"""Generate and verify the checked deterministic OpenAPI v1 document."""

from __future__ import annotations

import argparse
import json
from collections.abc import Hashable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from openapi_spec_validator import validate

from .app import create_app

DEFAULT_CONTRACT = Path("docs/openapi/v1.json")


def generate_document() -> dict[str, Any]:
    document = create_app().openapi()
    validate(cast(Mapping[Hashable, Any], document))
    operation_ids: list[str] = []
    for path_item in document.get("paths", {}).values():
        if not isinstance(path_item, Mapping):
            continue
        for operation in path_item.values():
            if not isinstance(operation, Mapping):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str):
                operation_ids.append(operation_id)
    duplicates = sorted(
        operation_id
        for operation_id in set(operation_ids)
        if operation_ids.count(operation_id) > 1
    )
    if duplicates:
        raise ValueError(f"duplicate OpenAPI operation IDs: {', '.join(duplicates)}")
    return document


def render_document() -> str:
    return json.dumps(
        generate_document(),
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when PATH differs instead of updating it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rendered = render_document()
    if args.check:
        try:
            current = args.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"OpenAPI contract is missing: {args.path}")
            return 1
        if current != rendered:
            print(
                f"OpenAPI contract is stale: {args.path}; regenerate with "
                f"python -m cognistore.api.openapi {args.path}"
            )
            return 1
        print(f"OpenAPI contract is current: {args.path}")
        return 0

    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(rendered, encoding="utf-8")
    print(f"Wrote OpenAPI contract: {args.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
