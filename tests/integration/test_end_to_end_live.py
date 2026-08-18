"""The ONE test that covers the whole dictation chain in a single run.

WHY THIS EXISTS: every bug in the 2026-08-17 reliability audit lived in a seam
that no test crossed. The corpus gate feeds WAVs straight to Whisper (no
hotkey, no recorder, no paste). The hotkey suite uses a fake keyboard. The
paste suite uses synthetic text. Each end was covered; the CHAIN was not, and
that is precisely where dictation was broken for a month.

This test crosses every seam:

    real hotkey handlers
      -> real VoiceRecorder (always-open ring buffer, pre-roll, tail drain)
        -> real Transcriber (the configured Whisper model, on the GPU)
          -> real text cleanup
            -> real Paster (real Win32 focus + real Ctrl+V, off the GUI thread)
              -> a real top-level window, whose contents are read back

The only substitutions are the two things a machine cannot supply: a human
voice (a known-text WAV is fed through `_audio_callback`, the exact function
the sound device calls) and physical keypresses (the real handlers are invoked
directly; OS hook registration is covered by test_hotkey_register.py).

TWO TIERS, because Windows will not grant foreground privilege to a process
launched from a background tool/agent:

  * FOREGROUND AVAILABLE (run from your own interactive terminal): the real
    Paster performs a real Ctrl+V and the target window's text is asserted.
  * FOREGROUND REFUSED (agent/CI): the chain still runs for real, but the final
    Win32 injection is captured rather than performed, and the exact
    (hwnd, text) handed to the paste worker is asserted.

This must NEVER be papered over by patching `winapi.get_foreground_window` to
return the target: that makes the real Paster believe focus is correct and fire
a real Ctrl+V into whatever window is actually in front — precisely the silent
mis-paste that `set_foreground_window`'s verified return was hardened to
prevent. (Tried during development; it pasted into an unrelated window.)

Opt in — it loads the real model, and in the foreground tier it takes over the
active window and sends a real Ctrl+V:

    set RUN_E2E=1 && venv\\Scripts\\python.exe -m pytest \\
        tests/integration/test_end_to_end_live.py -q -s
"""

import os
import sys
import time
import wave

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="loads the real Whisper model and drives the real desktop; "
           "set RUN_E2E=1 to opt in",
)

FIXTURE = os.path.join(_ROOT, "tests", "fixtures", "audio", "normal_sentence.wav")
# Content words that must survive the whole chain. Deliberately not an exact
# string match: the model is user-configurable, and punctuation/casing differ
# between medium and large-v3.
EXPECT_WORDS = ("quarterly", "report", "review")
EXPECT_HEAD = "the quarterly report"


