"""Count-down gate so the realtime server can bind before models finish loading."""

from __future__ import annotations

from threading import Event, Lock


class PipelineReady:
    """Ready after ``count`` successful ``mark(True)`` calls.

    A single ``mark(False)`` permanently fails the gate: ``/health`` stays
    unready so the native panel does not open a microphone against a broken
    pipeline.
    """

    def __init__(self, count: int = 0) -> None:
        self._lock = Lock()
        self._remaining = max(0, int(count))
        self._ok = True
        self._event = Event()
        if self._remaining == 0:
            self._event.set()

    def mark(self, ok: bool = True) -> None:
        with self._lock:
            if not ok:
                self._ok = False
            if self._remaining > 0:
                self._remaining -= 1
            if self._remaining == 0:
                self._event.set()

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._remaining == 0 and self._ok

    @property
    def failed(self) -> bool:
        with self._lock:
            return not self._ok
