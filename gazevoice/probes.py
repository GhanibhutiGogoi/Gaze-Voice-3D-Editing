"""Short-lived, shared results for read-only status probes.

A dashboard refresh asks every unit how it's doing, and several units ask the
same question ("where's Blender?"). On a busy Mac each of those commands can
take seconds. So each probe runs at most once per window, concurrent callers
share that run, timeouts are never kept, and Refresh or any finished job
clears everything so the page never shows a stale answer after you act.
"""

from __future__ import annotations

import threading
import time
from typing import Sequence

from .runner import TIMED_OUT, CommandResult, CommandRunner


class ProbeTimedOut(Exception):
    """A status probe didn't answer in time. The unit shows "Still checking…", never a fault,
    because a timeout says nothing about whether the thing works."""

    def __init__(self, argv: Sequence[str]):
        super().__init__(f"{argv[0]} didn't answer in time")
        self.argv = tuple(argv)


class _Pending:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: CommandResult | None = None


class ProbeCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[tuple[str, ...], tuple[float, CommandResult]] = {}
        self._inflight: dict[tuple[str, ...], _Pending] = {}

    def run(
        self,
        runner: CommandRunner,
        argv: Sequence[str],
        *,
        timeout: float = 15.0,
        ttl: float = 30.0,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        result = self._run(runner, tuple(str(a) for a in argv), timeout, ttl)
        if raise_on_timeout and result.returncode == TIMED_OUT:
            raise ProbeTimedOut(result.argv)
        return result

    def _run(self, runner: CommandRunner, key: tuple[str, ...], timeout: float, ttl: float) -> CommandResult:
        with self._lock:
            hit = self._results.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
            pending = self._inflight.get(key)
            owner = pending is None
            if owner:
                pending = self._inflight[key] = _Pending()
        if not owner:
            pending.done.wait(timeout + 5)
            return pending.result if pending.result is not None else runner.run(key, timeout=timeout)
        try:
            result = runner.run(key, timeout=timeout)
            pending.result = result
            if result.returncode != TIMED_OUT:
                with self._lock:
                    self._results[key] = (time.monotonic(), result)
            return result
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            pending.done.set()

    def clear(self) -> None:
        with self._lock:
            self._results.clear()
