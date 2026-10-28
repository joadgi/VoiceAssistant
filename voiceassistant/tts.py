"""TTSEngine — local/online neural voices via VLC, explicit SAPI option.

Phase 3 rework:
  * ONE owned SerialWorker — utterances serialize; two workers can never
    drive the shared VLC player at once (the old overlap garble).
  * Per-utterance GENERATION: speak() during speech stops the current
    utterance and queues the new one (no toggle surprise, no drop). Each job
    carries its own stop-Event — a stale job can never be "un-stopped" by a
    newer call (the old shared-event clear() race).
  * Temp hygiene: per-utterance MP3 names, deleted after playback; the temp
    dir and VLC objects are released on shutdown().
  * Network hardening (Phase 0, kept): bounded synthesis waits — a stalled
    stream raises honestly instead of wedging or changing the selected voice.

Gapless-buffer rework (2026-08-27): one selected-text job now means exactly one
edge-tts Communicate stream and one VLC playback session. Splitting a selection
into separately synthesized mini-MP3s introduced audible encoder/prosody seams,
and canceling them left cloud requests draining behind the next read. VLC starts
after a protected lead while the same stream continues producing. The callback
may block for more data because VLC reads ahead; an empty Python queue is not
evidence that the speakers have run dry.

Stop/voice rework (2026-08-26) — the two user-visible bugs turned out to
share one root cause. See the STOP CONTRACT and VOICE CONTRACT below.

The TTS SerialWorker owns both local Kokoro inference and edge-tts's asyncio
loop. LibVLC's native media
callback consumes the in-memory stream while that same worker continues the
single async request; there is no second Python producer thread to outlive or
overlap the next utterance.
"""

import asyncio
import collections
import html
import os
import re
import shutil
import tempfile
import threading
import time
import queue as _queue

from PySide6.QtCore import QObject, Signal

from . import applog
from .workers import SerialWorker

# Modern Microsoft neural voices (available via edge-tts)
NEURAL_VOICES = [
    # Male — modern (Copilot-style, most natural)
    ("Andrew (Male, US) - Warm", "en-US-AndrewNeural"),
    ("Brian (Male, US) - Casual", "en-US-BrianNeural"),
    ("Christopher (Male, US) - News", "en-US-ChristopherNeural"),
    ("Eric (Male, US) - Rational", "en-US-EricNeural"),
    ("Guy (Male, US) - Passionate", "en-US-GuyNeural"),
    ("Roger (Male, US) - Lively", "en-US-RogerNeural"),
    ("Steffan (Male, US) - Calm", "en-US-SteffanNeural"),
    # Female
    ("Emma (Female, US) - Cheerful", "en-US-EmmaNeural"),
    ("Ava (Female, US) - Friendly", "en-US-AvaNeural"),
    ("Jenny (Female, US) - Friendly", "en-US-JennyNeural"),
    ("Aria (Female, US) - Expressive", "en-US-AriaNeural"),
    ("Michelle (Female, US) - Friendly", "en-US-MichelleNeural"),
    # British
    ("Ryan (Male, UK) - Clear", "en-GB-RyanNeural"),
    ("Sonia (Female, UK) - Professional", "en-GB-SoniaNeural"),
    # Australian
    ("William (Male, AU)", "en-AU-WilliamNeural"),
    ("Natasha (Female, AU)", "en-AU-NatashaNeural"),
]

# Fast local neural voices. Unlike edge-tts these synthesize on this machine,
# so playback cannot hiccup when Microsoft's service runs slower than audio.
LOCAL_NEURAL_VOICES = [
    ("Michael (Male, US) - Warm", "am_michael"),
    ("Adam (Male, US) - Clear", "am_adam"),
    ("Eric (Male, US) - Calm", "am_eric"),
    ("Liam (Male, US) - Natural", "am_liam"),
    ("Heart (Female, US) - Warm", "af_heart"),
    ("Sarah (Female, US) - Clear", "af_sarah"),
]

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
KOKORO_MODEL_PATH = os.path.join(
    _PROJECT_ROOT, "models", "kokoro", "kokoro-v1.0.fp16.onnx"
)
KOKORO_VOICES_PATH = os.path.join(
    _PROJECT_ROOT, "models", "kokoro", "voices-v1.0.bin"
)
LOCAL_PCM_SAMPLE_RATE = 24_000
LOCAL_MAX_SPEED = 2.6
# Kokoro starts mutating or swallowing words when it is asked to *generate*
# close to 2x. On the user's exact 919-character report excerpt, a local
# Whisper audit recovered 99.1% of words at model input 1.0, but only 84.0% at
# 1.98. Model input 1.2 followed by FFmpeg's pitch-preserving atempo filter at
# an effective 1.98x recovered 99.1%. The model's measured duration multiplier
# at input 1.2 is about 1.15 once punctuation pauses are included.
LOCAL_QUALITY_MODEL_SPEED = 1.2
LOCAL_QUALITY_EFFECTIVE_SPEED = 1.15
# ONNX Runtime's default is one spinning worker per physical core (16 here).
# Measured 2026-10-20 on this 16-core Ryzen with another program loading the
# CPU, mean synthesis time per second of audio over 9 blocks each: default
# 0.50, 16 threads without spinning 0.41, 8 threads without spinning 0.37.
# Spinning workers burn cores while idle, and on a starved CPU those are the
# cores our own audio threads need, so spinning stays off.
KOKORO_INTRA_OP_THREADS = max(1, min(8, (os.cpu_count() or 2) // 2))


def _neural_meta():
    """{neural id: (locale, "male"|"female")} parsed from the NEURAL_VOICES labels.

    Used ONLY to pick the closest SAPI voice when the neural path fails, so a
    fallback at least keeps the gender/locale the user chose.
    """
    meta = {}
    for name, vid in NEURAL_VOICES:
        gender = "female" if "(Female" in name else "male"
        locale = "-".join(vid.split("-")[:2])  # en-US-AndrewNeural -> en-US
        meta[vid] = (locale, gender)
    return meta


NEURAL_META = _neural_meta()

BASE_RATE_WPM = 175  # SAPI/offline base words-per-minute; scaled by _speed


_MARKDOWN_LINK_RE = re.compile(
    r"!?\[([^\]]*)\]\(\s*(?:<[^>]*>|[^)\r\n]*)\s*\)"
)
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>])")
_MARKDOWN_NUMBERED_ITEM_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
_MARKDOWN_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_CURRENCY_RE = re.compile(r"\$\s*(\d[\d,]*)(?:\.(\d{2}))?")
_ALPHANUMERIC_ID_RE = re.compile(
    r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,}\b"
)


def _speak_currency(match):
    """Turn a display price into words with unambiguous units."""
    dollars = int(match.group(1).replace(",", ""))
    cents_text = match.group(2)
    cents = int(cents_text) if cents_text is not None else None
    parts = []
    if dollars or cents in (None, 0):
        parts.append(f"{dollars} {'dollar' if dollars == 1 else 'dollars'}")
    if cents:
        parts.append(f"{cents} {'cent' if cents == 1 else 'cents'}")
    return " and ".join(parts)


def _spell_identifier(match):
    """Keep an ASIN-like identifier intelligible instead of inventing a word."""
    return " ".join(match.group(0))


