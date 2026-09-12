"""Live gate for the dictation preview: real Whisper, real controller, real pill.

The fast suite fakes the model, so it proves the WIRING but says nothing about
whether a draft is fast enough to feel live or accurate enough to trust. This
runs the real `LivePreview` controller against the real loaded model, feeding
it real speech the way a held hotkey would, and asserts the three properties
the feature actually promises:

  1. drafts arrive while you are still speaking (latency budget),
  2. the draft converges on what the final pass will paste,
  3. the final decode is never made to wait behind drafts.

Local/manual: `RUN_PREVIEW=1 pytest tests/integration/test_live_preview_live.py -s`
(add `PREVIEW_MODEL=<name>` to vet a non-default model). Skipped otherwise —
it loads a multi-GB model and needs a GPU to be meaningful.
"""

import os
import sys
import time

import numpy as np
import pytest

# The preview tests need no window, so they run offscreen. The inline-typing
# chain test needs a REAL window it can focus and type into, and an offscreen
# widget has no foreground to take — it would skip itself every time.
if os.environ.get("RUN_INLINE") != "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip("PySide6")
pytest.importorskip("soundfile")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voiceassistant.live_preview import LivePreview  # noqa: E402
from voiceassistant.transcriber import Transcriber  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PREVIEW") != "1",
    reason="live preview gate: set RUN_PREVIEW=1 (loads the real model)",
)

SR = 16000
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fixtures", "audio")
# A draft must land inside this budget for the caption to feel live. The GUI
# tick submits at most one at a time, so this is also the refresh interval.
DRAFT_BUDGET_MS = 1500
# Words the final pass produced that the last draft also had. The draft is
# greedy where the final is beam-search, so it is allowed to be imperfect —
# but it must not be a different sentence.
MIN_RECALL = 0.80


def _load(name):
    import soundfile as sf
    audio, sr = sf.read(os.path.join(FIXTURES, name), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    assert sr == SR, f"{name} is {sr} Hz"
    return audio


def _words(text):
    import re
    return [re.sub(r"[^\w']", "", w).lower() for w in text.split() if w.strip()]


def _recall(draft, final):
    fw, dw = _words(final), set(_words(draft))
    if not fw:
        return 1.0
    return sum(1 for w in fw if w in dw) / len(fw)


class _FeedRecorder:
    """Stands in for VoiceRecorder: plays `audio` out at wall-clock speed, so
    the controller sees exactly the growing capture a held hotkey produces."""

    def __init__(self, audio):
        self.audio = audio
        self.is_recording = True
        self.stopping = False
        self.t0 = time.perf_counter()

    def _n(self):
        elapsed = time.perf_counter() - self.t0
        return min(len(self.audio), int(elapsed * SR))

    def capture_frames(self):
        return self._n() if self.is_recording else 0

    def peek(self):
        return self.audio[: self._n()].copy() if self.is_recording else None


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def transcriber(qapp):
    model = os.environ.get("PREVIEW_MODEL", "large-v3")
    t = Transcriber(model_size=model, device="cuda", compute_type="float16",
                    language="en")
    ready = []
    t.model_ready.connect(lambda: ready.append(True))
    t.error.connect(lambda m: pytest.fail(f"model load failed: {m}"))
    t.load_model()
    deadline = time.time() + 300
    while not ready and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.05)
    assert ready, "model never became ready"
    print(f"\nmodel: {model} on {t.device}")
    yield t
    t.shutdown()


def _run_hold(transcriber, audio, speak_seconds):
    """Drive one simulated hold; return (drafts, controller, recorder)."""
    rec = _FeedRecorder(audio)
    ctl = LivePreview(rec, transcriber, enabled=True, light_cleanup=True)
    drafts = []  # (t_since_start_s, stable, tail)
    ctl.preview_text.connect(
        lambda s, t: drafts.append((time.perf_counter() - rec.t0, s, t)))
    ctl.begin()
    deadline = rec.t0 + speak_seconds
    while time.perf_counter() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    return drafts, ctl, rec


