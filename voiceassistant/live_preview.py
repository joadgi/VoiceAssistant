"""LivePreview — words on screen WHILE you speak, without touching the target.

Stage 1 of "type as I talk". The final dictation path is untouched: hold →
release → one large-v3 decode → paste. What this adds is a rolling draft of
the recording so far, decoded every few hundred milliseconds and shown on the
floating pill, so the user can see what the model is hearing before the key
comes up. Nothing here injects a keystroke or touches the clipboard.

Design, and the reasons behind it:

- **The preview shares the transcriber's loaded model and its worker.** On this
  machine the GPU sits at ~7.5 of 8 GB with the app's large-v3 resident, so a
  second model would gamble on an out-of-memory failure in the middle of a
  dictation. Reusing the model costs zero VRAM and gives the preview the same
  accuracy as the final pass. Preview jobs run on the transcriber's
  SerialWorker as a second job type (still one worker per subsystem); the
  final decode queues behind at most ONE in-flight preview because this
  controller never submits while a result is outstanding.
- **Greedy decode, no VAD, no temperature ladder.** The preview is a draft that
  is re-generated from scratch on every tick, so a bad guess lives for a few
  hundred milliseconds and is then replaced. The final pass keeps beam search
  and the no-VAD retry.
- **The whole uncommitted window is re-decoded each tick**, so earlier words
  CAN change as more context arrives — that is the behaviour the user asked
  for ("if the context changes, that needs to adjust"). Words the last two
  hypotheses agree on are rendered normally, the rest dimmed, so the eye is
  told which part is still moving.
- **Long recordings commit their oldest segments** once the live window is
  longer than `WINDOW_S`, keeping the decode bounded (Whisper's own window is
  30 s). Committed text is frozen and fed back as `initial_prompt` for
  continuity across the seam — the standard whisper-streaming trick.
- **The GUI-thread tick owns all policy** (when to decode, when to commit,
  when to stop), mirroring the recorder: the worker only decodes.
- **It fails quiet, not loud.** A preview decode error disables the preview for
  the rest of that recording and logs once; the dictation itself is unaffected.
  Logs record counts and latencies only — never the text.
"""

import re

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import applog
from .text import strip_fillers

SAMPLE_RATE = 16000

# How often the GUI tick checks whether a new preview decode is due.
TICK_MS = 100
# Don't bother decoding less than this much audio (the pre-roll is mostly
# silence and Whisper invents text on near-empty input).
MIN_AUDIO_S = 0.6
# A new decode needs at least this much fresh audio since the last one.
MIN_NEW_AUDIO_S = 0.25
# Live window management for long holds. Once the uncommitted audio exceeds
# WINDOW_S, segments ending earlier than (duration - KEEP_S) are frozen. HARD_S
# is the emergency cut when Whisper returns one long unbroken segment.
WINDOW_S = 20.0
KEEP_S = 8.0
HARD_S = 28.0
# Committed text fed back as the decode prompt (Whisper caps prompts ~224
# tokens; ~200 characters keeps well under that).
PROMPT_CHARS = 200

_WORD_STRIP = re.compile(r"[^\w']")


def _norm_word(word):
    return _WORD_STRIP.sub("", word).lower()


class PreviewStabilizer:
    """Pure logic: split the latest hypothesis into agreed and still-moving words.

    `update(text)` returns `(stable, tail)`: `stable` is the longest run of
    leading words that the previous hypothesis also produced (compared
    case/punctuation-insensitively), `tail` is the rest. The FULL latest
    hypothesis is always shown — stability only controls emphasis, so a
    correction to an early word is never hidden.
    """

    def __init__(self):
        self._prev = []

    def reset(self):
        self._prev = []

    def update(self, text):
        words = text.split()
        prev = self._prev
        n = 0
        limit = min(len(words), len(prev))
        while n < limit and _norm_word(words[n]) == _norm_word(prev[n]):
            n += 1
        self._prev = words
        return " ".join(words[:n]), " ".join(words[n:])


def choose_commit(segments, duration_s):
    """Pure logic: how many leading segments to freeze for a window of `duration_s`.

    `segments` is a list of (start_s, end_s, text) in order. Returns the count
    of leading segments to commit (0 = none). Only ever commits when the window
    has outgrown WINDOW_S, keeps the last KEEP_S live so recent words can still
    change, and past HARD_S forces progress even without a clean boundary.
    """
    if not segments or duration_s <= WINDOW_S:
        return 0
    cutoff = duration_s - KEEP_S
    n = 0
    for _start, end, _text in segments:
        if end <= cutoff:
            n += 1
        else:
            break
    if n == 0 and duration_s > HARD_S:
        # No segment ends early enough: freeze everything but the last so the
        # window shrinks; if there is only one segment, freeze it outright.
        n = max(1, len(segments) - 1)
    return n