def prepare_text_for_speech(text):
    """Return the words a rendered Markdown selection visibly represents.

    Read-aloud receives selections from browsers, editors, and generated reports.
    Those selections can contain Markdown source such as ``1\\.``, ``\\-`` and
    ``[label](<C:/private/path>)``. Passing that source straight to eSpeak made
    Kokoro literally say "backslash" before list items and pronounce the hidden
    link destination as invented syllables. This boundary keeps the visible label
    and prose while removing formatting that is not part of the spoken content.

    It also expands the few machine-readable forms that eSpeak/Kokoro is known
    to mispronounce: Amazon ASIN/SKU terms, mixed letter-number identifiers, and
    dollar prices. Ordinary prose, punctuation, years, and hyphenated words are
    otherwise left to the selected voice.
    """
    if not text:
        return ""

    normalized = html.unescape(str(text)).replace("\r\n", "\n").replace("\r", "\n")
    normalized = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), normalized)
    normalized = _MARKDOWN_ESCAPE_RE.sub(r"\1", normalized)
    normalized = _CURRENCY_RE.sub(_speak_currency, normalized)
    normalized = _ALPHANUMERIC_ID_RE.sub(_spell_identifier, normalized)
    # These are standard spoken forms in the Amazon workflow. Raw eSpeak turns
    # ASINs into singular "assin" and a bare SKU into letter fragments.
    normalized = re.sub(r"\bASINs\b", "A-sin identifiers", normalized)
    normalized = re.sub(r"\bASIN\b", "A-sin", normalized)
    normalized = re.sub(r"\bSKUs\b", "skews", normalized)
    normalized = re.sub(r"\bSKU\b", "skew", normalized)

    spoken_lines = []
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if line.startswith("```"):
            continue
        if not line:
            if spoken_lines and spoken_lines[-1] != "":
                spoken_lines.append("")
            continue

        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^>\s?", "", line)
        if re.fullmatch(r"[-*_]{3,}", line):
            continue

        numbered = _MARKDOWN_NUMBERED_ITEM_RE.match(line)
        bullet = _MARKDOWN_BULLET_RE.match(line)
        if numbered:
            content = numbered.group(2).strip()
            if content and content[-1] not in ".!?;:":
                content += "."
            line = f"{numbered.group(1)}. {content}"
        elif bullet:
            line = bullet.group(1).strip()
            if line and line[-1] not in ".!?;:":
                line += "."

        # Formatting delimiters are visual, not words. The boundary-aware
        # patterns preserve underscores/asterisks inside identifiers.
        line = line.replace("`", "")
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
        line = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", line)
        if line:
            spoken_lines.append(line)

    while spoken_lines and spoken_lines[-1] == "":
        spoken_lines.pop()
    return "\n".join(spoken_lines).strip()
EDGE_MP3_BYTES_PER_SECOND = 6000  # edge-tts output is 48 kbps CBR MP3


class _StreamingAudioBuffer:
    """Thread-safe byte stream between edge-tts and VLC's media callbacks.

    The producer can run far ahead without blocking. VLC consumes whatever is
    available and waits when the network falls behind. Stop/cancel wakes every
    waiter, so a custom-media read can never deadlock player.stop(). The edge-tts
    request owns the real network timeout; the consumer must not infer an audible
    underrun merely because VLC has read ahead of this queue.
    """

    def __init__(self, stop_event, cancel_event):
        self._stop_event = stop_event
        self._cancel_event = cancel_event
        self._condition = threading.Condition()
        self._chunks = collections.deque()
        self._head_offset = 0
        self._available = 0
        self._done = False
        self._eof_delivered = False
        self._error = None
        self._first_audio_at = None
        self._last_audio_at = None
        self._starvation_count = 0
        self._max_starvation_s = 0.0

    @property
    def available(self):
        with self._condition:
            return self._available

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def starvation_stats(self):
        with self._condition:
            return self._starvation_count, self._max_starvation_s

    @property
    def eof_delivered(self):
        """True once the consumer has been handed the end of the stream."""
        with self._condition:
            return self._eof_delivered

    def write(self, data):
        if not data:
            return
        now = time.monotonic()
        with self._condition:
            if self._done or self._cancelled():
                return
            self._chunks.append(bytes(data))
            self._available += len(data)
            if self._first_audio_at is None:
                self._first_audio_at = now
            self._last_audio_at = now
            self._condition.notify_all()

    def finish(self, error=None):
        with self._condition:
            self._done = True
            if error is not None:
                self._error = error
            self._condition.notify_all()

    def cancel(self):
        self._cancel_event.set()
        with self._condition:
            self._condition.notify_all()

    def wait_until_ready(self, min_bytes, first_timeout, stall_timeout):
        """Wait for the protected startup lead or the complete short stream."""
        started = time.monotonic()
        with self._condition:
            while True:
                if self._cancelled():
                    return False
                if self._available >= min_bytes or (self._done and self._available):
                    return True
                if self._done:
                    if self._error is not None:
                        raise self._error
                    raise RuntimeError("Neural TTS returned no playable audio")

                now = time.monotonic()
                if now - started >= first_timeout:
                    raise TimeoutError(
                        "Neural TTS was not ready to play within "
                        f"{first_timeout:.0f}s (network stall)"
                    )
                if self._first_audio_at is None:
                    pass
                elif now - self._last_audio_at >= stall_timeout:
                    raise TimeoutError(
                        f"Neural TTS stopped streaming for {stall_timeout:.0f}s "
                        "(network stall)"
                    )
                self._condition.wait(0.05)

    def read(self, size):
        """Return bytes, b'' for EOF, or None for stop/error.

        Called on a VLC-owned decoder thread. LibVLC aggressively reads ahead
        into its own decoder/cache, so this queue can be empty while several
        seconds of audio are still playing. Wait for the bounded edge-tts
        producer to write/finish instead of turning normal read-ahead into a
        false playback error. The 50 ms condition poll preserves the Stop
        contract even if no producer notification arrives.
        """
        waited_from = None
        with self._condition:
            while not self._available:
                if self._cancelled():
                    return None
                if self._done:
                    if self._error is not None:
                        return None
                    self._eof_delivered = True
                    return b""
                if waited_from is None:
                    waited_from = time.monotonic()
                self._condition.wait(0.05)

            if waited_from is not None:
                waited = time.monotonic() - waited_from
                self._starvation_count += 1
                self._max_starvation_s = max(self._max_starvation_s, waited)

            remaining = min(int(size), self._available)
            parts = []
            while remaining and self._chunks:
                head = self._chunks[0]
                take = min(remaining, len(head) - self._head_offset)
                parts.append(head[self._head_offset:self._head_offset + take])
                self._head_offset += take
                self._available -= take
                remaining -= take
                if self._head_offset == len(head):
                    self._chunks.popleft()
                    self._head_offset = 0
            return b"".join(parts)

    def _cancelled(self):
        return self._stop_event.is_set() or self._cancel_event.is_set()


class _PacedAudioStream:
    """Expose raw PCM to VLC at the rate represented by its sample clock.

    LibVLC's raw-audio demux aggressively read the callback to EOF. On the
    user's 941-character passage it consumed 44.959 seconds of generated PCM
    in 20.250 seconds, marked the media Ended, and let the next Speak stop the
    still-queued output. That sounded like whole sections were being skipped.

    A small lead keeps VLC fed, then reads are clocked at the PCM byte rate.
    This makes Ended line up with what has actually reached the speakers. The
    waiter is stop-aware; test clocks/waiters are injectable so the duration
    contract can be pinned without making the suite sleep in real time.

    The clock deliberately keeps running through a PAUSE. Freezing it looks
    tidier and was measured worse: VLC shifts its own clock by the pause, so
    data that also arrived late counted the pause twice and VLC sat silent
    after resume for about as long as it had been paused (1.8 s after a 2 s
    pause). Left running, VLC buffers ahead while paused -- 10 s during a 10 s
    pause, then played every sample at 1x with nothing skipped.
    """

    def __init__(
        self,
        source,
        stop_event,
        sample_rate=LOCAL_PCM_SAMPLE_RATE,
        channels=1,
        bytes_per_sample=2,
        lead_seconds=0.25,
        frame_seconds=0.05,
        clock=None,
        waiter=None,
    ):
        self._source = source
        self._stop_event = stop_event
        self._bytes_per_frame = int(channels) * int(bytes_per_sample)
        self._bytes_per_second = int(sample_rate) * self._bytes_per_frame
        self._lead_seconds = float(lead_seconds)
        self._frame_bytes = max(
            self._bytes_per_frame,
            int(self._bytes_per_second * float(frame_seconds)),
        )
        self._clock = clock or time.monotonic
        self._waiter = waiter or stop_event.wait
        self._started_at = None
        self._sent = 0

    @property
    def error(self):
        return self._source.error

    def read(self, size):
        requested = max(self._bytes_per_frame, int(size))
        threshold = min(requested, self._frame_bytes)
        if self._started_at is None:
            self._started_at = self._clock()

        while not self._stop_event.is_set():
            elapsed = max(0.0, self._clock() - self._started_at)
            permitted = int(
                (elapsed + self._lead_seconds) * self._bytes_per_second
            )
            allowed = permitted - self._sent
            if allowed >= threshold:
                take = min(requested, allowed)
                take -= take % self._bytes_per_frame
                data = self._source.read(take)
                if data:
                    self._sent += len(data)
                return data

            missing = threshold - allowed
            delay = min(0.02, max(0.002, missing / self._bytes_per_second))
            self._waiter(delay)

        return None


