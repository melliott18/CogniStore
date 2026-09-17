"""Suppress sensitive diagnostics while provider or credentialed SDK code runs."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

_ACTIVE: ContextVar[bool] = ContextVar("cognistore_secret_operation", default=False)
_HANDLER_LOCK = Lock()


def _install_guard() -> None:
    with _HANDLER_LOCK:
        previous = logging.Logger.handle
        if getattr(previous, "_cognistore_secret_diagnostics", False):
            return

        def handle(logger: logging.Logger, record: logging.LogRecord) -> None:
            # Wire logs and third-party exceptions may contain bootstrap tokens
            # or plaintext not yet registered for value-based redaction. Stop
            # before any filters/formatters/handlers inspect or serialize them.
            # Unlike a record factory, this also covers fields supplied in extra.
            if not _ACTIVE.get():
                previous(logger, record)

        setattr(handle, "_cognistore_secret_diagnostics", True)
        setattr(logging.Logger, "handle", handle)


@contextmanager
def secret_operation() -> Iterator[None]:
    """Silence logs only in this context, including lazily added SDK handlers.

    The guard stays installed and delegates unchanged outside this scope.
    Context variables keep unrelated threads and nested operations independent.
    """
    _install_guard()
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