def test_drafts_arrive_while_speaking_and_track_the_speech(transcriber):
    """Words must appear DURING the hold, refresh steadily, and grow."""
    audio = np.concatenate([_load("long_paragraph_trailing_silence.wav"),
                            _load("normal_sentence.wav")])
    drafts, ctl, rec = _run_hold(transcriber, audio, speak_seconds=12.0)
    ctl.end()
    ctl.shutdown()

    assert drafts, "no draft ever reached the pill during a 12s hold"
    first_t = drafts[0][0]
    print(f"  first draft at {first_t:.2f}s, {len(drafts)} drafts in 12s")
    for t, stable, tail in drafts:
        print(f"    {t:5.2f}s  [{stable[-70:]}] <{tail}>")

    assert first_t < 3.0, f"first draft took {first_t:.2f}s — not live"
    # Steady refresh: the gaps between drafts are the user's perceived latency.
    gaps = [b[0] - a[0] for a, b in zip(drafts, drafts[1:])]
    assert gaps, "only one draft in 12 seconds"
    median_gap_ms = sorted(gaps)[len(gaps) // 2] * 1000
    print(f"  median gap between drafts: {median_gap_ms:.0f} ms")
    assert median_gap_ms < DRAFT_BUDGET_MS, (
        f"drafts refresh every {median_gap_ms:.0f} ms — too slow to read as live")

    stats = ctl.stats()
    print(f"  decode latency median: {stats['preview_ms']:.0f} ms "
          f"over {stats['preview_decodes']} decodes")
    assert stats["preview_ms"] < DRAFT_BUDGET_MS

    # The transcript grows: the last draft says materially more than the first.
    assert len(" ".join(drafts[-1][1:]).split()) > len(" ".join(drafts[0][1:]).split())


def test_draft_matches_what_the_final_pass_will_paste(transcriber):
    """The preview must not show a different sentence from the pasted text."""
    audio = _load("long_paragraph_trailing_silence.wav")
    speak_s = len(audio) / SR + 1.5
    drafts, ctl, rec = _run_hold(transcriber, audio, speak_seconds=speak_s)
    ctl.end()
    ctl.shutdown()
    assert drafts, "no draft produced"
    last = " ".join(p for p in drafts[-1][1:] if p)

    final = transcriber._run_transcribe(audio, use_vad=True)
    recall = _recall(last, final)
    print(f"\n  final : {final}\n  draft : {last}\n  recall: {recall:.1%}")
    assert recall >= MIN_RECALL, (
        f"only {recall:.0%} of the final words appeared in the draft")


@pytest.mark.skipif(os.environ.get("RUN_INLINE") != "1",
                    reason="also set RUN_INLINE=1 (injects real keystrokes)")
def test_the_whole_chain_types_the_speech_into_a_real_window(transcriber, qapp):
    """Speech in, words in a real text box, corrected to the final transcription.

    This is the feature as the user experiences it, with every seam real: the
    model, the preview controller, the stabilizer, the paste worker, Win32
    injection, and the final reconciliation. It also counts the corrections,
    which is the number that says whether inline typing is pleasant or twitchy.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTextEdit

    from voiceassistant import winapi
    from voiceassistant.inline_typist import stream_target
    from voiceassistant.paste import INLINE_TYPED, Paster
    from voiceassistant.text import clean_transcript

    edit = QTextEdit()
    edit.setWindowTitle("inline dictation live gate")
    edit.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    edit.resize(700, 220)
    edit.show()
    edit.raise_()
    edit.activateWindow()
    hwnd = int(edit.winId())
    winapi.set_foreground_window(hwnd)
    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    if winapi.get_foreground_window() != hwnd:
        edit.close()
        pytest.skip("could not take the foreground — refusing to type blind")

    existing = "EXISTING USER TEXT. "
    edit.setPlainText(existing)
    cursor = edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    edit.setTextCursor(cursor)

    audio = _load("long_paragraph_trailing_silence.wav")
    rec = _FeedRecorder(audio)
    ctl = LivePreview(rec, transcriber, enabled=True, light_cleanup=True)
    paster = Paster()
    backspaces = []
    real_backspace = winapi.send_backspaces

    def counting_backspace(count, expected_hwnd=None):
        backspaces.append(count)
        return real_backspace(count, expected_hwnd=expected_hwnd)

    first_typed = []
    try:
        import unittest.mock as mock
        with mock.patch.object(winapi, "send_backspaces", counting_backspace):
            paster._begin_inline_job(hwnd, 1)
            ctl.preview_text.connect(
                lambda s, t: (paster.type_to(hwnd, 1, stream_target(s)),
                              first_typed or first_typed.append(
                                  time.perf_counter() - rec.t0)))
            ctl.begin()
            end_at = rec.t0 + len(audio) / SR + 1.0
            while time.perf_counter() < end_at:
                QCoreApplication.processEvents()
                time.sleep(0.01)
            ctl.end()
            rec.is_recording = False
            # Let the queued type jobs finish, as a real release would.
            for _ in range(200):
                QCoreApplication.processEvents()
                time.sleep(0.01)
            streamed = edit.toPlainText()

            final = clean_transcript(transcriber._run_transcribe(audio, use_vad=True),
                                     light=True)
            done = []
            paster.finalize_inline(hwnd, 1, final,
                                   lambda outcome, text: done.append(outcome))
            for _ in range(300):
                QCoreApplication.processEvents()
                time.sleep(0.01)
                if done:
                    break
            result = edit.toPlainText()
    finally:
        paster.shutdown()
        ctl.shutdown()
        edit.close()

    print(f"\n  first word in the box at {first_typed[0]:.2f}s"
          if first_typed else "\n  nothing was typed")
    print(f"  while speaking : {streamed!r}")
    print(f"  final          : {result!r}")
    print(f"  corrections    : {len(backspaces)} ({sum(backspaces)} characters)")

    assert first_typed, "no words reached the window while speaking"
    assert first_typed[0] < 3.0, f"first word took {first_typed[0]:.2f}s"
    assert streamed.startswith(existing), "the user's own text was disturbed"
    assert len(streamed) > len(existing) + 20, "barely anything was typed"
    assert done == [INLINE_TYPED], f"reconciliation did not complete: {done}"
    assert result == existing + final, "the window does not hold the final text"
    assert sum(backspaces) < len(final), (
        "more characters were deleted than the whole utterance — that reads as "
        "flickering, not correcting")


def test_final_decode_is_not_stuck_behind_drafts(transcriber):
    """Release must not wait on a queue of drafts — this is the paste latency."""
    audio = np.concatenate([_load("long_paragraph_trailing_silence.wav")] * 2)
    drafts, ctl, rec = _run_hold(transcriber, audio, speak_seconds=10.0)

    # Release: exactly what MainWindow._on_recording_stopped does, in order.
    ctl.end()
    rec.is_recording = False
    clip = audio[: 10 * SR]
    t0 = time.perf_counter()
    done = []
    transcriber.transcription_ready.connect(lambda r: done.append(r))
    transcriber.transcribe(clip, context=1234)
    deadline = t0 + 30
    while not done and time.perf_counter() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    ctl.shutdown()

    assert done, "final transcription never completed"
    print(f"\n  final decode delivered {elapsed_ms:.0f} ms after release "
          f"({len(drafts)} drafts had run)")
    # One in-flight draft may still be finishing; a QUEUE of them would not fit.
    assert elapsed_ms < 6000, (
        f"final decode took {elapsed_ms:.0f} ms — drafts are delaying the paste")
    assert done[0].text.strip(), "final decode returned nothing"
