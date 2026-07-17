"""Fault-injection tests — every simulated failure must degrade gracefully.

Phase 3 gate: mic death, corrupt settings, failing worker jobs. None of these
may hang, wedge a state machine, or silently discard user data.
"""

import json
import os
import sys
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voiceassistant.config as config_mod
from voiceassistant.config import Config, DEFAULTS
from voiceassistant.recorder import VoiceRecorder
from voiceassistant.workers import SerialWorker


# ---------------------------------------------------------------------------
# Mic dies mid-recording (USB unplug): stop() must STILL emit recording_stopped
# ---------------------------------------------------------------------------
class _DyingStream:
    """Simulates PortAudio raising when the device vanished."""

    def __init__(self, fail_stop=True, fail_close=True):
        self.fail_stop = fail_stop
        self.fail_close = fail_close
        self.closed = False

    def stop(self):
        if self.fail_stop:
            raise RuntimeError("simulated: device unavailable")

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("simulated: close failed too")


class TestMicDeath:
    def _recorder_with_dying_stream(self, **kw):
        rec = VoiceRecorder()
        rec._is_recording = True
        rec._stream = _DyingStream(**kw)
        return rec

    def test_stop_still_emits_recording_stopped(self):
        rec = self._recorder_with_dying_stream()
        stopped = []
        errors = []
        rec.recording_stopped.connect(lambda audio: stopped.append(audio))
        rec.error.connect(lambda msg: errors.append(msg))

        rec.stop()  # must NOT raise

        assert len(stopped) == 1, "recording_stopped did not emit — dictation wedged"
        assert isinstance(stopped[0], np.ndarray)
        assert errors and "Microphone stop error" in errors[0]
        assert rec._stream is None, "dead stream not released"
        assert rec.is_recording is False

    def test_close_runs_even_when_stop_raises(self):
        rec = self._recorder_with_dying_stream(fail_stop=True, fail_close=False)
        stream = rec._stream
        rec.recording_stopped.connect(lambda audio: None)
        rec.stop()
        assert stream.closed, "stream.close() skipped after stop() raised (leak)"

    def test_partial_audio_still_delivered(self):
        rec = self._recorder_with_dying_stream()
        rec._audio_queue.put(np.ones((1024, 1), dtype="float32") * 0.1)
        got = []
        rec.recording_stopped.connect(lambda audio: got.append(audio))
        rec.stop()
        assert got and len(got[0]) == 1024, "captured audio lost on device death"


# ---------------------------------------------------------------------------
# Max-record-duration safety cap (L5): a forgotten/stuck held hotkey must not
# grow the buffer forever. Callback-level so no real device is needed.
# ---------------------------------------------------------------------------
class TestMaxDurationCap:
    def _armed_recorder(self, max_seconds):
        rec = VoiceRecorder(sample_rate=16000, max_seconds=max_seconds)
        # Arm the counters the way start() does, without opening a device.
        rec._is_recording = True
        rec._overflow_count = 0
        rec._frames_captured = 0
        rec._max_hit = False
        rec._max_frames = int((rec.max_seconds or 0) * rec.sample_rate)
        return rec

    def _block(self, n=1024):
        return np.zeros((n, 1), dtype="float32")

    def test_cap_fires_once_after_threshold(self):
        rec = self._armed_recorder(max_seconds=1.0)  # 16000 frames
        fired = []
        rec.max_duration_reached.connect(lambda: fired.append(True))

        # Feed just under the cap: 15 blocks * 1024 = 15360 < 16000.
        for _ in range(15):
            rec._audio_callback(self._block(), 1024, None, None)
        assert not fired, "cap fired before threshold"

        # Cross it and keep going — must fire exactly once.
        for _ in range(10):
            rec._audio_callback(self._block(), 1024, None, None)
        assert fired == [True], f"cap should fire exactly once, got {len(fired)}"

    def test_no_cap_when_disabled(self):
        rec = self._armed_recorder(max_seconds=0)  # disabled
        fired = []
        rec.max_duration_reached.connect(lambda: fired.append(True))
        for _ in range(200):
            rec._audio_callback(self._block(), 1024, None, None)
        assert not fired, "cap fired though it was disabled"

    def test_capped_audio_is_preserved_not_dropped(self):
        # Everything captured before the cap must still be delivered.
        rec = self._armed_recorder(max_seconds=1.0)
        for _ in range(20):
            rec._audio_callback(self._block(), 1024, None, None)
        delivered = []
        rec.recording_stopped.connect(lambda a: delivered.append(a))
        rec._stream = None  # nothing to close in this synthetic setup
        rec.stop()
        assert delivered and len(delivered[0]) == 20 * 1024, "capped audio lost"


# ---------------------------------------------------------------------------
# Corrupt settings.json: back up, reset, surface — never silently discard
# ---------------------------------------------------------------------------
class TestCorruptSettings:
    def _patch_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "settings.json"))

    def test_corrupt_file_backed_up_and_surfaced(self, tmp_path, monkeypatch):
        self._patch_paths(tmp_path, monkeypatch)
        (tmp_path / "settings.json").write_text("{ definitely not json")

        cfg = Config()

        assert cfg.load_error is not None, "corruption not surfaced"
        backup = tmp_path / "settings.json.corrupt.bak"
        assert backup.exists(), "corrupt file was discarded without a backup"
        assert "not json" in backup.read_text()
        assert cfg["whisper_model"] == DEFAULTS["whisper_model"]

    def test_atomic_save_leaves_no_temp_files(self, tmp_path, monkeypatch):
        self._patch_paths(tmp_path, monkeypatch)
        cfg = Config()
        cfg.set("tts_speed", 1.7)
        files = os.listdir(tmp_path)
        assert "settings.json" in files
        assert not [f for f in files if f.endswith(".tmp")], f"temp litter: {files}"
        on_disk = json.loads((tmp_path / "settings.json").read_text())
        assert on_disk["tts_speed"] == 1.7

    def test_defer_save_and_flush(self, tmp_path, monkeypatch):
        self._patch_paths(tmp_path, monkeypatch)
        cfg = Config()
        cfg.save()  # ensure file exists
        before = (tmp_path / "settings.json").read_text()

        cfg.set("tts_speed", 2.9, defer_save=True)  # slider-tick pattern
        assert (tmp_path / "settings.json").read_text() == before, (
            "defer_save wrote to disk anyway"
        )
        cfg.flush()
        after = json.loads((tmp_path / "settings.json").read_text())
        assert after["tts_speed"] == 2.9


# ---------------------------------------------------------------------------
# SerialWorker: a failing job must never kill the subsystem's worker
# ---------------------------------------------------------------------------
class TestWorkerResilience:
    def test_worker_survives_job_exception(self):
        w = SerialWorker("test")
        w.submit(lambda: 1 / 0)  # boom
        probe = threading.Event()
        w.submit(probe.set)
        assert probe.wait(3), "worker died after a job exception"
        w.shutdown()

    def test_shutdown_is_bounded(self):
        w = SerialWorker("test-shutdown")
        w.submit(lambda: None)
        w.shutdown(timeout=3)
        assert not w._thread.is_alive(), "worker thread did not stop"

    def test_jobs_serialize_in_order(self):
        w = SerialWorker("test-order")
        order = []
        done = threading.Event()
        for i in range(5):
            w.submit(order.append, i)
        w.submit(done.set)
        assert done.wait(3)
        assert order == [0, 1, 2, 3, 4]
        w.shutdown()
