r"""Muted real-VLC stress gate for local read-aloud lifecycle behavior.

This complements test_tts_eval.py: that suite audits the words in captured PCM;
this one sends real PCM through the actual VLC callback transport at volume zero
and repeatedly exercises completion, Stop, and replace-active behavior.

    $env:RUN_TTS_EVAL="1"
    venv\Scripts\python.exe -m pytest tests/integration/test_tts_stress_live.py -v -s
"""

import os
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_TTS_EVAL"),
    reason="muted real-VLC stress gate; set RUN_TTS_EVAL=1",
)


def _wait(predicate, timeout, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class _SignalLog:
    def __init__(self, engine):
        from PySide6.QtCore import Qt

        direct = Qt.ConnectionType.DirectConnection
        self._lock = threading.Lock()
        self.started = []
        self.finished = []
        self.status = []
        self.errors = []

        def record(bucket, value=None):
            with self._lock:
                bucket.append((time.monotonic(), value))

        engine.speaking_started.connect(lambda: record(self.started), direct)
        engine.speaking_finished.connect(lambda: record(self.finished), direct)
        engine.status.connect(lambda value: record(self.status, value), direct)
        engine.error.connect(lambda value: record(self.errors, value), direct)

    def count_status(self, prefix):
        with self._lock:
            return sum(1 for _when, value in self.status if value.startswith(prefix))


@pytest.fixture(scope="module")
def live_engine():
    from voiceassistant.tts import TTSEngine

    engine = TTSEngine(volume=0.0)
    engine.set_voice("kokoro:am_michael")
    assert _wait(lambda: engine._kokoro is not None, 30), "Kokoro did not load"
    log = _SignalLog(engine)
    try:
        yield engine, log
    finally:
        engine.shutdown()


@pytest.mark.parametrize("speed", [1.0, 1.98, 2.6])
def test_muted_real_vlc_completes_at_supported_speeds(live_engine, speed):
    engine, log = live_engine
    before_finished = len(log.finished)
    before_errors = len(log.errors)
    engine.set_speed(speed)
    engine.speak(
        "The muted transport test preserves every word and completes the local stream."
    )
    assert _wait(lambda: len(log.finished) > before_finished, 25), (
        f"real VLC playback did not finish at {speed:.2f}x; status={log.status[-6:]}"
    )
    assert len(log.errors) == before_errors, log.errors[before_errors:]
    assert not engine.is_speaking


def test_repeated_stop_unwinds_quickly_without_errors(live_engine):
    engine, log = live_engine
    engine.set_speed(1.98)
    text = (
        "This deliberately long selection gives the stress gate enough time to "
        "stop active playback before completion. " * 12
    )
    for iteration in range(8):
        before_finished = len(log.finished)
        before_playing = log.count_status("Playing locally")
        before_errors = len(log.errors)
        engine.speak(text)
        assert _wait(
            lambda: log.count_status("Playing locally") > before_playing,
            15,
        ), f"cycle {iteration + 1} never reached playback"
        stopped_at = time.monotonic()
        engine.stop()
        assert _wait(lambda: len(log.finished) > before_finished, 1.5), (
            f"cycle {iteration + 1} did not unwind after Stop"
        )
        assert time.monotonic() - stopped_at < 1.5
        assert len(log.errors) == before_errors, log.errors[before_errors:]
        assert not engine.is_speaking


def test_new_speak_replaces_active_read_and_new_read_finishes(live_engine):
    engine, log = live_engine
    engine.set_speed(1.98)
    before_finished = len(log.finished)
    before_errors = len(log.errors)
    before_playing = log.count_status("Playing locally")
    before_complete = log.count_status("Speech complete")
    engine.speak(
        "The first read is intentionally long and must be retired cleanly. " * 15
    )
    assert _wait(
        lambda: log.count_status("Playing locally") > before_playing,
        15,
    ), "first read never reached playback"

    engine.speak("The replacement read is the only read that should finish normally.")
    assert _wait(lambda: len(log.finished) >= before_finished + 2, 25), (
        f"replace-active sequence did not retire both jobs; status={log.status[-8:]}"
    )
    assert len(log.errors) == before_errors, log.errors[before_errors:]
    assert log.count_status("Speech complete") == before_complete + 1
    assert not engine.is_speaking
