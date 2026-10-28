r"""Muted real-Kokoro, real-VLC gate for pausing read-aloud.

The fakes in tests/test_tts_pause.py pin the logic; this pins what LibVLC
actually does, which is where every surprise came from on 2026-10-28: VLC never
reports Ended for a raw stream that was really paused, and freezing the PCM
pacer during a pause made VLC sit silent after resume for about as long as it
had been paused. Volume is zero; nothing is heard.

    $env:RUN_TTS_EVAL="1"
    venv\Scripts\python.exe -m pytest tests/integration/test_tts_pause_live.py -v -s
"""

import os
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_TTS_EVAL"),
    reason="muted real-VLC pause gate; set RUN_TTS_EVAL=1",
)

TEXT = (
    "Pausing a read must hold the voice exactly where it is, and resuming must "
    "carry on from that word without skipping ahead or waiting in silence. "
    "This passage is long enough to pause in the middle and still have plenty left."
)
PAUSE_S = 3.0


def _wait(predicate, timeout, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class _Log:
    def __init__(self, engine):
        from PySide6.QtCore import Qt

        direct = Qt.ConnectionType.DirectConnection
        self._lock = threading.Lock()
        self.events = []

        def record(kind, value=None):
            with self._lock:
                self.events.append((kind, time.monotonic(), value))

        engine.playback_started.connect(lambda: record("playing"), direct)
        engine.speaking_finished.connect(lambda: record("finished"), direct)
        engine.error.connect(lambda v: record("error", v), direct)
        engine.falling_behind.connect(lambda v: record("behind", v), direct)

    def mark(self):
        with self._lock:
            return len(self.events)

    def since(self, mark, kind):
        with self._lock:
            return [e for e in self.events[mark:] if e[0] == kind]


@pytest.fixture(scope="module")
def live():
    from voiceassistant.tts import TTSEngine

    engine = TTSEngine(volume=0.0)
    engine.set_voice("kokoro:am_michael")
    engine.set_speed(1.98)
    assert _wait(lambda: engine._kokoro is not None, 30), "Kokoro did not load"
    log = _Log(engine)
    try:
        yield engine, log
    finally:
        engine.shutdown()


def _read(engine, log, pause_after=None):
    """Speak TEXT; optionally pause `pause_after` s into playback for PAUSE_S.
    Returns (seconds from first audio to finished, media ms frozen during pause)."""
    mark = log.mark()
    engine.speak(TEXT)
    assert _wait(lambda: log.since(mark, "playing"), 20), "never reached playback"
    playing_at = log.since(mark, "playing")[0][1]
    held = None
    if pause_after is not None:
        time.sleep(pause_after)
        assert engine.pause() is True
        time.sleep(0.3)                          # VLC goes quiet within ~50 ms
        before = engine._vlc_player.get_time()
        time.sleep(PAUSE_S - 0.3)
        held = (before, engine._vlc_player.get_time())
        assert engine.is_paused
        assert engine.resume() is True
    assert _wait(lambda: log.since(mark, "finished"), 60), "the read never finished"
    finished_at = log.since(mark, "finished")[0][1]
    assert not log.since(mark, "error"), log.since(mark, "error")
    assert not log.since(mark, "behind"), "a pause was reported as the CPU falling behind"
    assert not engine.is_speaking and not engine.is_paused
    return finished_at - playing_at, held


def test_a_paused_read_holds_resumes_and_finishes_on_time(live):
    """Same text twice: the paused read must take the pause longer, give or
    take the 0.6 s end detector -- shorter would mean skipped words, much
    longer would mean the silent wait after resume."""
    engine, log = live
    baseline, _ = _read(engine, log)
    paused, (before, after) = _read(engine, log, pause_after=1.5)
    print(f"\n  baseline {baseline:.2f}s, paused {paused:.2f}s, "
          f"media time while paused {before} -> {after} ms")
    assert after == before, "audio kept playing while paused"
    extra = paused - baseline
    assert PAUSE_S - 0.3 <= extra <= PAUSE_S + 1.2, (
        f"pausing for {PAUSE_S}s added {extra:.2f}s to the read")


def test_stop_from_a_paused_read_unwinds_and_the_next_read_plays(live):
    engine, log = live
    mark = log.mark()
    engine.speak(TEXT)
    assert _wait(lambda: log.since(mark, "playing"), 20)
    time.sleep(0.5)
    assert engine.pause() is True
    time.sleep(0.5)
    stopped_at = time.monotonic()
    engine.stop()
    assert _wait(lambda: log.since(mark, "finished"), 1.5), "Stop did not unwind a paused read"
    assert time.monotonic() - stopped_at < 1.5
    assert not engine.is_paused
    # The shared player must not be left paused for the next read.
    duration, _ = _read(engine, log)
    assert duration > 1.0
