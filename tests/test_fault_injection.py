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
import voiceassistant.recorder as rec_mod
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


class _DummyStream:
    """A stream that is alive as far as teardown is concerned."""

    def __init__(self):
        self.closed = False

    def stop(self):
        pass

    def close(self):
        self.closed = True


def _armed(max_seconds=120.0, stream=None, preroll_ms=0):
    """A recorder mid-capture without touching a real device.

    Mirrors what start() sets up: the capture start offset into the always-on
    ring, plus an open stream.
    """
    rec = VoiceRecorder(max_seconds=max_seconds, preroll_ms=preroll_ms)
    rec._stream = stream if stream is not None else _DummyStream()
    rec._alive = True
    rec._is_recording = True
    rec._capture_start = rec._frames_written
    return rec


def _block(n=1024, amp=0.0):
    return np.full((n, 1), amp, dtype="float32")


class TestMicDeath:
    """The mic stream is now opened once and kept open, so 'mic death' is no
    longer a failure of stop() — it is a failure of the LIVE stream. The
    guarantees are the same: never raise out of teardown, never leak the
    handle, never lose captured audio, and never leave the user recording
    into a dead device without telling them."""

    def test_close_stream_never_raises_and_releases_handle(self):
        rec = _armed(stream=_DyingStream())
        rec.close_stream()  # must NOT raise even though stop() AND close() do
        assert rec._stream is None, "dead stream not released"
        assert rec.is_recording is False

    def test_close_runs_even_when_stop_raises(self):
        stream = _DyingStream(fail_stop=True, fail_close=False)
        rec = _armed(stream=stream)
        rec.close_stream()
        assert stream.closed, "stream.close() skipped after stop() raised (leak)"

    def test_captured_audio_is_delivered_from_the_ring(self):
        rec = _armed()
        for _ in range(3):
            rec._audio_callback(_block(amp=0.1), 1024, None, None)
        got = []
        rec.recording_stopped.connect(lambda audio: got.append(audio))
        rec._finish_capture()  # the tail timer's slot, driven directly
        assert got and len(got[0]) == 3 * 1024, "captured audio lost"
        assert float(np.max(np.abs(got[0]))) > 0.05, "delivered audio is silent"

    def test_stalled_stream_is_surfaced_not_silently_recorded(self, monkeypatch):
        """A stream that stops delivering is the WORST failure mode: recording
        silence looks exactly like success until the empty transcript. The
        watchdog must notice, tell the user, and end the capture."""
        rec = _armed()
        states, errors = [], []
        rec.stream_state.connect(lambda ok, msg: states.append(ok))
        rec.error.connect(lambda m: errors.append(m))
        # Recovery must not reach for a real device in tests.
        monkeypatch.setattr(VoiceRecorder, "open_stream", lambda self: False)

        ticks = int(rec_mod._STALL_SECONDS / (rec_mod._TICK_MS / 1000.0)) + 2
        for _ in range(ticks):
            rec._on_tick()  # no frames ever arrive

        assert False in states, "dead mic never surfaced to the UI"
        assert errors, "user not told the mic dropped out mid-recording"
        assert rec.is_recording is False, "left recording into a dead stream"

    def test_unavailable_device_is_retried_slowly_and_reported_once(self, monkeypatch):
        """With no usable mic the tick fires 20x/second. Retrying the open on
        every tick would spin PortAudio and write the same error line to
        debug.log 20x/second, so retries are throttled and the outage is
        reported once — not once per attempt."""
        attempts = []

        def boom(*a, **k):
            attempts.append(1)
            raise RuntimeError("simulated: no capture device")

        monkeypatch.setattr(rec_mod.sd, "InputStream", boom)
        rec = VoiceRecorder()
        states = []
        rec.stream_state.connect(lambda ok, msg: states.append(ok))

        assert rec.open_stream() is False
        for _ in range(rec_mod._REOPEN_COOLDOWN_TICKS * 2 + 4):
            rec._on_tick()

        assert len(attempts) <= 4, f"hot retry loop: {len(attempts)} opens in ~4s"
        assert attempts, "never retried at all"
        assert states.count(False) == 1, (
            f"outage reported {states.count(False)}x — should be once per outage"
        )

    def test_healthy_stream_is_not_declared_dead(self):
        """The watchdog must not misfire on a working mic."""
        rec = _armed()
        states = []
        rec.stream_state.connect(lambda ok, msg: states.append(ok))
        ticks = int(rec_mod._STALL_SECONDS / (rec_mod._TICK_MS / 1000.0)) + 5
        for _ in range(ticks):
            rec._audio_callback(_block(amp=0.1), 1024, None, None)
            rec._on_tick()
        assert False not in states, "healthy mic wrongly declared dead"
        assert rec.is_recording is True