def _load_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _pump(app, seconds):
    """Run the Qt event loop for `seconds`, dispatching queued worker signals."""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def _pump_until(app, predicate, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


class _DummyStream:
    """Stands in for the open PortAudio stream during the injection phase, so
    the real device is not fighting our known audio for the ring buffer."""

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        pytest.skip("needs a real desktop session (QT_QPA_PLATFORM=offscreen set)")
    return QApplication.instance() or QApplication([])


# =========================================================================== #
# 1. The real capture device actually delivers audio.
# =========================================================================== #
def test_real_device_delivers_audio_and_offers_preroll(app):
    from voiceassistant.recorder import SAMPLE_RATE, VoiceRecorder

    rec = VoiceRecorder(max_seconds=30, preroll_ms=300)
    try:
        assert rec.open_stream(), "could not open the real capture device"
        _pump(app, 1.2)
        assert rec.is_alive, "stream opened but was declared dead"
        assert rec._frames_written > 0, "device delivered NO audio"
        buffered_s = rec._frames_written / SAMPLE_RATE
        assert buffered_s > 0.5, f"only {buffered_s:.2f}s buffered in 1.2s"
        # The whole point of the always-open stream: audio from BEFORE a press.
        assert rec._frames_written >= 0.3 * SAMPLE_RATE, "no pre-roll available"

        t0 = time.perf_counter()
        rec.start()
        start_us = (time.perf_counter() - t0) * 1e6
        assert rec.is_recording
        assert start_us < 5000, (
            f"start() took {start_us:.0f}us - a per-recording device open is back"
        )
        print(f"\n  real device: {buffered_s:.2f}s buffered, "
              f"start() = {start_us:.0f}us, overflows = {rec._overflow_count}")
    finally:
        rec.close_stream()


# =========================================================================== #
# 2. Hotkey -> recorder -> Whisper -> cleanup -> paste, end to end.
# =========================================================================== #
def test_hotkey_press_to_pasted_text(app, tmp_path, monkeypatch):
    import voiceassistant.config as cfg
    import voiceassistant.ocr as ocr
    import voiceassistant.winapi as winapi
    from voiceassistant import metrics
    from voiceassistant.window import MainWindow
    from PySide6.QtWidgets import QTextEdit

    # Hermetic: CONFIG_FILE is redirected to tmp below, so this runs against
    # DEFAULTS['whisper_model'] rather than whatever the user has configured.
    # Use the corpus gate (CORPUS_MODEL=...) to validate a specific model.
    monkeypatch.setattr(metrics, "METRICS_PATH", str(tmp_path / "metrics.jsonl"))

    # --- a real, separate top-level window to dictate into ---
    target = QTextEdit()
    target.setWindowTitle("VA end-to-end paste target")
    target.resize(560, 200)
    target.show()
    target.raise_()
    target.activateWindow()
    target.setFocus()
    _pump(app, 0.6)
    target_hwnd = int(target.winId())
    if winapi.get_foreground_window() != target_hwnd:
        winapi.set_foreground_window(target_hwnd)  # the app's own helper
        _pump(app, 0.3)
    real_focus = winapi.get_foreground_window() == target_hwnd

    # --- a real MainWindow; only the tray icon, OS hotkey hooks and the OCR
    #     model are skipped. Transcriber, recorder and cleanup are REAL.
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)

    mw = MainWindow(entry_script="main.py")
    submitted = []
    try:
        mw.hide()
        mw.indicator.hide()
        mw._dictation_active = True

        if not real_focus:
            # Capture what the paste worker WOULD do rather than injecting a real
            # Ctrl+V blind. See the module docstring for why faking focus instead
            # is not acceptable.
            monkeypatch.setattr(
                mw.paster, "submit",
                lambda hwnd, text, done_cb: submitted.append((hwnd, text)),
            )

        _pump_until(app, lambda: mw.transcriber.is_loaded, 300,
                    "the real Whisper model to load")
        print(f"\n  model: {mw.transcriber.model_size} on {mw.transcriber.device}")
        print(f"  tier : {'REAL Ctrl+V' if real_focus else 'capture-only (no foreground)'}")

        # Hand the ring buffer over to our known audio instead of the mic.
        mw.recorder.close_stream()
        mw.recorder._stream = _DummyStream()
        mw.recorder._alive = True

        # ---- press (the real handler; binds the dictation to a window) ----
        mw._hotkey_press_handler()
        assert mw.recorder.is_recording, "real handler did not start recording"
        if real_focus:
            assert mw._pending_target_hwnd == target_hwnd, "bound the wrong window"
        else:
            # Windows refused us the foreground, so the handler captured whatever
            # really is in front. Re-point the record-start -> record-stop
            # hand-off at our target so the assertion is about OUR window.
            mw._pending_target_hwnd = target_hwnd

        # ---- "speak" (the exact call the sound device makes) ----
        audio = _load_wav(FIXTURE)
        for i in range(0, len(audio), 1024):
            block = audio[i:i + 1024].reshape(-1, 1)
            mw.recorder._audio_callback(block, len(block), None, None)

        # ---- release ----
        mw._hotkey_release_handler()
        assert not mw._ptt_active

        _pump_until(app, lambda: not mw.recorder.is_recording, 5,
                    "the tail drain to finish the capture")

        def landed():
            return bool(submitted) or bool(target.toPlainText().strip()) or bool(
                mw.text_output.toPlainText().strip())

        _pump_until(app, landed, 240, "the dictation to come out the far end")
        _pump(app, 1.0)

        if real_focus:
            in_window = target.toPlainText().strip()
            delivered = in_window or mw.text_output.toPlainText().strip()
            where = "real window via Ctrl+V" if in_window else "panel fallback"
        else:
            assert submitted, "nothing reached the paste worker"
            hwnd, delivered = submitted[0]
            assert hwnd == target_hwnd, f"aimed at {hwnd}, not target {target_hwnd}"
            where = "paste worker (hwnd + text captured)"

        print(f"  delivered via {where}: {delivered!r}")
        assert delivered, "the dictation vanished before the far end"
        low = delivered.lower()
        missing = [w for w in EXPECT_WORDS if w not in low]
        assert not missing, (
            f"chain corrupted the dictation: {missing} missing from {delivered!r}"
        )
        # The head of the utterance must survive - that was the 117-137ms bug.
        assert EXPECT_HEAD in low, (
            f"first words clipped: {delivered!r} (pre-roll/head-of-audio regression)"
        )
        # The metrics leg must be primed by the real chain, or --report would
        # under-count exactly the outcomes worth knowing about.
        if real_focus:
            assert [r["outcome"] for r in metrics.load()] == [metrics.OUTCOME_PASTED]
        else:
            # The paste never completed in this tier, so the record is still
            # queued for the paste result rather than written.
            assert len(mw._metrics_awaiting_paste) == 1, (
                "metrics leg not primed by the real chain"
            )
            m = mw._metrics_awaiting_paste[0]
            assert m["chars"] == len(delivered)
            assert m["transcribe_ms"] > 0, "decode latency not measured"
            assert m["audio_s"] > 4.0, f"audio duration wrong: {m}"
            print(f"  metrics primed: {m}")
    finally:
        try:
            mw._force_quit = True
            mw.close()
        except Exception:
            pass
        target.close()
        _pump(app, 0.2)
