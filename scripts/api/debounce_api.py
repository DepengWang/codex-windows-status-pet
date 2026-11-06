"""Trailing-edge debouncer for toolkit timers (Tk ``after`` / ``after_cancel``)."""

from __future__ import annotations


class Debouncer:
    """Run callback once, delay_ms after the last trigger().

    after(delay_ms, fn) -> job id and cancel(job id) are injected, so the class
    has no Tk dependency and can be tested with a fake clock. Every call must
    happen on the same (UI) thread.
    """

    def __init__(self, after, cancel, delay_ms, callback):
        self._after = after
        self._cancel = cancel
        self._delay_ms = delay_ms
        self._callback = callback
        self._job = None

    @property
    def pending(self):
        return self._job is not None

    def trigger(self):
        self.cancel()
        self._job = self._after(self._delay_ms, self._fire)

    def cancel(self):
        job, self._job = self._job, None
        if job is not None:
            try:
                self._cancel(job)
            except Exception:
                pass

    def _fire(self):
        self._job = None
        self._callback()
