from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from sqlalchemy import LargeBinary, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, UserDefinedType


class PgVector(UserDefinedType[tuple[float, ...]]):
    """A pgvector type, optionally constrained for expression-index casts."""

    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        if dimensions is not None and (
            isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0
        ):
            raise ValueError("vector dimensions must be a positive integer")
        self.dimensions = dimensions

    def get_col_spec(self, **_kw: object) -> str:
        if self.dimensions is None:
            return "VECTOR"
        return f"VECTOR({self.dimensions})"


class PortableVector(TypeDecorator[tuple[float, ...]]):
    """Store a vector as pgvector on PostgreSQL and canonical JSON elsewhere.

    A typmodless PostgreSQL column lets one table hold independently versioned
    embedding spaces with different dimensions.  Space-specific expression
    indexes cast the column to ``vector(n)`` and always include the matching
    space/dimension predicate.  SQLite retains the same values as compact JSON
    text so migrations and persistence tests remain portable.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgVector())
        return dialect.type_descriptor(Text())

    def process_bind_param(
        self,
        value: Sequence[float] | None,
        _dialect: Dialect,
    ) -> str | None:
        if value is None:
            return None
        vector: list[float] = []
        for component in value:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                raise ValueError("vector components must be finite numbers")
            normalized = float(component)
            if not math.isfinite(normalized):
                raise ValueError("vector components must be finite numbers")
            vector.append(normalized)
        if not vector:
            raise ValueError("vectors must contain at least one component")
        return json.dumps(vector, allow_nan=False, separators=(",", ":"))

    def process_result_value(
        self,
        value: str | bytes | Sequence[float] | None,
        _dialect: Dialect,
    ) -> tuple[float, ...] | None:
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).decode("utf-8")
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
            raise ValueError("persisted vector must be a JSON array")
        vector = tuple(float(component) for component in decoded)
        if not vector or any(not math.isfinite(component) for component in vector):
            raise ValueError("persisted vector components must be finite numbers")
        return vector


class NulSafeText(TypeDecorator[str]):
    """Store exact Python strings, including NUL, on every catalog backend.

    PostgreSQL rejects NUL in ``TEXT``. Identity fields therefore use ``BYTEA``
    there and ordinary ``TEXT`` on SQLite so legacy databases remain readable.
    Encoding at the type boundary keeps the domain contract backend-neutral.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(LargeBinary())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | bytes | None:
        if value is None or dialect.name != "postgresql":
            return value
        return value.encode("utf-8", errors="surrogatepass")

    def process_result_value(self, value: str | bytes | None, dialect: Dialect) -> str | None:
        if value is None or isinstance(value, str):
            return value
        return bytes(value).decode("utf-8", errors="surrogatepass")
