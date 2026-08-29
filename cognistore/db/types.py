from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


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
