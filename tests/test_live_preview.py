"""Live preview (Stage 1 of type-as-you-talk): policy, decode seam, pill, wiring.

What is pinned here, and why each matters:

- **The stabilizer never hides a correction.** The full latest hypothesis is
  always shown; agreement only controls emphasis. A preview that froze early
  words would lie about what the final pass will paste.
- **At most ONE preview decode is outstanding.** The preview shares the
  transcriber's worker with the FINAL decode, so a backlog of drafts would
  push the real transcription (the thing that gets pasted) behind them.
- **end() cancels queued drafts and drops in-flight results.** A draft for a
  recording that already ended must never repaint the pill over the
  "Transcribing…" state, and must never delay the final decode.
- **A preview failure is quiet and scoped to the recording.** The dictation
  itself is untouched; the draft simply stops.
- **Long holds stay bounded.** Past the live window, the oldest segments are
  frozen and the decode window shrinks, so a two-minute hold does not turn
  into a two-minute decode.
- **The decode seam is greedy, VAD-free, and always answers.** The controller's
  in-flight flag is cleared by the result, so a job that emitted nothing on
  error would freeze the preview for the rest of the recording.
"""

import os
import sys
import tempfile

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voiceassistant import live_preview as lp  # noqa: E402
from voiceassistant.live_preview import (  # noqa: E402
    HARD_S, KEEP_S, WINDOW_S, LivePreview, PreviewStabilizer, choose_commit,
)
from voiceassistant.transcriber import PreviewResult, Transcriber  # noqa: E402

SR = 16000


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --------------------------------------------------------------------------- #
# 1. PreviewStabilizer — pure logic
# --------------------------------------------------------------------------- #
class TestStabilizer:
    def test_first_hypothesis_is_all_tail(self):
        st = PreviewStabilizer()
        assert st.update("hello there world") == ("", "hello there world")

    def test_repeat_makes_everything_stable(self):
        st = PreviewStabilizer()
        st.update("hello there world")
        assert st.update("hello there world") == ("hello there world", "")

    def test_correction_shows_and_moves_the_boundary(self):
        st = PreviewStabilizer()
        st.update("we need to ship the fix")
        # The model revised "ship" -> "shift": the FULL new text is returned,
        # stable stops before the changed word.
        stable, tail = st.update("we need to shift the fix today")
        assert stable == "we need to"
        assert tail == "shift the fix today"
        assert (stable + " " + tail) == "we need to shift the fix today"

    def test_agreement_ignores_case_and_punctuation(self):
        st = PreviewStabilizer()
        st.update("hello world")
        stable, tail = st.update("Hello, world.")
        assert stable == "Hello, world."
        assert tail == ""

    def test_reset_forgets_previous(self):
        st = PreviewStabilizer()
        st.update("same text")
        st.reset()
        assert st.update("same text") == ("", "same text")

    def test_empty_hypothesis(self):
        st = PreviewStabilizer()
        assert st.update("") == ("", "")


# --------------------------------------------------------------------------- #
# 2. choose_commit — pure logic
# --------------------------------------------------------------------------- #
class TestChooseCommit:
    SEGS = [(0.0, 5.0, "a"), (5.0, 11.0, "b"), (11.0, 17.0, "c"), (17.0, 24.0, "d")]

    def test_short_window_commits_nothing(self):
        assert choose_commit(self.SEGS, WINDOW_S) == 0
        assert choose_commit([], 100.0) == 0

    def test_commits_segments_older_than_keep(self):
        # 24 s window, keep the last 8 s live -> cutoff 16 s -> a, b qualify.
        n = choose_commit(self.SEGS, 24.0)
        assert n == 2
        assert self.SEGS[n - 1][1] <= 24.0 - KEEP_S

    def test_no_clean_boundary_waits_until_hard_limit(self):
        one_long = [(0.0, 23.0, "one long unbroken segment")]
        assert choose_commit(one_long, 23.0) == 0
        assert choose_commit(one_long, HARD_S + 1) == 1

    def test_hard_limit_keeps_last_segment_live_when_possible(self):
        late = [(0.0, 26.0, "x"), (26.0, 29.0, "y")]
        assert choose_commit(late, HARD_S + 1) == 1


