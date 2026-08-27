"""TTSEngine — edge-tts neural voices via VLC, explicit pyttsx3 SAPI option.

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

The TTS SerialWorker also owns edge-tts's asyncio loop. LibVLC's native media
callback consumes the in-memory stream while that same worker continues the
single async request; there is no second Python producer thread to outlive or
overlap the next utterance.
"""

import asyncio
import collections
import os
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
                    return None if self._error is not None else b""
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


class TTSEngine(QObject):
    """Neural TTS with real-time speed control via VLC."""

    speaking_started = Signal()
    speaking_finished = Signal()
    status = Signal(str)
    error = Signal(str)
    FIRST_AUDIO_TIMEOUT = 6.0   # chosen neural voice gets a bounded startup window
    SYNTHESIS_STALL_TIMEOUT = 10.0  # no more bytes while buffering -> honest error
    START_BUFFER_PLAY_SECONDS = 3.0  # long reads: absorb jitter at high playback speeds
    SHORT_START_BUFFER_PLAY_SECONDS = 1.0  # short reads must begin promptly
    SHORT_TEXT_MAX_CHARS = 600
    NEURAL_RETRIES = 1          # one cheap neural retry for a fast service hiccup
    RETRY_PAUSE_S = 0.4

    def __init__(self, volume=1.0):
        super().__init__()
        self._speed = 1.0  # playback speed multiplier (0.5 to 3.0)
        self._volume = volume
        self._speaking = False
        self._gen = 0                     # utterance generation counter
        self._active_stop = None          # stop Event of the CURRENT utterance
        self._voice_id = "en-US-AndrewNeural"
        self._sapi_voice_id = None        # set ONLY when the user picks a SAPI voice
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

    def get_voices(self):
        """Return list of (id, name) — neural voices, then explicit offline options."""
        voices = [(vid, f"[Neural] {name}") for name, vid in NEURAL_VOICES]
        if self._pyttsx_engine:
            try:
                for v in self._pyttsx_engine.getProperty("voices"):
                    short = v.name.split(" - ")[0] if " - " in v.name else v.name
                    voices.append((f"sapi:{v.id}", f"[Offline] {short}"))
            except Exception:
                pass
        return voices

    def set_voice(self, voice_id):
        if voice_id.startswith("sapi:"):
            self._use_offline = True
            self._sapi_voice_id = voice_id[5:]
            self._voice_id = self._sapi_voice_id
            if self._pyttsx_engine:
                self._pyttsx_engine.setProperty("voice", self._voice_id)
        else:
            self._use_offline = False
            self._sapi_voice_id = None
            self._voice_id = voice_id

    def set_speed(self, speed):
        """Set playback speed (0.5 to 3.0). Takes effect immediately during playback."""
        self._speed = max(0.5, min(3.0, float(speed)))
        if self._vlc_player and self._speaking:
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
        if not text.strip():
            return
        self.stop()  # no-op when idle
        self._gen += 1
        stop_event = threading.Event()
        self._active_stop = stop_event
        self._speaking = True
        self._worker.submit(self._speak_job, text, self._gen, stop_event)

    def stop(self):
        """Stop the current utterance immediately.

        Order matters: the stop event is set FIRST so no worker-side wait can
        start new audio behind our back, and only then is each backend told to
        shut up. Runs on the GUI thread — every call here is non-blocking or
        bounded to a couple of milliseconds.
        """
        if self._active_stop is not None:
            self._active_stop.set()
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
            if self._use_offline:
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
            elif not self._use_offline:
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
        try:
            self._vlc_player.set_rate(self._speed)
        except Exception:
            pass

    def _wait_vlc_stream(self, stream, stop_event):
        """Wait for an already-started LibVLC stream to finish."""
        import vlc

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

        deadline = time.monotonic() + self._offline_chunk_budget(chunk)
        try:
            while not done.is_set():
                if stop_event.is_set():
                    break
                if time.monotonic() > deadline:
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