CPU_BUSY_HINT = (
    "The CPU is too busy - check Task Manager for a program using most of it."
)


class _SynthesisPace:
    """Track whether local synthesis stays ahead of the speakers.

    Kokoro runs on the CPU, so a program that saturates it starves read-aloud.
    Measured 2026-10-20: with a runaway DisplayFusion holding 20-33 of 32
    logical cores, blocks took 1.1-1.6x their own playing time to generate,
    against ~0.3x normally. Of 970 logged blocks, 888 generated in under 0.5x
    and every block at or over 1.0x fell inside a CPU-contention episode. The
    stream ran dry between batches, the user heard stutter, and nothing in the
    app said why.

    Pure arithmetic on durations the caller measures, so it is testable
    without a clock. ``played_s`` is wall time since playback started, or None
    for the block that starts playback.
    """

    AUDIBLE_GAP_S = 0.5     # a dry spell this long is a pause the user hears
    MIN_SLOW_START_S = 1.0  # a sub-second phrase starting late is not news

    def __init__(self):
        self.blocks = 0
        self.audio_s = 0.0
        self.worst_ratio = 0.0
        self.gap_s = 0.0
        self._dry_at_s = 0.0  # playback time at which queued audio runs out

    def add(self, synth_s, audio_s, played_s=None):
        """Record one block; return a user-facing reason if it fell behind."""
        self.blocks += 1
        self.audio_s += audio_s
        ratio = synth_s / audio_s if audio_s > 0 else 0.0
        self.worst_ratio = max(self.worst_ratio, ratio)
        if played_s is None:
            self._dry_at_s = audio_s
            if synth_s >= self.MIN_SLOW_START_S and ratio >= 1.0:
                return (
                    f"Read-aloud started late: speech took {ratio:.1f}x "
                    "longer to generate than to play."
                )
            return None
        gap = played_s - self._dry_at_s
        self._dry_at_s = max(self._dry_at_s, played_s) + audio_s
        if gap > 0:
            self.gap_s += gap
        if gap >= self.AUDIBLE_GAP_S:
            return (
                f"Read-aloud paused {gap:.1f}s waiting for speech to be "
                "generated."
            )
        return None