# --------------------------------------------------------------------------- #
# 3. LivePreview controller with fakes
# --------------------------------------------------------------------------- #
class _FakeRecorder:
    def __init__(self):
        self.is_recording = True
        self.stopping = False
        self.frames = 0

    def capture_frames(self):
        return self.frames if self.is_recording else 0

    def peek(self):
        if not self.is_recording:
            return None
        return np.zeros(self.frames, dtype=np.float32)


class _FakeTranscriber(QObject):
    preview_ready = Signal(object)

    def __init__(self):
        super().__init__()
        self.is_loaded = True
        self.calls = []       # (n_frames, prompt, gen, offset)
        self.cancels = 0

    def preview(self, audio, prompt, gen, offset_frames):
        self.calls.append((len(audio), prompt, gen, offset_frames))

    def cancel_previews(self):
        self.cancels += 1


@pytest.fixture
def ctl(qapp):
    rec = _FakeRecorder()
    tr = _FakeTranscriber()
    c = LivePreview(rec, tr, enabled=True, light_cleanup=True)
    emitted = []
    c.preview_text.connect(lambda s, t: emitted.append((s, t)))
    c._timer.stop()  # ticks are driven by hand below
    yield c, rec, tr, emitted
    c.shutdown()


def _result(ctl_obj, segments, n_frames, offset=0, error="", latency=120.0):
    return PreviewResult(ctl_obj._gen, segments, offset, n_frames,
                         latency_ms=latency, error=error)


