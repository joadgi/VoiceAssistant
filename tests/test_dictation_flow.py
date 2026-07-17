"""End-to-end orchestration test for the DICTATION handler chain.

Unit tests cover the *pieces* (text cleanup, the record gate math, the paster,
the transcriber). What NOTHING else exercises is the WIRING in
`voiceassistant/window.py` that stitches them together:

    _hotkey_press_handler -> _on_record_from_hotkey (captures _pending_target_hwnd)
      -> recorder.start -> [recording] -> _hotkey_release_handler -> _on_stop_record
      -> recorder.stop -> recording_stopped(audio) -> _on_recording_stopped
      (consume pending hwnd + record gate) -> transcriber.transcribe(audio, context=hwnd)
      -> transcription_ready(TranscriptionResult) -> _on_transcription_ready
      (dedupe / clean / hallucination backstop / route) -> paster.submit -> _on_paste_done

This drives a REAL MainWindow through those actual signals/slots, offscreen,
with the two heavy engines replaced by in-process fakes so the whole chain runs
synchronously with NO model, GPU, mic, focus theft, keystrokes, or clipboard
I/O. All the app's own slots, guards and routing logic run unmodified - only
Whisper (transcribe) and the Win32 paste worker (submit) are stubbed, and both
are stubbed at exactly the seam the real subsystems expose.

Safe to run always: offscreen Qt + fully faked side effects + tiny synthetic
audio. Skips cleanly if PySide6 can't start offscreen.
"""

import os
import sys
import tempfile

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from voiceassistant.transcriber import TranscriptionResult  # noqa: E402


# --------------------------------------------------------------------------- #
# Audio buffers (content is irrelevant - transcription is faked; only length
# and peak matter, because that is what the record gate reads).
# --------------------------------------------------------------------------- #
SR = 16000