class _PauseState:
    """Pause bookkeeping for ONE utterance: the GUI thread writes, the worker reads.

    ``backend`` is set once audio is actually playing ("vlc" or "sapi"); before
    that there is nothing to hold. ``was_paused`` outlives resume because LibVLC
    never reports Ended for a raw callback stream that was really paused -- 12
    of 12 paused PCM runs on 2026-10-28 stayed Playing forever, across the
    mmdevice, directsound and waveout outputs and both ways of resuming -- so
    such a read's end is detected from its own playback clock instead (see
    ``_wait_vlc_stream``). ``seconds()`` is the time spent paused, which any
    "how long has this been playing" arithmetic must leave out.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._since = None
        self._total = 0.0
        self.backend = None
        self.was_paused = False

    @property
    def live(self):
        return self.backend is not None

    @property
    def paused(self):
        return self._since is not None

    def pause(self):
        with self._lock:
            if self._since is not None:
                return False
            self._since = time.monotonic()
            self.was_paused = True
            return True

    def resume(self):
        with self._lock:
            if self._since is None:
                return False
            self._total += time.monotonic() - self._since
            self._since = None
            return True

    def seconds(self):
        with self._lock:
            ongoing = time.monotonic() - self._since if self._since is not None else 0.0
            return self._total + ongoing


class _UtteranceStop(threading.Event):
    """The stop event of one utterance, carrying that utterance's pause state.

    Every worker-side function already receives the stop event, and each
    utterance has exactly one, so the pause state rides on it rather than on
    the engine: a job still unwinding can never see the NEXT read's state.
    """

    def __init__(self):
        super().__init__()
        self.pause = _PauseState()


def _paused_seconds(stop_event):
    state = getattr(stop_event, "pause", None)
    return state.seconds() if state is not None else 0.0


class TTSEngine(QObject):
    """Local/online neural TTS plus an explicit Windows SAPI option."""

    speaking_started = Signal()
    speaking_finished = Signal()
    status = Signal(str)
    error = Signal(str)
    # Local synthesis fell behind playback (see _SynthesisPace). At most once
    # per utterance. Not an error: the read continues, just with pauses.
    falling_behind = Signal(str)
    # Audio of the current utterance is actually playing, so it can pause.
    playback_started = Signal()
    paused_changed = Signal(bool)
    FIRST_AUDIO_TIMEOUT = 6.0   # chosen neural voice gets a bounded startup window
    SYNTHESIS_STALL_TIMEOUT = 10.0  # no more bytes while buffering -> honest error
    START_BUFFER_PLAY_SECONDS = 3.0  # long reads: absorb jitter at high playback speeds
    SHORT_START_BUFFER_PLAY_SECONDS = 1.0  # short reads must begin promptly
    SHORT_TEXT_MAX_CHARS = 600
    NEURAL_RETRIES = 1          # one cheap neural retry for a fast service hiccup
    RETRY_PAUSE_S = 0.4
    # A read that was paused ends when its stream is exhausted and VLC's media
    # time has stood still this long (VLC never says Ended for it; see
    # _PauseState). Measured: get_time() advances every ~0.25-0.3 s while
    # audio plays, and an unpaused read reaches Ended ~0.45 s after EOF.
    PAUSED_END_STALL_S = 0.6

    def __init__(self, volume=1.0):
        super().__init__()
        self._speed = 1.0  # playback speed multiplier (0.5 to 3.0)
        self._volume = volume
        self._speaking = False
        self._gen = 0                     # utterance generation counter
        self._active_stop = None          # stop Event of the CURRENT utterance
        self._voice_id = "en-US-AndrewNeural"
        self._sapi_voice_id = None        # set ONLY when the user picks a SAPI voice
        self._local_voice_id = "am_michael"
        self._use_local = False
        self._kokoro = None
        self._kokoro_load_queued = False
        self._temp_dir = tempfile.mkdtemp(prefix="voiceassist_")
        self._worker = SerialWorker("tts")

        # The neural asyncio task runs on the TTS SerialWorker. stop() runs on
        # the GUI thread and uses these references only to schedule task.cancel
        # onto that loop; it never waits for the network on the GUI thread.
        self._async_lock = threading.Lock()
        self._active_async_loop = None
        self._active_async_task = None

        # VLC instance for real-time speed playback.
        # _vlc_lock closes the stop()/play() inversion: stop() runs on the GUI
        # thread and could land between set_media() and play() on the worker,
        # stopping a player that had not started yet — the new chunk then
        # played on regardless and Stop looked ignored.
        self._vlc_lock = threading.RLock()
        self._vlc_instance = None
        self._vlc_player = None
        self._vlc_callbacks = None       # keep custom-media callbacks alive
        self._init_vlc()

        # pyttsx3 powers user-selected offline voices. NOTE pyttsx3.init() returns a
        # PROCESS-CACHED engine per driver name, so this object and every later
        # init() are the SAME engine — _offline_lock keeps two utterances off it.
        self._offline_lock = threading.RLock()
        self._pyttsx_engine = None
        self._use_offline = False
        try:
            import pyttsx3
            self._pyttsx_engine = pyttsx3.init()
            self._pyttsx_engine.setProperty("rate", int(BASE_RATE_WPM * self._speed))
            self._pyttsx_engine.setProperty("volume", self._volume)
        except Exception:
            pass

    def _init_vlc(self):
        try:
            import vlc
            self._vlc_instance = vlc.Instance("--no-video", "--quiet")
            self._vlc_player = self._vlc_instance.media_player_new()
        except Exception as e:
            self.error.emit(f"VLC init failed: {e}")

    @property
    def is_speaking(self):
        return self._speaking

    @property
    def is_paused(self):
        state = getattr(self._active_stop, "pause", None)
        return bool(self._speaking and state is not None and state.paused)

    @property
    def can_pause(self):
        """True while the current read's audio is playing or paused."""
        state = getattr(self._active_stop, "pause", None)
        return bool(self._speaking and state is not None and state.live)

    def get_voices(self):
        """Return local neural, online neural, then explicit offline voices."""
        voices = [
            (f"kokoro:{vid}", f"[Local Neural] {name}")
            for name, vid in LOCAL_NEURAL_VOICES
        ]
        voices.extend(
            (vid, f"[Online Neural] {name}") for name, vid in NEURAL_VOICES
        )
        if self._pyttsx_engine:
            try:
                for v in self._pyttsx_engine.getProperty("voices"):
                    short = v.name.split(" - ")[0] if " - " in v.name else v.name
                    voices.append((f"sapi:{v.id}", f"[Offline] {short}"))
            except Exception:
                pass
        return voices

    def set_voice(self, voice_id):
        if voice_id.startswith("kokoro:"):
            self._use_local = True
            self._use_offline = False
            self._sapi_voice_id = None
            self._local_voice_id = voice_id[7:]
            self._voice_id = voice_id
            # Do not retain a number the local backend cannot honor.
            self._speed = min(self._speed, LOCAL_MAX_SPEED)
            if self._kokoro is None and not self._kokoro_load_queued:
                self._kokoro_load_queued = True
                self._worker.submit(self._load_kokoro)
        elif voice_id.startswith("sapi:"):
            self._use_local = False
            self._use_offline = True
            self._sapi_voice_id = voice_id[5:]
            self._voice_id = self._sapi_voice_id
            if self._pyttsx_engine:
                self._pyttsx_engine.setProperty("voice", self._voice_id)
        else:
            self._use_local = False
            self._use_offline = False
            self._sapi_voice_id = None
            self._voice_id = voice_id

    def set_speed(self, speed):
        """Set the requested speech-rate multiplier.

        Local neural audio is synthesized at a quality-safe model rate, then
        pitch-preserving tempo compression supplies higher requested speeds.
        VLC's rate control is intentionally not used for its raw PCM callback
        stream: live testing proved that VLC accepted ``set_rate(2.1)`` but
        still played that stream at 1.0x. A local change therefore affects the
        next read.
        Online MP3 and explicit SAPI voices retain their live backend control.
        """
        max_speed = LOCAL_MAX_SPEED if self._use_local else 3.0
        self._speed = max(0.5, min(max_speed, float(speed)))
        if self._vlc_player and self._speaking and not self._use_local:
            try:
                self._vlc_player.set_rate(self._speed)
            except Exception:
                pass
        if self._pyttsx_engine:
            try:
                self._pyttsx_engine.setProperty("rate", int(BASE_RATE_WPM * self._speed))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Speak / stop
    #
    # STOP CONTRACT: stop() must silence audio within ~100 ms from ANY state —
    # neural streaming, VLC playback, or explicitly selected SAPI — and must do so
    # WITHOUT blocking the GUI thread. Every wait on the worker side is bounded
    # and re-checks the job's stop event; nothing here may call a blocking
    # third-party "speak until finished" API.
    #
    # `pyttsx3.runAndWait()` was exactly such an API: it blocks inside the SAPI
    # driver loop until the whole text has been spoken, so on the fallback path
    # Stop did NOTHING — audio ran to the end while the UI already said it had
    # stopped, and is_speaking went False so the next hotkey press started a
    # SECOND utterance behind the first. That is the "stop button doesn't
    # always work" report, and because it only bites on the fallback path it
    # always arrived together with the wrong-voice bug below.
    # ------------------------------------------------------------------ #
    def speak(self, text):
        """Speak text. If something is already playing, it is stopped and the
        new utterance plays — callers wanting toggle behavior check
        is_speaking themselves (the old embedded toggle silently DROPPED a
        new OCR capture while busy)."""
        spoken_text = prepare_text_for_speech(text)
        if not spoken_text:
            return
        if spoken_text != text:
            applog.dbg(
                "TTS display markup normalized "
                f"(input_chars={len(text)}, spoken_chars={len(spoken_text)})"
            )
        self.stop()  # no-op when idle
        self._gen += 1
        stop_event = _UtteranceStop()
        self._active_stop = stop_event
        self._speaking = True
        self._worker.submit(self._speak_job, spoken_text, self._gen, stop_event)

    def stop(self):
        """Stop the current utterance immediately.

        Order matters: the stop event is set FIRST so no worker-side wait can
        start new audio behind our back, and only then is each backend told to
        shut up. Runs on the GUI thread — every call here is non-blocking or
        bounded to a couple of milliseconds.
        """
        active = self._active_stop
        if active is not None:
            active.set()
        with self._async_lock:
            loop = self._active_async_loop
            task = self._active_async_task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except (RuntimeError, AttributeError):
                pass
        self._vlc_stop()
        self._stop_offline()
        self._speaking = False
        self._release_pause(active)

    # ------------------------------------------------------------------ #
    # Pause / resume
    #
    # Each backend's OWN pause holds the audio exactly where it is: VLC's
    # set_pause (silent within ~50 ms, measured) or SAPI's SpVoice.Pause. A
    # read can only pause once its audio is playing (`playback_started`); the
    # lead-in while the first block is generated has nothing to hold yet.
    # Stop still works from a paused read, and a stopped or finished read is
    # never left paused.
    # ------------------------------------------------------------------ #
    def pause(self):
        """Hold the current read where it is. Returns True if it paused."""
        stop = self._active_stop
        state = getattr(stop, "pause", None)
        if (state is None or stop.is_set() or not self._speaking
                or not state.live or state.paused):
            return False
        if not self._hold(state.backend, True):
            return False
        state.pause()
        applog.dbg(f"read-aloud paused ({state.backend})")
        self.paused_changed.emit(True)
        return True

    def resume(self):
        """Continue a paused read. Returns True if it resumed."""
        stop = self._active_stop
        state = getattr(stop, "pause", None)
        if state is None or not state.paused or not self._hold(state.backend, False):
            return False
        state.resume()
        applog.dbg(f"read-aloud resumed (paused {state.seconds():.1f}s in total)")
        self.paused_changed.emit(False)
        return True

    def toggle_pause(self):
        """Pause, or resume a paused read. Returns True if anything changed."""
        return self.resume() if self.is_paused else self.pause()

    def _hold(self, backend, paused):
        """Pause or resume the backend that is playing. False if it refused."""
        try:
            if backend == "sapi":
                voice = self._sapi_voice()
                if voice is None:
                    return False
                if paused:
                    voice.Pause()
                else:
                    voice.Resume()
                return True
            with self._vlc_lock:
                if self._vlc_player is None:
                    return False
                self._vlc_player.set_pause(1 if paused else 0)
            return True
        except Exception:
            applog.exception("read-aloud pause/resume failed")
            return False

    def _sapi_voice(self):
        """pyttsx3's underlying SAPI SpVoice, or None when the driver hides it."""
        driver = getattr(getattr(self._pyttsx_engine, "proxy", None), "_driver", None)
        return getattr(driver, "_tts", None)

    def _release_pause(self, stop_event):
        """A stopped or finished read must not stay paused. SAPI keeps a pause
        COUNT on the one shared voice: purged but left paused, the next
        offline read would never make a sound."""
        state = getattr(stop_event, "pause", None)
        if state is None or not state.resume():
            return
        if state.backend == "sapi":
            self._hold("sapi", False)
        self.paused_changed.emit(False)

    def _go_live(self, stop_event, backend):
        """The worker reports that this utterance's audio is now playing."""
        if stop_event.is_set():
            return
        state = getattr(stop_event, "pause", None)
        if state is not None and state.backend is None:
            state.backend = backend
            self.playback_started.emit()

    def _stop_offline(self):
        """Purge SAPI's queue and cut the current utterance.

        `Engine.stop()` clears the proxy queue and calls the driver's stop,
        which issues SAPI `Speak("", SVSFPurgeBeforeSpeak)` — audio ceases at
        once. The worker's own loop then sees the stop event and unwinds.
        """
        engine = self._pyttsx_engine
        if engine is None:
            return
        try:
            engine.stop()
        except Exception:
            applog.exception("offline TTS stop failed")

    def shutdown(self):
        """Full teardown for app exit: stop, drain the worker, release VLC,
        remove the temp dir."""
        self.stop()
        self._worker.shutdown()
        with self._vlc_lock:
            try:
                if self._vlc_player is not None:
                    self._vlc_player.release()
                if self._vlc_instance is not None:
                    self._vlc_instance.release()
            except Exception:
                pass
            self._vlc_player = None
            self._vlc_instance = None
            self._vlc_callbacks = None
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Worker side
    # ------------------------------------------------------------------ #
    def _is_current(self, gen, stop_event):
        return gen == self._gen and not stop_event.is_set()

    def _speak_job(self, text, gen, stop_event):
        if not self._is_current(gen, stop_event):
            return  # superseded while queued
        self.speaking_started.emit()
        # Chunks actually PLAYED in the user's chosen voice. The old code had
        # no such notion, so a failure three sentences in re-read the WHOLE
        # text through SAPI: you heard the opening twice, the second time
        # robotic. Anything already played means truncate, never re-read.
        progress = []
        try:
            if self._use_local:
                self.status.emit("Generating local neural speech...")
                self._speak_local(text, stop_event, progress)
            elif self._use_offline:
                self.status.emit("Using offline TTS...")
                self._speak_offline(text, stop_event)
            else:
                self.status.emit("Generating neural speech...")
                self._speak_neural(text, gen, stop_event, progress)
            if self._is_current(gen, stop_event):
                self.status.emit("Speech complete")
        except Exception as e:
            if stop_event.is_set():
                pass  # user-initiated; not a failure
            elif progress:
                applog.exception("TTS failed mid-utterance")
                self.status.emit("Speech cut short (playback error)")
                self.error.emit(f"TTS error: {e}")
            elif not self._use_offline and not self._use_local:
                # A selected neural voice is a hard voice choice. Silently
                # changing Andrew/Emma/etc. into Windows SAPI is experienced as
                # the app choosing the wrong voice. Keep the job in the chosen
                # voice or fail honestly; SAPI runs only when the user selects
                # an offline voice explicitly.
                applog.error(f"selected neural TTS failed: {e}")
                self.status.emit("Chosen neural voice unavailable")
                self.error.emit(f"TTS error: chosen neural voice unavailable ({e})")
            else:
                self.error.emit(f"TTS error: {e}")
        finally:
            self._release_pause(stop_event)
            if gen == self._gen:
                self._speaking = False
                self._active_stop = None
            self.speaking_finished.emit()

    # ------------------------------------------------------------------ #
    # CHOSEN VOICE CONTRACT
    #
    # A neural selection is a hard choice, not a preference that the engine may
    # silently replace with Windows SAPI. Fast neural failures get one neural
    # retry; stalls and repeated failures surface an honest error. SAPI speaks
    # only when the user explicitly selects an offline voice in the dropdown.
    # ------------------------------------------------------------------ #

    def _load_kokoro(self):
        """Load the local neural model on the TTS worker, once.

        CPU is deliberate on this installation: the measured real-time factor
        is ~0.3 (more than 3x faster than playback), while the available ONNX
        GPU wheel expects a newer CUDA runtime than Whisper uses. Forcing CPU
        avoids a noisy failed-GPU probe without sacrificing the speed contract.
        """
        if self._kokoro is not None:
            self._kokoro_load_queued = False
            return self._kokoro
        try:
            if not os.path.isfile(KOKORO_MODEL_PATH):
                raise RuntimeError(
                    f"local neural model is missing ({KOKORO_MODEL_PATH})"
                )
            if not os.path.isfile(KOKORO_VOICES_PATH):
                raise RuntimeError(
                    f"local neural voices are missing ({KOKORO_VOICES_PATH})"
                )

            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            options = ort.SessionOptions()
            options.log_severity_level = 3
            options.intra_op_num_threads = KOKORO_INTRA_OP_THREADS
            options.add_session_config_entry(
                "session.intra_op.allow_spinning", "0"
            )
            session = ort.InferenceSession(
                KOKORO_MODEL_PATH,
                providers=["CPUExecutionProvider"],
                sess_options=options,
            )
            self._kokoro = Kokoro.from_session(session, KOKORO_VOICES_PATH)
            applog.info("local neural TTS ready (Kokoro, CPU)")
            return self._kokoro
        finally:
            self._kokoro_load_queued = False

    @staticmethod
    def _create_local_audio(engine, text, voice, speed):
        """Create intelligible Kokoro audio at the requested playback speed."""
        model_speed, tempo_factor = TTSEngine._local_speed_plan(speed)
        voice_style, _phonemes, batches = engine._prepare(
            text,
            voice,
            model_speed,
            "en-us",
            False,
            0.25,
            0.1,
        )
        audio, _timings = engine._create_batches(
            batches,
            voice_style,
            model_speed,
            True,
        )
        audio = TTSEngine._time_stretch_local_audio(audio, tempo_factor)
        return audio, LOCAL_PCM_SAMPLE_RATE

    @staticmethod
    def _local_speed_plan(requested_speed):
        """Return ``(safe Kokoro input, pitch-preserving tempo factor)``."""
        requested = max(0.5, min(LOCAL_MAX_SPEED, float(requested_speed)))
        if requested <= LOCAL_QUALITY_MODEL_SPEED:
            return requested, 1.0
        return (
            LOCAL_QUALITY_MODEL_SPEED,
            requested / LOCAL_QUALITY_EFFECTIVE_SPEED,
        )

    @staticmethod
    def _time_stretch_local_audio(samples, tempo_factor):
        """Tempo-compress mono float PCM with FFmpeg while preserving pitch.

        PyAV is already a faster-whisper dependency. This runs synchronously on
        the one TTS SerialWorker and creates no process or Python thread.
        """
        import av
        import numpy as np
        from fractions import Fraction

        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0 or tempo_factor <= 1.001:
            return audio

        frame = av.AudioFrame.from_ndarray(
            audio.reshape(1, -1), format="flt", layout="mono"
        )
        frame.sample_rate = LOCAL_PCM_SAMPLE_RATE
        frame.time_base = Fraction(1, LOCAL_PCM_SAMPLE_RATE)

        graph = av.filter.Graph()
        source = graph.add_abuffer(
            sample_rate=LOCAL_PCM_SAMPLE_RATE,
            format="flt",
            layout="mono",
            channels=1,
            time_base=Fraction(1, LOCAL_PCM_SAMPLE_RATE),
        )
        tempo = graph.add("atempo", f"{tempo_factor:.6f}")
        sink = graph.add("abuffersink")
        source.link_to(tempo)
        tempo.link_to(sink)
        graph.configure()
        source.push(frame)
        source.push(None)

        output = []
        while True:
            try:
                output.append(sink.pull().to_ndarray().reshape(-1))
            except (BlockingIOError, EOFError):
                break
        if not output:
            raise RuntimeError("Local speech tempo filter returned no audio")
        return np.concatenate(output).astype(np.float32, copy=False)

    def _speak_local(self, text, stop_event, progress):
        """Synthesize local neural PCM ahead of one continuous VLC stream.

        The complete selection is phonemized and divided exactly once by
        Kokoro's native punctuation-aware batcher. Streaming those prepared
        batches preserves Kokoro's sentence/clause pauses; the old extra
        180-character splitter erased the pause at every artificial boundary
        and made a 5,553-character read jump across 39 separate voice segments.

        Speed is snapshotted once for the utterance. Reading ``self._speed``
        per generated block created a mixed-rate queue when the slider moved:
        one live trace contained 2.60x, 2.50x, 2.20x, and 1.90x blocks in the
        same playback stream.
        No network, MP3 decoder boundary, or second Python thread is involved.

        Each block is timed against playback. When synthesis falls behind (the
        CPU is saturated by another program), ``falling_behind`` fires once so
        the stutter arrives with its cause instead of looking like a TTS bug.
        """
        import numpy as np

        engine = self._load_kokoro()
        started_at = time.monotonic()
        requested_speed = min(self._speed, LOCAL_MAX_SPEED)
        model_speed, tempo_factor = self._local_speed_plan(requested_speed)
        voice_style, _phonemes, batches = engine._prepare(
            text,
            self._local_voice_id,
            model_speed,
            "en-us",
            False,
            0.25,
            0.1,
        )
        if not batches or stop_event.is_set():
            return

        cancel_event = threading.Event()
        stream = _StreamingAudioBuffer(stop_event, cancel_event)
        pace = _SynthesisPace()
        warned = False
        started = False
        playback_started_at = None
        error = None
        try:
            for index, (phonemes, pause_after) in enumerate(batches):
                if stop_event.is_set():
                    break
                synth_started = time.monotonic()
                samples, _edges = engine._create_batch(
                    phonemes,
                    voice_style,
                    model_speed,
                    True,
                    pause_after,
                )
                samples = self._time_stretch_local_audio(samples, tempo_factor)
                pcm = (
                    np.clip(np.asarray(samples).reshape(-1), -1.0, 1.0)
                    * 32767.0
                ).astype("<i2", copy=False).tobytes()
                ready_at = time.monotonic()
                synth_s = ready_at - synth_started
                audio_s = len(pcm) / 2 / LOCAL_PCM_SAMPLE_RATE
                applog.dbg(
                    "local neural block ready "
                    f"(block={index + 1}/{len(batches)}, phonemes={len(phonemes)}, "
                    f"requested={requested_speed:.2f}x, model_input={model_speed:.2f}, "
                    f"tempo={tempo_factor:.2f}x, "
                    f"pause={pause_after:.2f}s, "
                    f"audio={audio_s:.2f}s, "
                    f"synth={synth_s:.2f}s)"
                )
                # Paused time is not playing time: VLC holds the audio while
                # synthesis carries on, so counting the pause would report a
                # dry spell that never happened (and blame the CPU for it).
                behind = pace.add(
                    synth_s,
                    audio_s,
                    None if playback_started_at is None
                    else ready_at - playback_started_at - _paused_seconds(stop_event),
                )
                if behind and not warned and not stop_event.is_set():
                    warned = True
                    applog.info(
                        "local neural synthesis behind playback "
                        f"(block={index + 1}/{len(batches)}, "
                        f"worst_synth_ratio={pace.worst_ratio:.2f}, "
                        f"est_gap={pace.gap_s:.2f}s)"
                    )
                    self.falling_behind.emit(f"{behind} {CPU_BUSY_HINT}")
                stream.write(pcm)
                if not started and pcm and not stop_event.is_set():
                    applog.dbg(
                        "local neural playback ready "
                        f"(chars={len(text)}, batches={len(batches)}, "
                        f"wait={time.monotonic() - started_at:.2f}s)"
                    )
                    self.status.emit(f"Playing locally at {requested_speed:.2f}x...")
                    self._start_vlc_pcm_stream(stream, stop_event, progress)
                    started = not stop_event.is_set()
                    if started:
                        playback_started_at = time.monotonic()
        except Exception as exc:
            error = exc
        finally:
            stream.finish(error)

        if stop_event.is_set():
            stream.cancel()
            self._log_local_outcome(
                "stopped", stream, pace, len(batches), playback_started_at,
                _paused_seconds(stop_event),
            )
            return
        if error is not None and not started:
            raise error
        if not started:
            raise RuntimeError("Local neural TTS returned no playable audio")
        self._wait_vlc_stream(stream, stop_event)
        self._log_local_outcome(
            "stopped" if stop_event.is_set() else "complete",
            stream, pace, len(batches), playback_started_at,
            _paused_seconds(stop_event),
        )
        if error is not None:
            raise error

    @staticmethod
    def _log_local_outcome(outcome, stream, pace, total_blocks, playback_started_at,
                           paused_s=0.0):
        """One summary line per local read, including reads the user stopped.

        This used to say "complete" for a read the user STOPPED: a 167 s read
        stopped at 97 s logged ``audio=167.215s, wall=96.812s``, which reads as
        VLC ending 70 s early. A read stopped while still synthesizing logged
        nothing at all, so the stuttering reads the user gave up on left no
        trace. Counts and durations only, never text. ``wall`` includes any
        time the user held the read paused, which ``paused`` states, so the two
        lines up against ``audio``. While paused VLC keeps reading ahead into
        its own buffer, so callback waits can also include that catch-up.
        """
        wall_s = (
            time.monotonic() - playback_started_at
            if playback_started_at is not None else 0.0
        )
        waits, max_wait_s = stream.starvation_stats
        applog.dbg(
            f"local neural playback {outcome} "
            f"(blocks={pace.blocks}/{total_blocks}, callback_waits={waits}, "
            f"max_callback_wait={max_wait_s:.3f}s, "
            f"worst_synth_ratio={pace.worst_ratio:.2f}, "
            f"est_gap={pace.gap_s:.2f}s, "
            f"audio={pace.audio_s:.3f}s, wall={wall_s:.3f}s, "
            f"paused={paused_s:.3f}s)"
        )

    def _start_vlc_pcm_stream(self, stream, stop_event, progress):
        """Start one raw 24 kHz mono PCM stream already synthesized at speed."""
        import ctypes
        import vlc

        paced_stream = _PacedAudioStream(stream, stop_event)

        @vlc.cb.MediaReadCb
        def read_cb(_opaque, buf, length):
            data = paced_stream.read(int(length))
            if data is None:
                return -1
            if not data:
                return 0
            ctypes.memmove(buf, data, len(data))
            return len(data)

        with self._vlc_lock:
            if not self._vlc_player:
                self._init_vlc()
            if not self._vlc_player:
                raise RuntimeError("VLC player not available")
            if stop_event.is_set():
                return
            media = self._vlc_instance.media_new_callbacks(
                None, read_cb, None, None, None
            )
            if media is None:
                raise RuntimeError("VLC could not open the local neural stream")
            media.add_option(":demux=rawaud")
            media.add_option(":rawaud-channels=1")
            media.add_option(f":rawaud-samplerate={LOCAL_PCM_SAMPLE_RATE}")
            media.add_option(":rawaud-fourcc=s16l")
            self._vlc_callbacks = (read_cb, paced_stream)
            self._vlc_player.set_media(media)
            media.release()
            self._vlc_player.audio_set_volume(int(self._volume * 100))
            self._vlc_player.play()

        for _ in range(40):
            if stop_event.is_set():
                self._vlc_stop()
                return
            if self._vlc_player.is_playing():
                if not progress:
                    progress.append("local-neural-stream-started")
                break
            if self._vlc_player.get_state() == vlc.State.Error:
                raise stream.error or RuntimeError("VLC local neural playback error")
            time.sleep(0.025)
        else:
            self._vlc_stop()
            raise stream.error or RuntimeError("VLC local neural stream did not start")

        if stop_event.is_set():
            self._vlc_stop()
            return
        self._go_live(stop_event, "vlc")
        # The PCM already contains the requested speed. Live testing proved
        # that VLC silently ignored non-1.0 rates on this callback-backed raw
        # stream, so applying _speed here both lies to the UI and risks a future
        # double-speed regression if VLC changes its behavior.
        try:
            self._vlc_player.set_rate(1.0)
        except Exception:
            pass

    def _speak_neural(self, text, gen, stop_event, progress):
        """Neural path with a bounded retry.

        Two guards on the retry:
          * Only while NOTHING has played yet, so a retry can never repeat
            audio the user already heard.
          * Never after a STALL. TimeoutError here is our own detector for no
            first audio or a stream that stopped advancing while the complete
            utterance was being buffered. Retrying buys another full timeout
            of dead air before an honest error. Fast failures (DNS, 403,
            websocket close) cost ~nothing to retry and are the ones that recover.
        """
        attempt = 0
        while True:
            try:
                self._synthesize_and_play(text, gen, stop_event, progress)
                return
            except TimeoutError:
                raise
            except Exception as e:
                if stop_event.is_set() or progress or attempt >= self.NEURAL_RETRIES:
                    raise
                attempt += 1
                applog.error(f"neural TTS attempt {attempt} failed ({e}); retrying")
                self.status.emit("Neural voice hiccuped - retrying...")
                if stop_event.wait(self.RETRY_PAUSE_S):
                    return  # stopped during the pause

    def _split_for_offline_fallback(self, text):
        """Small sentence chunks for a non-interruptible SAPI driver only."""
        import re

        text = text.strip()
        if not text:
            return []
        pieces = re.split(r"(?<=[.!?])\s+", text)
        chunks, buf = [], ""
        for p in pieces:
            p = p.strip()
            if not p:
                continue
            buf = f"{buf} {p}".strip() if buf else p
            if buf[-1] in ".!?" or len(buf) >= 160:
                chunks.append(buf)
                buf = ""
        if buf:
            chunks.append(buf)
        out = []
        for c in chunks:
            while len(c) > 240:
                cut = c.rfind(" ", 0, 240)
                cut = cut if cut > 0 else 240
                out.append(c[:cut].strip())
                c = c[cut:].strip()
            if c:
                out.append(c)
        return out

    def _synthesize_and_play(self, text, gen, stop_event, progress=None):
        """Stream one neural request through one VLC playback session.

        The old implementation sent every <=240-character sentence as a new
        edge-tts request. At 2.1x speed playback repeatedly outran those fresh
        web connections: silence, then catch-up. Waiting for the *complete*
        MP3 removed the gaps but made long selections take too long to start.
        Concurrent mini-MP3 requests then made startup quick but introduced a
        new discontinuity at every independently synthesized boundary. This
        path keeps one Communicate generator alive, waits for a protected lead,
        then lets VLC consume that same continuous stream while it is produced.

        `progress` accumulates the chunks that actually reached the speakers;
        the caller uses it to decide retry-vs-truncate (see _speak_job).
        """
        if progress is None:
            progress = []

        text = text.strip()
        if not text:
            return

        try:
            asyncio.run(
                self._synthesize_and_play_async(
                    text, gen, stop_event, progress
                )
            )
        except asyncio.CancelledError:
            if not stop_event.is_set():
                raise RuntimeError("Neural TTS task was canceled unexpectedly")

    async def _synthesize_and_play_async(self, text, gen, stop_event, progress):
        """Worker-owned async producer with LibVLC consuming on its native thread."""
        import edge_tts

        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        with self._async_lock:
            self._active_async_loop = loop
            self._active_async_task = task

        attempt_cancel = threading.Event()
        stream = _StreamingAudioBuffer(stop_event, attempt_cancel)
        audio_stream = None
        playback_started = False
        synth_started = time.monotonic()
        play_started = None
        error = None

        lead_seconds = (
            self.SHORT_START_BUFFER_PLAY_SECONDS
            if len(text) <= self.SHORT_TEXT_MAX_CHARS
            else self.START_BUFFER_PLAY_SECONDS
        )
        start_bytes = int(
            EDGE_MP3_BYTES_PER_SECOND * lead_seconds * max(1.0, self._speed)
        )
        startup_deadline = synth_started + self.FIRST_AUDIO_TIMEOUT
        last_audio_at = None
        self.status.emit("Buffering neural speech...")

        def start_playback():
            nonlocal playback_started, play_started
            if playback_started or stop_event.is_set():
                return
            buffered = stream.available
            applog.dbg(
                "neural TTS playback ready "
                f"(chars={len(text)}, buffered={buffered}, "
                f"wait={time.monotonic() - synth_started:.2f}s)"
            )
            self.status.emit(f"Playing at {self._speed:.2f}x...")
            self._start_vlc_stream(stream, stop_event, progress)
            if not stop_event.is_set():
                playback_started = True
                play_started = time.monotonic()

        try:
            communicate = edge_tts.Communicate(
                text,
                self._voice_id,
                connect_timeout=max(1, int(self.FIRST_AUDIO_TIMEOUT)),
                receive_timeout=max(1, int(self.SYNTHESIS_STALL_TIMEOUT)),
            )
            audio_stream = communicate.stream()
            iterator = audio_stream.__aiter__()

            while not stop_event.is_set():
                now = time.monotonic()
                if playback_started:
                    timeout = self.SYNTHESIS_STALL_TIMEOUT
                    if last_audio_at is not None:
                        timeout -= max(0.0, now - last_audio_at)
                else:
                    timeout = startup_deadline - now
                if timeout <= 0:
                    if playback_started:
                        raise TimeoutError(
                            "Neural TTS stopped streaming for "
                            f"{self.SYNTHESIS_STALL_TIMEOUT:.0f}s (network stall)"
                        )
                    raise TimeoutError(
                        "Neural TTS was not ready to play within "
                        f"{self.FIRST_AUDIO_TIMEOUT:.0f}s (network stall)"
                    )

                try:
                    ck = await asyncio.wait_for(iterator.__anext__(), timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    if playback_started:
                        raise TimeoutError(
                            "Neural TTS stopped streaming for "
                            f"{self.SYNTHESIS_STALL_TIMEOUT:.0f}s (network stall)"
                        ) from exc
                    raise TimeoutError(
                        "Neural TTS was not ready to play within "
                        f"{self.FIRST_AUDIO_TIMEOUT:.0f}s (network stall)"
                    ) from exc

                if ck.get("type") == "audio":
                    stream.write(ck.get("data", b""))
                    last_audio_at = time.monotonic()
                    if not playback_started and stream.available >= start_bytes:
                        start_playback()

            if stop_event.is_set():
                return
        except asyncio.CancelledError:
            if not stop_event.is_set():
                raise
        except Exception as exc:
            error = exc
        finally:
            # Closing the async generator exits edge-tts's aiohttp/websocket
            # contexts. A canceled task must be uncanceled first or the cleanup
            # await is immediately canceled again on Python 3.11+.
            if audio_stream is not None:
                current = asyncio.current_task()
                if current is not None and hasattr(current, "uncancel"):
                    while current.cancelling():
                        current.uncancel()
                try:
                    await audio_stream.aclose()
                except (Exception, asyncio.CancelledError):
                    pass
            stream.finish(error)
            with self._async_lock:
                if self._active_async_task is task:
                    self._active_async_loop = None
                    self._active_async_task = None

        if stop_event.is_set():
            stream.cancel()
            return
        if not playback_started:
            if error is not None:
                raise error
            if not stream.available:
                raise RuntimeError("Neural TTS returned no playable audio")
            start_playback()  # complete short streams may be smaller than the lead
        self._wait_vlc_stream(stream, stop_event)
        if stop_event.is_set():
            stream.cancel()
            return

        starvations, max_starvation = stream.starvation_stats
        applog.dbg(
            "neural TTS playback ended "
            f"(chars={len(text)}, {time.monotonic() - play_started:.2f}s, "
            f"buffer_waits={starvations}, max_wait={max_starvation:.2f}s)"
        )
        if error is not None:
            raise error

    def _start_vlc_stream(self, stream, stop_event, progress):
        """Start LibVLC's custom stream; return while its native callback plays."""
        import ctypes
        import vlc

        @vlc.cb.MediaReadCb
        def read_cb(_opaque, buf, length):
            data = stream.read(int(length))
            if data is None:
                return -1
            if not data:
                return 0
            ctypes.memmove(buf, data, len(data))
            return len(data)

        with self._vlc_lock:
            if not self._vlc_player:
                self._init_vlc()
            if not self._vlc_player:
                raise RuntimeError("VLC player not available")
            if stop_event.is_set():
                return

            media = self._vlc_instance.media_new_callbacks(
                None, read_cb, None, None, None
            )
            if media is None:
                raise RuntimeError("VLC could not open the neural audio stream")
            self._vlc_callbacks = (read_cb,)
            self._vlc_player.set_media(media)
            media.release()
            self._vlc_player.audio_set_volume(int(self._volume * 100))
            self._vlc_player.play()

        started = False
        for _ in range(40):
            if stop_event.is_set():
                self._vlc_stop()
                return
            if self._vlc_player.is_playing():
                started = True
                if not progress:
                    progress.append("neural-stream-started")
                break
            state = self._vlc_player.get_state()
            if state == vlc.State.Error:
                raise stream.error or RuntimeError("VLC playback error")
            time.sleep(0.025)

        if not started:
            self._vlc_stop()
            raise stream.error or RuntimeError("VLC neural stream did not start")
        if stop_event.is_set():
            self._vlc_stop()
            return
        self._go_live(stop_event, "vlc")
        try:
            self._vlc_player.set_rate(self._speed)
        except Exception:
            pass

    def _wait_vlc_stream(self, stream, stop_event):
        """Wait for an already-started LibVLC stream to finish.

        A read that was PAUSED never reaches Ended on the raw PCM stream (see
        _PauseState), so for it the end is also accepted when the stream has
        handed VLC its last byte and VLC's media time has stopped moving while
        unpaused. Reads that were never paused end exactly as before.
        """
        import vlc

        pause = getattr(stop_event, "pause", None)
        last_media_ms = None
        last_moved = time.monotonic()
        while True:
            if stop_event.is_set():
                self._vlc_stop()
                return
            state = self._vlc_player.get_state()
            if state == vlc.State.Error:
                raise stream.error or RuntimeError("VLC playback error")
            if state in (vlc.State.Ended, vlc.State.Stopped):
                if stream.error is not None:
                    raise stream.error
                return
            if pause is not None and pause.was_paused:
                now = time.monotonic()
                media_ms = self._vlc_player.get_time()
                if pause.paused or media_ms != last_media_ms:
                    last_media_ms, last_moved = media_ms, now
                elif (getattr(stream, "eof_delivered", False)
                      and now - last_moved >= self.PAUSED_END_STALL_S):
                    applog.dbg(
                        "read-aloud ended after a pause "
                        f"(media_time={media_ms}ms, paused={pause.seconds():.1f}s)"
                    )
                    self._vlc_stop()
                    if stream.error is not None:
                        raise stream.error
                    return
            time.sleep(0.05)

    def _play_vlc_stream(self, stream, stop_event, progress):
        """Compatibility wrapper for complete producer-fed stream playback."""
        self._start_vlc_stream(stream, stop_event, progress)
        if not stop_event.is_set():
            self._wait_vlc_stream(stream, stop_event)

    def _play_vlc(self, path, stop_event):
        """Play a complete audio file via VLC, honoring stop + live speed changes."""
        import vlc

        with self._vlc_lock:
            if not self._vlc_player:
                self._init_vlc()
            if not self._vlc_player:
                raise RuntimeError("VLC player not available")
            # Re-checked under the lock: if stop() already ran, never start.
            if stop_event.is_set():
                return

            media = self._vlc_instance.media_new(path)
            self._vlc_player.set_media(media)
            media.release()  # player holds its own reference
            self._vlc_player.audio_set_volume(int(self._volume * 100))
            self._vlc_player.play()

        # Warm-up: VLC's play() is asynchronous. This loop MUST honor the stop
        # event — it used to sleep up to a full second unconditionally, so a
        # Stop pressed here kept playing until the warm-up expired.
        for _ in range(40):
            if stop_event.is_set():
                self._vlc_stop()
                return
            if self._vlc_player.is_playing():
                break
            time.sleep(0.025)
        if stop_event.is_set():
            self._vlc_stop()
            return
        try:
            self._vlc_player.set_rate(self._speed)
        except Exception:
            pass

        while True:
            if stop_event.is_set():
                self._vlc_stop()
                return
            state = self._vlc_player.get_state()
            if state == vlc.State.Error:
                return
            if state in (vlc.State.Ended, vlc.State.Stopped):
                break
            time.sleep(0.05)

    def _vlc_stop(self):
        with self._vlc_lock:
            if self._vlc_player:
                try:
                    self._vlc_player.stop()
                except Exception:
                    applog.exception("vlc stop failed")

    # ------------------------------------------------------------------ #
    # Offline (SAPI) fallback — INTERRUPTIBLE
    # ------------------------------------------------------------------ #
    def _offline_voice_id(self):
        """Which SAPI voice token the offline path should select.

        NEVER the neural id. `self._voice_id` holds an edge-tts name such as
        'en-US-AndrewNeural' whenever a neural voice is chosen, and handing
        that to SAPI selects nothing — Windows keeps whatever default it had.
        That is why a fallback could come out in a voice the user never picked,
        rather than the closest match to the one they did.
        """
        if self._use_offline:
            return self._sapi_voice_id
        return self._match_sapi_voice(self._voice_id)

    def _match_sapi_voice(self, neural_id):
        """Closest installed SAPI voice to a neural one (locale+gender, then
        gender). Returns None to leave the system default alone."""
        meta = NEURAL_META.get(neural_id)
        if not meta or not self._pyttsx_engine:
            return None
        locale, gender = meta
        try:
            voices = self._pyttsx_engine.getProperty("voices")
        except Exception:
            return None

        def is_gender(v):
            return str(getattr(v, "gender", "") or "").lower() == gender

        def is_locale(v):
            langs = [str(x).lower() for x in (getattr(v, "languages", None) or [])]
            if any(locale.lower() in x for x in langs):
                return True
            return locale.upper() in str(getattr(v, "id", "")).upper()

        for pred in (lambda v: is_gender(v) and is_locale(v), is_gender):
            for v in voices:
                try:
                    if pred(v):
                        return v.id
                except Exception:
                    continue
        return None

    def _offline_chunk_budget(self, chunk):
        """Upper bound on how long one chunk may take. Purely a safety net for
        a wedged SAPI driver — a normal chunk finishes far inside it."""
        return 10.0 + (len(chunk) * 0.12) / max(0.5, self._speed)

    def _speak_offline(self, text, stop_event):
        """Offline fallback using pyttsx3 SAPI — interruptible, per the STOP
        CONTRACT above.

        Driven through pyttsx3's EXTERNAL event loop (`startLoop(False)` +
        `iterate()`) instead of `runAndWait()`, so the stop event is checked
        every ~10 ms rather than once per utterance. The normal path queues the
        complete selection once, avoiding a SAPI teardown/restart pause at each
        sentence. Only a driver without external-loop support uses sentence
        chunks, so its Stop over-run is bounded to one sentence.
        """
        if stop_event.is_set():
            return
        import pyttsx3

        engine = self._pyttsx_engine or pyttsx3.init()
        self._pyttsx_engine = engine

        with self._offline_lock:
            self._reset_engine_loop(engine)
            try:
                engine.setProperty("rate", int(BASE_RATE_WPM * self._speed))
                engine.setProperty("volume", self._volume)
            except Exception:
                applog.exception("offline TTS: rate/volume rejected")
            voice = self._offline_voice_id()
            if voice:
                try:
                    engine.setProperty("voice", voice)
                except Exception:
                    applog.exception("offline TTS: voice rejected")

            # Happy path: one continuous SAPI utterance, still interruptible
            # because _speak_offline_chunk pumps the external loop itself.
            if self._speak_offline_chunk(
                engine, text, stop_event, allow_blocking_fallback=False
            ):
                return

            # Degraded driver: it cannot expose an interruptible event loop.
            # Chunk ONLY here, limiting a blocking runAndWait() to one sentence.
            for chunk in self._split_for_offline_fallback(text) or [text]:
                if stop_event.is_set():
                    return
                self._speak_offline_chunk(
                    engine, chunk, stop_event, allow_blocking_fallback=True
                )

    @staticmethod
    def _reset_engine_loop(engine):
        """Clear a stuck `_inLoop`.

        pyttsx3 raises 'run loop already started' from BOTH runAndWait() and
        startLoop() when a previous run died inside the loop — after which the
        fallback is silently mute for the rest of the session.
        """
        if not getattr(engine, "_inLoop", False):
            return
        try:
            engine.endLoop()
        except Exception:
            try:
                engine._inLoop = False
            except Exception:
                pass

    def _speak_offline_chunk(
        self, engine, chunk, stop_event, allow_blocking_fallback=True
    ):
        """Speak one queued SAPI utterance.

        Returns True when the external loop ran (or the explicitly allowed
        blocking fallback ran), False when external-loop support is absent and
        the caller must retry using small chunks.
        """
        done = threading.Event()
        token = None
        try:
            token = engine.connect(
                "finished-utterance", lambda name=None, completed=None: done.set()
            )
        except Exception:
            pass

        try:
            engine.say(chunk)
            engine.startLoop(False)
        except Exception:
            # No external-loop support on this driver. Purge the whole-text
            # attempt before the caller retries in small blocking chunks.
            applog.exception("offline TTS: external loop unavailable")
            self._disconnect(engine, token)
            self._reset_engine_loop(engine)
            if not allow_blocking_fallback:
                try:
                    engine.stop()
                except Exception:
                    pass
                return False
            try:
                engine.runAndWait()
            except Exception:
                applog.exception("offline TTS: runAndWait failed")
            return True

        self._go_live(stop_event, "sapi")
        # The budget covers SPEAKING time; a read the user holds paused for a
        # minute is not a wedged driver.
        deadline = time.monotonic() + self._offline_chunk_budget(chunk)
        paused_before = _paused_seconds(stop_event)
        try:
            while not done.is_set():
                if stop_event.is_set():
                    break
                paused_s = _paused_seconds(stop_event) - paused_before
                if time.monotonic() - paused_s > deadline:
                    applog.error("offline TTS: chunk exceeded its budget; moving on")
                    break
                engine.iterate()
                time.sleep(0.01)
        finally:
            # endLoop() also purges the driver queue and stops the current
            # utterance, which is precisely what Stop needs.
            try:
                engine.endLoop()
            except Exception:
                self._reset_engine_loop(engine)
            self._disconnect(engine, token)
        return True

    @staticmethod
    def _disconnect(engine, token):
        if token is None:
            return
        try:
            engine.disconnect(token)
        except Exception:
            pass


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _drain_and_unlink(audio_q):
    while True:
        try:
            leftover = audio_q.get_nowait()
        except _queue.Empty:
            return
        if leftover:
            _unlink(leftover)