class TestController:
    def test_disabled_never_activates_or_submits(self, ctl):
        c, rec, tr, _ = ctl
        c.enabled = False
        c.begin()
        rec.frames = 5 * SR
        c._on_tick()
        assert not c.active
        assert tr.calls == []

    def test_unloaded_model_never_activates(self, ctl):
        c, rec, tr, _ = ctl
        tr.is_loaded = False
        c.begin()
        assert not c.active

    def test_waits_for_minimum_audio(self, ctl):
        c, rec, tr, _ = ctl
        c.begin()
        rec.frames = int(0.3 * SR)
        c._on_tick()
        assert tr.calls == []
        rec.frames = int(1.0 * SR)
        c._on_tick()
        assert len(tr.calls) == 1

    def test_at_most_one_decode_outstanding(self, ctl):
        c, rec, tr, _ = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        rec.frames = 4 * SR
        c._on_tick()
        c._on_tick()
        assert len(tr.calls) == 1, "a second draft was queued behind an in-flight one"
        # The result frees the slot; the next tick submits again.
        tr.preview_ready.emit(_result(c, [(0.0, 2.0, "hello")], 2 * SR))
        c._on_tick()
        assert len(tr.calls) == 2

    def test_needs_fresh_audio_between_decodes(self, ctl):
        c, rec, tr, _ = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        tr.preview_ready.emit(_result(c, [(0.0, 2.0, "hello")], 2 * SR))
        rec.frames = 2 * SR + int(0.1 * SR)   # only 100 ms new
        c._on_tick()
        assert len(tr.calls) == 1

    def test_result_renders_stable_and_tail(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        tr.preview_ready.emit(_result(c, [(0.0, 2.0, "hello there")], 2 * SR))
        assert emitted[-1] == ("", "hello there")
        rec.frames = 3 * SR
        c._on_tick()
        tr.preview_ready.emit(_result(c, [(0.0, 3.0, "hello there friend")], 3 * SR))
        assert emitted[-1] == ("hello there", "friend")

    def test_light_cleanup_strips_fillers_from_the_draft(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        tr.preview_ready.emit(_result(c, [(0.0, 2.0, "so um we should")], 2 * SR))
        assert "um" not in emitted[-1][1].split()

    def test_stale_generation_is_ignored(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        old = _result(c, [(0.0, 2.0, "old draft")], 2 * SR)
        c.end()
        c.begin()
        tr.preview_ready.emit(old)
        assert emitted == []

    def test_end_cancels_and_stops(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        c.end()
        assert tr.cancels == 1
        assert not c.active
        assert not c._timer.isActive()
        tr.preview_ready.emit(_result(c, [(0.0, 2.0, "late")], 2 * SR))
        assert emitted == []
        rec.frames = 4 * SR
        c._on_tick()
        assert len(tr.calls) == 1

    def test_error_disables_quietly_for_this_recording(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        tr.preview_ready.emit(_result(c, [], 2 * SR, error="RuntimeError: CUDA out of memory"))
        assert emitted == []
        rec.frames = 4 * SR
        c._on_tick()
        assert len(tr.calls) == 1
        # ...but the NEXT recording tries again.
        c.end()
        c.begin()
        rec.frames = 2 * SR
        c._on_tick()
        assert len(tr.calls) == 2

    def test_no_submit_while_stopping_or_idle(self, ctl):
        c, rec, tr, _ = ctl
        c.begin()
        rec.frames = 2 * SR
        rec.stopping = True
        c._on_tick()
        rec.stopping = False
        rec.is_recording = False
        c._on_tick()
        assert tr.calls == []

    def test_long_hold_commits_and_shrinks_the_window(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 24 * SR
        c._on_tick()
        segs = [(0.0, 5.0, "First sentence."), (5.0, 11.0, "Second sentence."),
                (11.0, 17.0, "Third one."), (17.0, 24.0, "still going")]
        tr.preview_ready.emit(_result(c, segs, 24 * SR))
        stable, tail = emitted[-1]
        assert stable.startswith("First sentence. Second sentence.")
        assert "still going" in tail
        assert c._committed_frames == 11 * SR
        rec.frames = 25 * SR
        c._on_tick()
        n, prompt, _gen, offset = tr.calls[-1]
        assert offset == 11 * SR
        assert n == 25 * SR - 11 * SR, "the decode window did not shrink"
        assert prompt.endswith("Second sentence.")

    def test_stats_report_count_and_median(self, ctl):
        c, rec, tr, _ = ctl
        c.begin()
        for lat in (300.0, 100.0, 200.0):
            rec.frames += 2 * SR
            c._on_tick()
            tr.preview_ready.emit(_result(c, [(0.0, 1.0, "x")], rec.frames, latency=lat))
        assert c.stats() == {"preview_decodes": 3, "preview_ms": 200.0}


# --------------------------------------------------------------------------- #
# 3b. The REAL recorder's peek() seam (the live gate fakes this one)
# --------------------------------------------------------------------------- #
class TestRecorderPeek:
    """`peek()` must hand back the capture SO FAR without ending it — a preview
    that consumed the recording, or that reported frames while idle, would
    break the dictation it is supposed to be previewing."""

    @staticmethod
    def _armed(preroll_ms=0):
        from voiceassistant.recorder import VoiceRecorder

        rec = VoiceRecorder(max_seconds=120.0, preroll_ms=preroll_ms)
        rec._stream = object()
        rec._alive = True
        rec._is_recording = True
        rec._capture_start = rec._frames_written
        return rec

    @staticmethod
    def _block(n=1024, amp=0.1):
        return np.full((n, 1), amp, dtype="float32")

    def test_idle_recorder_reports_nothing(self):
        from voiceassistant.recorder import VoiceRecorder

        rec = VoiceRecorder()
        assert rec.capture_frames() == 0
        assert rec.peek() is None
        assert rec.stopping is False

    def test_peek_grows_and_does_not_end_the_recording(self):
        rec = self._armed()
        rec._audio_callback(self._block(), 1024, None, None)
        first = rec.peek()
        assert len(first) == 1024 and rec.capture_frames() == 1024
        rec._audio_callback(self._block(), 1024, None, None)
        second = rec.peek()
        assert len(second) == 2048
        assert np.allclose(second[:1024], first), "earlier audio changed under the preview"
        assert rec.is_recording, "peek ended the recording"
        # ...and the FINAL capture still delivers everything.
        got = []
        rec.recording_stopped.connect(got.append)
        rec._finish_capture()
        assert len(got) == 1 and len(got[0]) == 2048

    def test_peek_includes_the_preroll(self):
        rec = self._armed()
        rec._audio_callback(self._block(amp=0.2), 1024, None, None)
        rec._capture_start = max(0, rec._frames_written - 512)  # as start() does
        assert rec.capture_frames() == 512
        assert len(rec.peek()) == 512

    def test_stopping_flag_tracks_the_tail_drain(self, qapp):
        rec = self._armed()
        rec._audio_callback(self._block(), 1024, None, None)
        assert rec.stopping is False
        rec.stop()
        assert rec.stopping is True, "preview kept decoding through the tail drain"
        rec._tail_timer.stop()
        rec._finish_capture()
        assert rec.stopping is False


# --------------------------------------------------------------------------- #
# 4. Transcriber._preview_job — the decode seam
# --------------------------------------------------------------------------- #
class _Seg:
    def __init__(self, start, end, text, nsp=0.0):
        self.start, self.end, self.text, self.no_speech_prob = start, end, text, nsp


class _FakeModel:
    def __init__(self, segs, on_iter=None, raise_exc=None):
        self.segs = segs
        self.kwargs = None
        self.on_iter = on_iter
        self.raise_exc = raise_exc

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        if self.raise_exc:
            raise self.raise_exc

        def gen():
            for s in self.segs:
                if self.on_iter:
                    self.on_iter()
                yield s
        return gen(), None


@pytest.fixture
def tr(qapp):
    t = Transcriber(model_size="tiny", language="en", initial_prompt="")
    got = []
    t.preview_ready.connect(got.append)
    yield t, got
    t.shutdown()


class TestPreviewJob:
    def test_greedy_vad_free_and_filters_no_speech(self, tr):
        t, got = tr
        t._model = _FakeModel([_Seg(0.0, 1.0, " hello "), _Seg(1.0, 2.0, "noise", nsp=0.9)])
        t._preview_gen = 7
        t._preview_job(np.zeros(SR, dtype=np.float32), "", 7, 0)
        assert len(got) == 1
        r = got[0]
        assert r.gen == 7 and r.error == ""
        assert r.segments == [(0.0, 1.0, "hello")]
        assert r.n_frames == SR and r.offset_frames == 0
        k = t._model.kwargs
        assert k["beam_size"] == 1 and k["best_of"] == 1
        assert k["vad_filter"] is False
        assert k["condition_on_previous_text"] is False
        assert "initial_prompt" not in k

    def test_prompt_combines_user_prompt_and_committed_text(self, tr):
        t, got = tr
        t.initial_prompt = "ASIN SKU"
        t._model = _FakeModel([_Seg(0.0, 1.0, "x")])
        t._preview_gen = 1
        t._preview_job(np.zeros(SR, dtype=np.float32), "earlier words.", 1, 5)
        assert t._model.kwargs["initial_prompt"] == "ASIN SKU earlier words."
        assert got[0].offset_frames == 5

    def test_cancelled_job_answers_without_decoding(self, tr):
        t, got = tr
        t._model = _FakeModel([_Seg(0.0, 1.0, "x")])
        t._preview_gen = 2
        t._preview_job(np.zeros(SR, dtype=np.float32), "", 1, 0)
        assert len(got) == 1 and got[0].segments == [] and got[0].error == ""
        assert t._model.kwargs is None, "a cancelled draft still hit the GPU"

    def test_cancel_mid_decode_stops_iterating(self, tr):
        t, got = tr

        def cancel():
            t._preview_gen = -1
        t._model = _FakeModel([_Seg(0.0, 1.0, "a"), _Seg(1.0, 2.0, "b")], on_iter=cancel)
        t._preview_gen = 3
        t._preview_job(np.zeros(SR, dtype=np.float32), "", 3, 0)
        assert got[0].segments == []

    def test_exception_is_reported_not_raised(self, tr):
        t, got = tr
        t._model = _FakeModel([], raise_exc=RuntimeError("CUDA out of memory"))
        t._preview_gen = 4
        t._preview_job(np.zeros(SR, dtype=np.float32), "", 4, 0)
        assert got[0].error.startswith("RuntimeError")
        assert got[0].segments == []

    def test_preview_and_cancel_manage_generation(self, tr):
        t, _ = tr
        t._model = _FakeModel([])
        t.preview(np.zeros(SR, dtype=np.float32), "", 9, 0)
        assert t._preview_gen == 9
        t.cancel_previews()
        assert t._preview_gen == -1


# --------------------------------------------------------------------------- #
# 5. The pill's caption card
# --------------------------------------------------------------------------- #
class TestPill:
    def test_preview_expands_and_clear_collapses(self, qapp):
        from voiceassistant.widgets import RecordingIndicator
        ind = RecordingIndicator()
        ind.show_recording()
        assert ind.size().width() == ind.COMPACT_W
        ind.show_preview("hello there", "friend")
        assert ind.preview_text() == "hello there friend"
        assert ind.width() == ind.CARD_W
        assert ind.height() > ind.COMPACT_H
        assert not ind._preview.isHidden()
        ind.clear_preview()
        assert ind.preview_text() == ""
        assert ind.size().width() == ind.COMPACT_W
        assert ind.size().height() == ind.COMPACT_H
        ind.close()

    def test_transcribing_keeps_preview_but_error_and_idle_clear_it(self, qapp):
        from voiceassistant.widgets import RecordingIndicator
        ind = RecordingIndicator()
        ind.show_recording()
        ind.show_preview("some words", "")
        ind.show_transcribing()
        assert ind.preview_text() == "some words"
        ind.show_pasting()
        assert ind.preview_text() == "some words"
        ind.show_error("Too short")
        assert ind.preview_text() == ""
        ind.show_preview("again", "")
        ind.show_idle()
        assert ind.preview_text() == ""
        ind.close()

    def test_long_draft_trims_from_the_front(self, qapp):
        from voiceassistant.widgets import RecordingIndicator
        ind = RecordingIndicator()
        ind.show_recording()
        stable = " ".join(f"word{i}" for i in range(60))
        ind.show_preview(stable, "newest")
        shown = ind._preview.text()
        assert shown.startswith("…")
        assert "newest" in shown
        assert "word0 " not in shown
        assert ind.preview_text().endswith("newest")
        ind.close()

    def test_empty_preview_collapses(self, qapp):
        from voiceassistant.widgets import RecordingIndicator
        ind = RecordingIndicator()
        ind.show_preview("x", "")
        ind.show_preview("", "")
        assert ind.preview_text() == ""
        assert ind.size().width() == ind.COMPACT_W
        ind.close()


# --------------------------------------------------------------------------- #
# 6. MainWindow wiring
# --------------------------------------------------------------------------- #
@pytest.fixture
def mw(qapp, monkeypatch):
    import voiceassistant.config as cfg
    import voiceassistant.ocr as ocr
    import voiceassistant.recorder as rec_mod
    import voiceassistant.transcriber as trm
    import voiceassistant.tts as tts
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    monkeypatch.setattr(cfg, "CONFIG_FILE",
                        os.path.join(tempfile.mkdtemp(), "settings.json"))
    monkeypatch.setattr(trm.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(tts.TTSEngine, "_load_kokoro", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)
    monkeypatch.setattr(rec_mod.VoiceRecorder, "open_stream", lambda self: True)
    w = MainWindow(entry_script="main.py")
    w.transcriber._model = object()
    yield w
    for closer in (w._show_request_timer.stop, w.live_preview.shutdown,
                   w.tts.shutdown, w.paster.shutdown,
                   w.transcriber._worker.shutdown, w._selection_reader.shutdown):
        try:
            closer()
        except Exception:
            pass


class TestWiring:
    def test_recording_start_activates_preview_and_draft_reaches_pill(self, mw):
        mw.live_preview._timer.stop()
        mw.recorder.recording_started.emit()
        assert mw.live_preview.active
        mw.live_preview._timer.stop()
        mw.live_preview.preview_text.emit("hello", "world")
        assert mw.indicator.preview_text() == "hello world"

    def test_stop_ends_preview_before_final_decode_and_records_metrics(self, mw, monkeypatch):
        from voiceassistant import metrics
        order = []
        monkeypatch.setattr(mw.transcriber, "cancel_previews",
                            lambda: order.append("cancel"))
        monkeypatch.setattr(mw.transcriber, "transcribe",
                            lambda audio, context=None: order.append("final") or 1)
        mw.recorder.recording_started.emit()
        mw.live_preview._timer.stop()
        mw.live_preview._latencies_ms = [150.0, 250.0]
        t = np.arange(SR, dtype=np.float32) / SR
        loud = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        mw._on_recording_stopped(loud)
        assert order == ["cancel", "final"]
        assert not mw.live_preview.active
        # The pending metrics carry the preview numbers into the final row.
        pending = list(mw._metrics_pending.values())
        assert pending and pending[-1]["preview_decodes"] == 2
        assert pending[-1]["preview_ms"] == 250.0
        assert callable(metrics.record)

    def test_report_summarizes_preview_rows(self):
        from voiceassistant import metrics
        rows = [
            {"outcome": "pasted", "preview_decodes": 4, "preview_ms": 300.0},
            {"outcome": "pasted", "preview_decodes": 6, "preview_ms": 500.0},
            {"outcome": "pasted"},
        ]
        s = metrics.summarize(rows)
        assert s["preview_dictations"] == 2
        assert s["preview_ms_p50"] in (300.0, 500.0)
        assert "live preview" in metrics.format_report(rows)


# --------------------------------------------------------------------------- #
# 7. Commit-seam regressions (found by the 2026-09-12 audit)
# --------------------------------------------------------------------------- #
class TestCommitSeam:
    """On the tick where the preview freezes its oldest segments, the reported
    stable text must NOT shrink.

    It used to: the stabilizer compared only the live half against a previous
    hypothesis that still began with the just-frozen words, so the prefix
    comparison misaligned and stable collapsed for one tick. On the pill that
    is a flicker; with inline typing it is a delete-and-retype burst in the
    user's document, and past the streaming correction limit typing stops
    silently for the rest of the dictation.
    """

    @staticmethod
    def _segments(duration_s):
        return [(0.0, 5.0, "First sentence."), (5.0, 11.0, "Second sentence."),
                (11.0, 17.0, "Third one."), (17.0, duration_s, "still going")]

    def test_stable_text_never_shrinks_across_a_commit(self, ctl):
        c, rec, tr, emitted = ctl
        c.begin()
        # Two ticks below the commit threshold so the stabilizer has settled.
        for frames, segs in ((12 * SR, [(0.0, 5.0, "First sentence."),
                                        (5.0, 11.0, "Second sentence.")]),
                             (12 * SR, [(0.0, 5.0, "First sentence."),
                                        (5.0, 11.0, "Second sentence.")])):
            rec.frames += 2 * SR
            c._on_tick()
            tr.preview_ready.emit(_result(c, segs, frames))
        settled = emitted[-1][0]
        assert settled.startswith("First sentence."), settled

        # Now a tick that commits.
        rec.frames = 24 * SR
        c._on_tick()
        tr.preview_ready.emit(_result(c, self._segments(24.0), 24 * SR))
        after = emitted[-1][0]
        assert c._committed_text, "nothing was committed; test no longer covers the seam"
        assert after.startswith(settled), (
            "stable text shrank across the commit seam: %r -> %r" % (settled, after))

    def test_committed_text_gets_the_same_cleanup_as_the_live_half(self, ctl):
        """Frozen fillers survived into the typed draft while the final pass
        stripped them, pushing the reconciliation past its correction limit."""
        c, rec, tr, emitted = ctl
        c.begin()
        rec.frames = 24 * SR
        c._on_tick()
        segs = [(0.0, 5.0, "So um the first point."),
                (5.0, 11.0, "And uh the second."),
                (11.0, 17.0, "Third one."), (17.0, 24.0, "still going")]
        tr.preview_ready.emit(_result(c, segs, 24 * SR))
        assert c._committed_text, "nothing committed"
        words = c._committed_text.lower().split()
        assert "um" not in words and "uh" not in words, c._committed_text


class TestDecodeFailureIsLoud:
    """A decode that RAISES must not lose the dictation silently.

    Found by the 2026-09-12 audit: the failure reached only the generic error
    slot, which sets a status line and idles the pill — indistinguishable from
    "nothing happened" on a tray-first app. No metric was recorded (so
    `--report`, the documented arbiter of "is dictation OK", could not see its
    own worst outcome), and any inline-typed draft was orphaned in the user's
    document with nothing left to correct it.
    """

    def test_a_raising_decode_reports_the_job_it_lost(self, qapp):
        from voiceassistant.transcriber import Transcriber

        t = Transcriber(model_size="tiny", language="en")
        try:
            class _Boom:
                def transcribe(self, *a, **k):
                    raise RuntimeError("CUDA out of memory")

            t._model = _Boom()
            failures, readies = [], []
            t.transcription_failed.connect(
                lambda job_id, ctx, msg: failures.append((job_id, ctx, msg)))
            t.transcription_ready.connect(readies.append)
            t._transcribe_job(np.zeros(SR, dtype=np.float32), 4242, 7)
            assert readies == []
            assert len(failures) == 1
            job_id, ctx, msg = failures[0]
            assert job_id == 7, "the failure did not name its job"
            assert ctx == 4242, "the failure did not carry the target window"
            assert "CUDA out of memory" in msg
        finally:
            t.shutdown()

    def test_the_window_records_it_erases_the_draft_and_says_so(self, mw):
        from voiceassistant import metrics

        mw._inline_jobs[9] = 3
        mw._metrics_pending[9] = {"hold_s": 2.0}
        discarded = []
        mw._inline_discard = lambda session=None, erase=True: discarded.append(session)
        mw._on_transcription_failed(9, 1234, "Transcription error: CUDA out of memory")
        assert discarded == [3], "the orphaned draft was not reclaimed"
        rows = metrics.load()
        assert rows[-1]["outcome"] == metrics.OUTCOME_DECODE_FAILED
        assert rows[-1]["hold_s"] == 2.0, "the dictation's metrics were dropped"
        assert 9 not in mw._metrics_pending and 9 not in mw._inline_jobs
        # The pill is the only surface a tray-first user sees.
        assert "failed" in mw.indicator._label.text().lower()
        assert "lost" in mw.status_bar.currentMessage().lower()

    def test_decode_failure_counts_as_a_bad_dictation_in_the_report(self):
        from voiceassistant import metrics

        s = metrics.summarize([{"outcome": metrics.OUTCOME_DECODE_FAILED},
                               {"outcome": metrics.OUTCOME_PASTED}])
        assert s["success_rate"] == 0.5
        text = metrics.format_report([{"outcome": metrics.OUTCOME_DECODE_FAILED}])
        assert "Decode failures" in text
        assert text.isascii()


class TestMicDeathCleansUp:
    def test_a_mic_stall_ends_the_preview_and_reclaims_the_draft(self, mw):
        """A stall never emits recording_stopped, so none of the normal
        end-of-dictation cleanup runs unless this path does it."""
        mw.recorder.recording_started.emit()
        assert mw.live_preview.active
        discarded = []
        mw._inline_discard = lambda session=None, erase=True: discarded.append(True)
        mw._on_mic_error("Microphone dropped out mid-recording")
        assert not mw.live_preview.active, "the preview kept ticking after the mic died"
        assert not mw.live_preview._timer.isActive()
        assert discarded, "the typed draft was left in the user's document"