class LivePreview(QObject):
    """GUI-thread controller for the rolling preview of the active recording."""

    # (stable_text, tail_text) — the full current draft, split for emphasis.
    preview_text = Signal(str, str)

    def __init__(self, recorder, transcriber, enabled=True, light_cleanup=True):
        super().__init__()
        self.recorder = recorder
        self.transcriber = transcriber
        self.enabled = bool(enabled)
        self.light_cleanup = bool(light_cleanup)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

        self._active = False
        self._gen = 0             # bumps on begin/end; stale results are dropped
        self._inflight = False
        self._failed = False
        self._committed_text = ""
        self._committed_frames = 0   # frames of the capture already frozen
        self._last_decoded_frames = 0
        self._stabilizer = PreviewStabilizer()
        self._latencies_ms = []

        self.transcriber.preview_ready.connect(self._on_preview_ready)

    @property
    def active(self):
        return self._active

    # ------------------------------------------------------------------ #
    @Slot()
    def begin(self):
        """A recording started — start ticking (no decode until audio exists)."""
        self._gen += 1
        self._active = self.enabled and self.transcriber.is_loaded
        self._inflight = False
        self._failed = False
        self._committed_text = ""
        self._committed_frames = 0
        self._last_decoded_frames = 0
        self._stabilizer.reset()
        self._latencies_ms = []
        if self._active:
            self._timer.start()

    @Slot()
    def end(self):
        """The recording ended — stop ticking, drop any in-flight result.

        Cancels queued preview jobs on the transcriber so the FINAL decode is
        not held behind a draft that nobody will see.
        """
        self._timer.stop()
        self._active = False
        self._gen += 1
        self._inflight = False
        self.transcriber.cancel_previews()

    def shutdown(self):
        self._timer.stop()
        self._active = False

    def stats(self):
        """Per-recording numbers for metrics: decode count + median latency."""
        lat = sorted(self._latencies_ms)
        median = lat[len(lat) // 2] if lat else None
        return {"preview_decodes": len(lat), "preview_ms": median}

    # ------------------------------------------------------------------ #
    @Slot()
    def _on_tick(self):
        if not self._active or self._inflight or self._failed:
            return
        rec = self.recorder
        if not rec.is_recording or rec.stopping:
            return
        total = rec.capture_frames()
        avail = total - self._committed_frames
        if avail < MIN_AUDIO_S * SAMPLE_RATE:
            return
        if total - self._last_decoded_frames < MIN_NEW_AUDIO_S * SAMPLE_RATE:
            return
        audio = rec.peek()
        if audio is None or len(audio) <= self._committed_frames:
            return
        window = audio[self._committed_frames:]
        prompt = self._committed_text[-PROMPT_CHARS:] if self._committed_text else ""
        self._inflight = True
        self._last_decoded_frames = total
        self.transcriber.preview(window, prompt, self._gen, self._committed_frames)

    @Slot(object)
    def _on_preview_ready(self, result):
        if result.gen != self._gen or not self._active:
            return  # a draft for a recording that is over
        self._inflight = False
        if result.error:
            # Quiet failure: the dictation still works, the draft just stops.
            self._failed = True
            applog.info(f"live preview disabled for this recording: {result.error}")
            return
        self._latencies_ms.append(float(result.latency_ms))

        segments = list(result.segments)
        duration_s = result.n_frames / SAMPLE_RATE
        n_commit = choose_commit(segments, duration_s)
        if n_commit:
            frozen = " ".join(s[2] for s in segments[:n_commit]).strip()
            if frozen:
                # Frozen text gets the SAME cleanup as the live half. Without
                # this, everything before a commit seam kept its fillers while
                # the final transcription strips them, so the final
                # reconciliation had to rewrite the sentence from the seam
                # onward — on a long dictation that exceeds the correction
                # limit and the draft is abandoned in the user's document.
                if self.light_cleanup:
                    frozen = strip_fillers(frozen).strip()
                self._committed_text = (self._committed_text + " " + frozen).strip()
            self._committed_frames = result.offset_frames + int(
                segments[n_commit - 1][1] * SAMPLE_RATE
            )
            segments = segments[n_commit:]
            applog.dbg(
                f"live preview committed {n_commit} segment(s); "
                f"window now starts at {self._committed_frames / SAMPLE_RATE:.1f}s"
            )

        live = " ".join(s[2] for s in segments).strip()
        if self.light_cleanup:
            live = strip_fillers(live)
        # Stabilize the WHOLE draft, not just the live window. Comparing only
        # the live half means that on the tick where a commit happens, the
        # previous hypothesis still starts with the words that were just
        # frozen, the prefix comparison misaligns, and the reported stable
        # text SHRINKS for one tick. On the pill that is a flicker; with
        # inline typing it is a delete-and-retype burst in the user's
        # document, and if the live tail is longer than the streaming
        # correction limit, typing stops silently for the rest of the
        # dictation. Committed words are stable by definition, so keeping
        # them in the comparison basis makes the seam invisible.
        full = " ".join(p for p in (self._committed_text, live) if p)
        stable, tail = self._stabilizer.update(full)
        # Committed text is settled BY DEFINITION — it is frozen and will never
        # be decoded again — so it can never be part of the moving tail, even
        # on the very first hypothesis after a commit when the stabilizer has
        # no previous run to agree with.
        if len(stable) < len(self._committed_text):
            stable = self._committed_text
            tail = full[len(stable):].strip()
        self.preview_text.emit(stable, tail)
