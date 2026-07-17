"""SerialWorker — the app-wide threading law, in one class.

LAW: every subsystem owns exactly ONE SerialWorker; no ad-hoc
threading.Thread anywhere in the app. Jobs on one worker are strictly
serialized (no overlap races within a subsystem), exceptions in a job are
logged and never kill the worker, and shutdown is bounded.
"""

import queue
import threading

from . import applog


class SerialWorker:
    """One owned worker thread draining one job queue."""

    _STOP = object()

    def __init__(self, name):
        self.name = name
        self._q = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"worker-{name}", daemon=True
        )
        self._thread.start()

    def submit(self, fn, *args, **kwargs):
        """Queue fn(*args, **kwargs) to run on this worker."""
        self._q.put((fn, args, kwargs))

    def pending(self):
        """Approximate count of jobs waiting (excludes the running one)."""
        return self._q.qsize()

    def shutdown(self, timeout=2.0):
        """Stop after the current job; bounded wait."""
        self._q.put(self._STOP)
        self._thread.join(timeout)

    def _run(self):
        while True:
            item = self._q.get()
            if item is self._STOP:
                return
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception:
                # A failed job must never kill the subsystem's worker.
                applog.exception(f"worker '{self.name}' job failed")