# ---------------------------------------------------------------------------
# Max-record-duration safety cap (L5): a forgotten/stuck held hotkey must not
# grow the buffer forever. Callback-level so no real device is needed.
# ---------------------------------------------------------------------------
class TestMaxDurationCap:
    """The cap is now evaluated on the GUI-thread tick rather than inside the
    audio callback (the callback must stay minimal — emitting signals at audio
    rate contributed to the logged input overflows)."""

    def test_cap_fires_once_after_threshold(self):
        rec = _armed(max_seconds=1.0)  # 16000 frames
        fired = []
        rec.max_duration_reached.connect(lambda: fired.append(True))

        # Feed just under the cap: 15 blocks * 1024 = 15360 < 16000.
        for _ in range(15):
            rec._audio_callback(_block(), 1024, None, None)
        rec._on_tick()
        assert not fired, "cap fired before threshold"

        # Cross it and keep ticking — must fire exactly once.
        for _ in range(10):
            rec._audio_callback(_block(), 1024, None, None)
        for _ in range(5):
            rec._on_tick()
        assert fired == [True], f"cap should fire exactly once, got {len(fired)}"

    def test_no_cap_when_disabled(self):
        rec = _armed(max_seconds=0)  # disabled
        fired = []
        rec.max_duration_reached.connect(lambda: fired.append(True))
        for _ in range(200):
            rec._audio_callback(_block(), 1024, None, None)
            rec._on_tick()
        assert not fired, "cap fired though it was disabled"

    def test_capped_audio_is_preserved_not_dropped(self):
        # Everything captured before the cap must still be delivered.
        rec = _armed(max_seconds=1.0)
        for _ in range(20):
            rec._audio_callback(_block(amp=0.2), 1024, None, None)
        delivered = []
        rec.recording_stopped.connect(lambda a: delivered.append(a))
        rec._finish_capture()
        assert delivered and len(delivered[0]) == 20 * 1024, "capped audio lost"

    def test_ring_never_returns_interleaved_garbage_on_overrun(self):
        """A capture longer than the ring must degrade to 'most recent audio',
        never to a wrapped/interleaved buffer that would transcribe as noise."""
        rec = _armed(max_seconds=1.0)  # ring = 1s + headroom
        size = len(rec._ring)
        for _ in range(int(size / 1024) + 40):
            rec._audio_callback(_block(amp=0.3), 1024, None, None)
        got = []
        rec.recording_stopped.connect(lambda a: delivered_append(got, a))
        rec._finish_capture()
        assert got, "no audio delivered on overrun"
        assert len(got[0]) <= size, "returned more audio than the ring holds"
        assert np.allclose(got[0], 0.3), "overrun produced discontinuous audio"


def delivered_append(bucket, audio):
    bucket.append(audio)


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


