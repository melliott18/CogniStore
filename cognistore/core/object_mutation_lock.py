"""Fail-fast exclusion for an API object's backend and catalog publication."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class ObjectMutationConflictError(RuntimeError):
    """Another API mutation owns this logical object; retry after it finishes."""


class ObjectMutationFence:
    """Exclude overlapping mutations without retaining idle keys or waiting.

    This local component also prevents session advisory lock reentrance from
    admitting a second mutation on the same PostgreSQL connection.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: set[tuple[str, str]] = set()

    @contextmanager
    def hold(self, bucket: str, key: str) -> Iterator[None]:
        identity = (bucket, key)
        with self._guard:
            if identity in self._active:
                raise ObjectMutationConflictError("An object mutation is already in progress")
            self._active.add(identity)
        try:
            yield
        finally:
            with self._guard:
                self._active.remove(identity)