def _sine(seconds, amp, sr=SR):
    n = int(seconds * sr)
    t = np.arange(n, dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


LOUD = _sine(1.0, 0.5)         # 1.0s, peak ~0.5   -> passes the record gate
TOO_SHORT = _sine(0.1, 0.5)    # 0.1s < 0.2s min   -> dropped (duration branch)
TOO_QUIET = _sine(1.0, 0.001)  # peak 0.001 < 0.008 -> dropped (peak branch)


# --------------------------------------------------------------------------- #
# Engine fakes - installed at the exact seams the real subsystems expose.
# --------------------------------------------------------------------------- #
class _FakeTranscribe:
    """Stands in for `Transcriber.transcribe`: no Whisper, no worker thread.

    Records each call's (n_samples, context) and synchronously emits
    `transcription_ready` with scripted text - exactly what the real worker
    does when a job finishes, including carrying `context` (the target HWND)
    straight through onto the result.
    """

    def __init__(self, transcriber):
        self._tr = transcriber
        self.calls = []            # [(n_samples, context), ...]
        self.text = "hello world"
        self.duration_s = 1.0
        self.retried = False
        self.no_speech = None      # None -> derive from empty text
        self.force_job_id = None   # None -> monotonic, like the real transcriber
        self._seq = 0

    def transcribe(self, audio, context=None):
        self.calls.append((len(audio), context))
        if self.force_job_id is None:
            self._seq += 1
            job_id = self._seq
        else:
            job_id = self.force_job_id
        no_speech = (not self.text) if self.no_speech is None else self.no_speech
        self._tr.transcription_ready.emit(
            TranscriptionResult(
                text=self.text,
                job_id=job_id,
                context=context,
                duration_s=self.duration_s,
                retried=self.retried,
                no_speech=no_speech,
            )
        )


class _FakePaste:
    """Stands in for `Paster.submit`: records (hwnd, text) and fires done_cb.

    No Win32 focus switch, no synthetic Ctrl+V, no clipboard writes - so the
    test can assert WHAT would be pasted and WHERE without touching the OS.
    """

    def __init__(self):
        self.calls = []      # [(hwnd, text), ...]
        self.success = True

    def submit(self, hwnd, text, done_cb):
        self.calls.append((hwnd, text))
        done_cb(self.success, text)


class _Harness:
    def __init__(self, mw, transcribe, paste):
        self.mw = mw
        self.transcribe = transcribe
        self.paste = paste
        self.fg = 0  # value returned by patched winapi.get_foreground_window()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def flow(qapp, monkeypatch):
    """A real MainWindow with heavy startup neutralized and the transcriber +
    paster replaced by synchronous fakes."""
    import voiceassistant.config as cfg
    import voiceassistant.transcriber as tr
    import voiceassistant.ocr as ocr
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    # Same neutralization as tests/test_ui_smoke.py (no disk, no model load,
    # no registry write, no global hotkeys, no tray).
    monkeypatch.setattr(cfg, "CONFIG_FILE",
                        os.path.join(tempfile.mkdtemp(), "settings.json"))
    monkeypatch.setattr(tr.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)

    mw = MainWindow(entry_script="main.py")
    h = _Harness(mw, None, None)

    # The only Win32 read in the FRONT of the chain. Read-only (never steals
    # focus), but pinned for determinism.
    monkeypatch.setattr(winapi, "get_foreground_window", lambda: h.fg)

    # Swap the two heavy engines for fakes at their real call seams.
    mw.transcriber._model = object()  # is_loaded -> True (front-of-chain gate)
    ft = _FakeTranscribe(mw.transcriber)
    fp = _FakePaste()
    monkeypatch.setattr(mw.transcriber, "transcribe", ft.transcribe)
    monkeypatch.setattr(mw.paster, "submit", fp.submit)
    h.transcribe, h.paste = ft, fp

    yield h

    # Tear down owned workers/timers so threads don't linger across tests.
    for closer in (
        lambda: mw._show_request_timer.stop(),
        lambda: mw.transcriber._worker.shutdown(),
        lambda: mw._read_worker.shutdown(),
        lambda: getattr(mw.ocr, "_worker", None) and mw.ocr._worker.shutdown(),
        lambda: mw.tts.shutdown(),
        lambda: mw.paster.shutdown(),
    ):
        try:
            closer()
        except Exception:
            pass


def _idle(mw):
    """The floating pill's idle label (see RecordingIndicator._set_idle)."""
    return mw.indicator._label.text().startswith("Ready")


# --------------------------------------------------------------------------- #
# 1. Happy path - driven from the REAL top of the chain (hotkey press/release).
# --------------------------------------------------------------------------- #
def test_happy_path_pastes_cleaned_text_to_captured_window(flow, monkeypatch):
    """Proves the full press->record->stop->transcribe->paste WIRING: the HWND
    captured at press reaches paster.submit, the transcript is run through
    clean_transcript on the way, and the paste-done slot renders the state."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    TARGET = 0xABCD  # a foreign window, distinct from our own
    assert TARGET != int(mw.winId())
    flow.fg = TARGET
    mw._dictation_active = True
    assert mw.config.get("auto_paste") is True
    ft.text = "hello world"

    # Fake the recorder so start/stop drive the state machine with no mic.
    rec = mw.recorder
    audio_box = {"buf": LOUD}

    def fake_start():
        rec._is_recording = True
        rec.recording_started.emit()

    def fake_stop():
        if not rec._is_recording:
            return
        rec._is_recording = False
        rec.recording_stopped.emit(audio_box["buf"])

    monkeypatch.setattr(rec, "start", fake_start)
    monkeypatch.setattr(rec, "stop", fake_stop)

    # Press: captures the foreground window and starts recording.
    mw._sig_hotkey_press.emit()
    assert rec.is_recording is True
    assert mw._pending_target_hwnd == TARGET

    # Release: stop -> recording_stopped -> transcribe -> paste, all synchronous.
    mw._sig_hotkey_release.emit()

    assert ft.calls == [(len(LOUD), TARGET)]          # hwnd rode onto the job
    assert fp.calls == [(TARGET, "Hello world.")]     # cleaned + pasted THERE
    assert mw._pending_target_hwnd is None            # hand-off consumed
    assert mw.status_bar.currentMessage() == "Transcribed and pasted"
    # done-state pill (ASCII-safe: label is "Pasted <check>", set by show_done)
    assert mw.indicator._label.text().startswith("Pasted")
    assert mw.text_output.toPlainText() == ""         # pasted, not dumped to panel


# --------------------------------------------------------------------------- #
# 2. Own-window guard - target == our own HWND must route to panel, not paste.
# --------------------------------------------------------------------------- #
def test_own_window_guard_routes_to_panel(flow, monkeypatch):
    """Proves _on_transcription_ready's is_own_window branch: when the job's
    context equals int(self.winId()), the text is appended to the panel and
    paster.submit is NEVER called (never dictate into ourselves)."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    own = int(mw.winId())
    if own == 0:  # offscreen may not realize a native handle; pin one
        own = 0x5EED
        monkeypatch.setattr(mw, "winId", lambda: own)
    mw._dictation_active = True
    ft.text = "note to self"

    # Simulate record-start having captured OUR OWN window as the target.
    mw._pending_target_hwnd = own
    mw.recorder.recording_stopped.emit(LOUD)

    assert ft.calls == [(len(LOUD), own)]  # consumed + handed to the job
    assert fp.calls == []                  # is_own_window -> no paste
    panel = mw.text_output.toPlainText()
    assert "[Voice]" in panel and "Note to self." in panel
    assert mw._pending_target_hwnd is None


# --------------------------------------------------------------------------- #
# 3. Record gate - too short / too quiet is dropped before transcribe.
# --------------------------------------------------------------------------- #
def test_record_gate_drops_short_and_quiet(flow):
    """Proves _on_recording_stopped's gate fires ahead of transcription and
    that the pending target is cleared even on a dropped clip (no stale leak).
    Covers BOTH gate branches: duration (short) and peak (quiet)."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    mw._dictation_active = True
    for buf, label in ((TOO_SHORT, "short"), (TOO_QUIET, "quiet")):
        ft.calls.clear()
        mw._pending_target_hwnd = 0x1234  # a target that must be discarded
        mw.recorder.recording_stopped.emit(buf)

        assert ft.calls == [], f"{label}: gate must drop before transcribe"
        assert fp.calls == [], f"{label}: nothing pasted"
        assert "ignored" in mw.status_bar.currentMessage().lower(), label
        assert _idle(mw), f"{label}: pill returns to idle"
        assert mw._pending_target_hwnd is None, f"{label}: pending target cleared"


# --------------------------------------------------------------------------- #
# 4a. no_speech result - silent (no paste, no panel entry).
# --------------------------------------------------------------------------- #
def test_no_speech_result_is_silent(flow):
    """Proves the no_speech early-return in _on_transcription_ready: neither a
    paste nor a panel append happens (the empty-transcript path, which is how
    a truly silent clip actually arrives)."""
    mw, fp = flow.mw, flow.paste
    mw._dictation_active = True
    mw.transcriber.transcription_ready.emit(
        TranscriptionResult(text="", job_id=1, context=0xABCD,
                            duration_s=2.0, retried=False, no_speech=True)
    )
    assert fp.calls == []
    assert mw.text_output.toPlainText() == ""
    assert mw.status_bar.currentMessage() == "No speech detected"
    assert _idle(mw)


# --------------------------------------------------------------------------- #
# 4b. Filler-only transcript - documents a real spam bug in the routing slot.
# --------------------------------------------------------------------------- #
def test_filler_only_transcript_not_pasted_and_no_panel_marker(flow):
    """A filler-only utterance ("um") is a REACHABLE input (Whisper transcribes
    it; no_speech is derived from the RAW text so it is False). clean_transcript
    strips it to "" -> nothing to paste AND nothing to show. FIXED: the handler
    now routes on the CLEANED text being empty, so it neither pastes nor dumps
    a contentless "[Voice]" marker into the panel (it reports "No speech")."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    mw._dictation_active = True
    mw.transcriber.transcription_ready.emit(
        TranscriptionResult(text="um", job_id=1, context=0xABCD,
                            duration_s=2.0, retried=False, no_speech=False)
    )
    assert fp.calls == []  # cleaned to "" -> correctly not pasted
    assert mw.text_output.toPlainText().strip() == ""  # no blank marker in panel


# --------------------------------------------------------------------------- #
def test_panel_record_during_ptt_hold_does_not_clobber_target(flow):
    """Hand-off race regression: while a push-to-talk dictation is recording
    into an external window, clicking "Record to Panel" must NOT null the
    pending target (which used to mis-route that dictation to the panel on
    release). The button is a guarded no-op while a recording is in flight."""
    mw = flow.mw
    mw._dictation_active = True
    mw._pending_target_hwnd = 0xABCD          # PTT captured an external window
    mw.recorder._is_recording = True          # simulate recording in progress
    try:
        mw._on_record()                       # user clicks "Record to Panel" mid-hold
        assert mw._pending_target_hwnd == 0xABCD, \
            "panel Record clobbered the in-flight push-to-talk target"
    finally:
        mw.recorder._is_recording = False


# --------------------------------------------------------------------------- #
def test_stale_or_duplicate_job_id_is_dropped(flow):
    """Job ids are monotonic; a delivery with id <= the last handled id is a
    duplicate or an out-of-order straggler and must be ignored — not just the
    exact-equal case."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    mw._dictation_active = True
    mw.transcriber.transcription_ready.emit(
        TranscriptionResult(text="first", job_id=5, context=0xABCD,
                            duration_s=1.0, no_speech=False))
    assert len(fp.calls) == 1
    for stale in (5, 3):  # equal, then lower
        mw.transcriber.transcription_ready.emit(
            TranscriptionResult(text="stale", job_id=stale, context=0xABCD,
                                duration_s=1.0, no_speech=False))
    assert len(fp.calls) == 1, "a stale/duplicate job id was not dropped"


# --------------------------------------------------------------------------- #
def test_own_window_guard_survives_flag_toggle(flow):
    """winId() changes when setWindowFlags recreates the native handle (e.g.
    toggling always-on-top). A dictation whose target was our own window under
    the OLD handle must STILL be treated as own-window (routed to the panel,
    never pasted) after a toggle — the guard matches against all our handles."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    mw._dictation_active = True
    old_hwnd = int(mw.winId())
    mw.config.set("always_on_top", not mw.config["always_on_top"])
    mw._apply_window_flags()  # recreates the handle; new winId may differ
    mw.transcriber.transcription_ready.emit(
        TranscriptionResult(text="into our own window", job_id=1, context=old_hwnd,
                            duration_s=1.0, no_speech=False))
    assert fp.calls == [], "pasted into our own window (guard lost the pre-toggle handle)"
    assert old_hwnd in mw._own_hwnds


# --------------------------------------------------------------------------- #
def test_recorder_pins_capture_rate_to_16k():
    """Whisper requires 16 kHz; the recorder must pin to it (and every
    duration calc derives from recorder.sample_rate) so a stray config value
    can't desync the gate or mis-time the audio."""
    from voiceassistant.recorder import VoiceRecorder
    assert VoiceRecorder(sample_rate=16000).sample_rate == 16000
    assert VoiceRecorder(sample_rate=44100).sample_rate == 16000  # coerced


# --------------------------------------------------------------------------- #
# 5. Job-id duplicate guard - same job_id delivered twice is ignored.
# --------------------------------------------------------------------------- #
def test_duplicate_job_id_is_ignored(flow):
    """Proves the _last_job_id dedupe in _on_transcription_ready: a second
    delivery of the same job_id is dropped, so a duplicated signal can neither
    double-paste nor overwrite with different text."""
    mw, fp = flow.mw, flow.paste
    mw._dictation_active = True
    TARGET = 0xABCD
    tr = mw.transcriber

    tr.transcription_ready.emit(
        TranscriptionResult(text="first message", job_id=7, context=TARGET,
                            duration_s=2.0, retried=False, no_speech=False)
    )
    assert fp.calls == [(TARGET, "First message.")]

    # Same job_id again (duplicate delivery) -> must be dropped.
    tr.transcription_ready.emit(
        TranscriptionResult(text="second message", job_id=7, context=TARGET,
                            duration_s=2.0, retried=False, no_speech=False)
    )
    assert fp.calls == [(TARGET, "First message.")]  # unchanged
    assert "Second" not in mw.text_output.toPlainText()


# --------------------------------------------------------------------------- #
# 6. Dictation OFF - text goes to the panel, never pastes (even with a target).
# --------------------------------------------------------------------------- #
def test_dictation_off_routes_to_panel(flow):
    """Proves the _dictation_active gate in the routing condition: paused, even
    WITH a valid captured target, the transcript is appended to the panel and
    paster.submit is never called."""
    mw, ft, fp = flow.mw, flow.transcribe, flow.paste
    mw._dictation_active = False  # paused
    flow.fg = 0xABCD
    ft.text = "meeting at noon"

    mw._pending_target_hwnd = 0xABCD  # a target is present but must be ignored
    mw.recorder.recording_stopped.emit(LOUD)

    assert ft.calls == [(len(LOUD), 0xABCD)]  # still transcribes with context
    assert fp.calls == []                     # ...but dictation off -> no paste
    panel = mw.text_output.toPlainText()
    assert "[Voice]" in panel and "Meeting at noon." in panel
    assert mw.status_bar.currentMessage() == "Transcription complete"


# --------------------------------------------------------------------------- #
# 7. HWND hand-off - _pending_target_hwnd is consumed exactly once; a later
#    dictation can never reuse a stale target.
# --------------------------------------------------------------------------- #
def test_pending_hwnd_consumed_once_no_stale_reuse(flow, monkeypatch):
    """Proves the record-START -> record-STOP hand-off: the captured HWND is
    consumed into the job on recording_stopped and the shared field is cleared,
    so a SECOND recording with no fresh capture transcribes with context=None
    (never the stale first target). This is the invariant behind the job-bound
    HWND refactor."""
    mw, ft = flow.mw, flow.transcribe
    mw._dictation_active = True
    flow.fg = 111
    monkeypatch.setattr(mw.recorder, "start", lambda: None)  # no mic

    # Record-start captures the foreground window.
    mw._on_record_from_hotkey()
    assert mw._pending_target_hwnd == 111

    # Stop: target is consumed onto the job and the field is cleared.
    mw.recorder.recording_stopped.emit(LOUD)
    assert ft.calls[-1] == (len(LOUD), 111)
    assert mw._pending_target_hwnd is None

    # A second stop with no fresh capture must NOT reuse the stale 111.
    mw.recorder.recording_stopped.emit(LOUD)
    assert ft.calls[-1] == (len(LOUD), None)
    assert len(ft.calls) == 2