# ---------------------------------------------------------------------------
# Model loading: cache-first (no network on launch), and a LOUD device fallback
# ---------------------------------------------------------------------------
class _FakeWhisper:
    """Records how WhisperModel was constructed; can fail on demand."""

    def __init__(self, calls, fail_cuda=False, fail_local=False):
        self.calls = calls
        self.fail_cuda = fail_cuda
        self.fail_local = fail_local

    def __call__(self, size, device=None, compute_type=None, local_files_only=False,
                 **kw):
        self.calls.append(
            {"size": size, "device": device, "compute": compute_type,
             "local_only": bool(local_files_only)}
        )
        if self.fail_local and local_files_only:
            from huggingface_hub.errors import LocalEntryNotFoundError

            raise LocalEntryNotFoundError("simulated: nothing cached")
        if self.fail_cuda and device == "cuda":
            raise RuntimeError("simulated: cudnn missing")
        return object()


class TestModelLoad:
    def _patch(self, monkeypatch, **kw):
        import faster_whisper

        calls = []
        monkeypatch.setattr(faster_whisper, "WhisperModel",
                            _FakeWhisper(calls, **kw))
        return calls

    def _transcriber(self):
        from voiceassistant.transcriber import Transcriber

        t = Transcriber(model_size="large-v3", device="cuda", compute_type="float16")
        monkey = {"nvidia": False}
        t._add_nvidia_dll_dirs = lambda: monkey.__setitem__("nvidia", True)
        return t

    def test_cached_model_loads_without_touching_the_network(self, monkeypatch):
        """faster-whisper otherwise revalidates against huggingface.co on EVERY
        launch: measured 176.3s vs 7.0s for an already-cached large-v3, i.e. ~3
        minutes after each boot where the hotkey only says 'still loading'."""
        calls = self._patch(monkeypatch)
        t = self._transcriber()
        ready = []
        t.model_ready.connect(lambda: ready.append(True))
        t._load_job()
        assert ready == [True]
        assert len(calls) == 1, f"expected ONE load attempt, got {calls}"
        assert calls[0]["local_only"] is True, "did not prefer the local cache"

    def test_cache_miss_falls_back_to_downloading_once(self, monkeypatch):
        """A genuinely uncached model must still install on first run."""
        calls = self._patch(monkeypatch, fail_local=True)
        t = self._transcriber()
        msgs, ready = [], []
        t.model_loading.connect(msgs.append)
        t.model_ready.connect(lambda: ready.append(True))
        t._load_job()
        assert ready == [True], "first-run download path did not complete"
        assert [c["local_only"] for c in calls][:2] == [True, False], (
            f"expected cache-first then download, got {calls}"
        )
        assert any("Download" in m for m in msgs), "user not told a download started"

    def test_cuda_failure_is_not_mistaken_for_a_cache_miss(self, monkeypatch):
        """A cuDNN/CUDA error must NOT trigger a pointless multi-GB download —
        and must not leave us pinned to CPU after one."""
        calls = self._patch(monkeypatch, fail_cuda=True)
        t = self._transcriber()
        t._load_job()
        assert all(c["local_only"] for c in calls), (
            f"a CUDA failure triggered a network download: {calls}"
        )
        assert [c["device"] for c in calls] == ["cuda", "cpu"], calls

    def test_cpu_fallback_is_loud(self, monkeypatch):
        """A silent CPU session is 10-20x slower and reads as 'dictation got
        slow' with no explanation."""
        self._patch(monkeypatch, fail_cuda=True)
        t = self._transcriber()
        degraded, ready = [], []
        t.degraded.connect(degraded.append)
        t.model_ready.connect(lambda: ready.append(True))
        t._load_job()
        assert ready == [True]
        assert t.device == "cpu" and t.compute_type == "int8"
        assert degraded, "CPU fallback was silent"
        assert "CPU" in degraded[0]

    def test_no_degraded_signal_when_the_gpu_works(self, monkeypatch):
        self._patch(monkeypatch)
        t = self._transcriber()
        degraded = []
        t.degraded.connect(degraded.append)
        t._load_job()
        assert degraded == [], "spurious degraded warning on a healthy GPU load"
