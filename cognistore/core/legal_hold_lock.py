"""Reentrant shared/exclusive fence for legal hold lifecycle operations."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class LegalHoldFence:
    """Allow concurrent mutations, but exclusively serialize hold changes.

    Waiting lifecycle writers stop new readers from starving them. Existing
    readers may nest so an operation can commit catalog state while a hold is
    waiting. Upgrading a running destructive operation into a hold writer is
    rejected: activating a hold inside its own protected side effects cannot
    satisfy the ordering contract.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    @contextmanager
    def hold(self, *, exclusive: bool = False) -> Iterator[None]:
        identity = threading.get_ident()
        with self._condition:
            if exclusive:
                if self._writer == identity:
                    self._writer_depth += 1
                else:
                    if identity in self._readers:
                        raise RuntimeError("cannot change legal holds during a destructive operation")
                    self._waiting_writers += 1
                    try:
                        while self._writer is not None or self._readers:
                            self._condition.wait()
                        self._writer = identity
                        self._writer_depth = 1
                    finally:
                        self._waiting_writers -= 1
            else:
                while (
                    self._writer is not None and self._writer != identity
                ) or (
                    self._waiting_writers and identity not in self._readers
                    and self._writer != identity
                ):
                    self._condition.wait()
                self._readers[identity] = self._readers.get(identity, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                if exclusive:
                    self._writer_depth -= 1
                    if not self._writer_depth:
                        self._writer = None
                else:
                    count = self._readers[identity] - 1
                    if count:
                        self._readers[identity] = count
                    else:
                        del self._readers[identity]
                self._condition.notify_all()
