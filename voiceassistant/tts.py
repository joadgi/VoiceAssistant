"""TTSEngine — edge-tts neural voices via VLC, pyttsx3 SAPI offline fallback.

Phase 3 rework:
  * ONE owned SerialWorker — utterances serialize; two workers can never
    drive the shared VLC player at once (the old overlap garble).
  * Per-utterance GENERATION: speak() during speech stops the current
    utterance and queues the new one (no toggle surprise, no drop). Each job
    carries its own stop-Event — a stale job can never be "un-stopped" by a
    newer call (the old shared-event clear() race).
  * Temp hygiene: per-utterance MP3 names, deleted after playback; the temp
    dir and VLC objects are released on shutdown().
  * Network hardening (Phase 0, kept): bounded queue waits — a stalled
    stream raises into the offline fallback instead of wedging; mid-stream
    death reports "cut short", never a false "complete".

Threading-law exception (documented): each utterance runs ONE transient
edge-tts producer thread owned by its job; it exits on the job's stop event
or network completion and is abandoned (daemon) only on a hard network stall.
"""

import asyncio
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


BASE_RATE_WPM = 175  # SAPI/offline base words-per-minute; scaled by _speed


class TTSEngine(QObject):
    """Neural TTS with real-time speed control via VLC."""

    speaking_started = Signal()
    speaking_finished = Signal()
    status = Signal(str)
    error = Signal(str)

    FIRST_AUDIO_TIMEOUT = 6.0   # no first chunk in time -> raise -> offline fallback
    STALL_TIMEOUT = 30.0        # mid-stream stall -> truncate, don't re-read

    def __init__(self, volume=1.0):
        super().__init__()
        self._speed = 1.0  # playback speed multiplier (0.5 to 3.0)
        self._volume = volume
        self._speaking = False
        self._gen = 0                     # utterance generation counter
        self._active_stop = None          # stop Event of the CURRENT utterance
        self._voice_id = "en-US-AndrewNeural"
        self._temp_dir = tempfile.mkdtemp(prefix="voiceassist_")
        self._worker = SerialWorker("tts")

        # VLC instance for real-time speed playback
        self._vlc_instance = None
        self._vlc_player = None
        self._init_vlc()

        # pyttsx3 as fallback for offline
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
        """Return list of (id, name) — neural voices first, then offline fallback."""
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
            self._voice_id = voice_id[5:]
            if self._pyttsx_engine:
                self._pyttsx_engine.setProperty("voice", self._voice_id)
        else:
            self._use_offline = False
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
            self._pyttsx_engine.setProperty("rate", int(BASE_RATE_WPM * self._speed))

    # ------------------------------------------------------------------ #
    # Speak / stop
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
        """Stop the current utterance immediately."""
        if self._active_stop is not None:
            self._active_stop.set()
        if self._vlc_player:
            try:
                self._vlc_player.stop()
            except Exception:
                pass
        self._speaking = False

    def shutdown(self):
        """Full teardown for app exit: stop, drain the worker, release VLC,
        remove the temp dir."""
        self.stop()
        self._worker.shutdown()
        try:
            if self._vlc_player is not None:
                self._vlc_player.release()
            if self._vlc_instance is not None:
                self._vlc_instance.release()
        except Exception:
            pass
        self._vlc_player = None
        self._vlc_instance = None
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
        try:
            if self._use_offline:
                self.status.emit("Using offline TTS...")
                self._speak_offline(text, stop_event)
            else:
                self.status.emit("Generating neural speech...")
                self._synthesize_and_play(text, gen, stop_event)
            if self._is_current(gen, stop_event):
                self.status.emit("Speech complete")
        except Exception as e:
            if not stop_event.is_set():
                if not self._use_offline and self._pyttsx_engine:
                    try:
                        self.status.emit("Neural speech failed; using offline voice...")
                        self._speak_offline(text, stop_event)
                        self.status.emit("Speech complete")
                    except Exception as e2:
                        self.error.emit(f"TTS error: {e2}")
                else:
                    self.error.emit(f"TTS error: {e}")
        finally:
            if gen == self._gen:
                self._speaking = False
                self._active_stop = None
            self.speaking_finished.emit()

    def _split_for_streaming(self, text):
        """Break text into small chunks (sentences) so the first plays fast."""
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

    def _synthesize_and_play(self, text, gen, stop_event):
        """Play the first sentence as soon as it downloads; fetch the rest in
        the background. Bounded waits everywhere — a network stall can never
        wedge this worker (see class docstring)."""
        import edge_tts

        chunks = self._split_for_streaming(text)
        if not chunks:
            return

        audio_q = _queue.Queue()
        producer_error = [None]

        def producer():
            async def gen_audio():
                for i, chunk in enumerate(chunks):
                    if stop_event.is_set():
                        return
                    path = os.path.join(self._temp_dir, f"tts_{gen}_{i}.mp3")
                    communicate = edge_tts.Communicate(chunk, self._voice_id)
                    with open(path, "wb") as f:
                        async for ck in communicate.stream():
                            if stop_event.is_set():
                                return
                            if ck["type"] == "audio":
                                f.write(ck["data"])
                    if os.path.getsize(path) > 0:
                        audio_q.put(path)

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(gen_audio())
            except Exception as e:
                producer_error[0] = e
            finally:
                loop.close()
                audio_q.put(None)  # sentinel: no more chunks

        threading.Thread(
            target=producer, name=f"tts-producer-{gen}", daemon=True
        ).start()

        played_any = False
        wait_started = time.monotonic()
        while not stop_event.is_set():
            try:
                path = audio_q.get(timeout=0.25)
            except _queue.Empty:
                limit = self.STALL_TIMEOUT if played_any else self.FIRST_AUDIO_TIMEOUT
                if time.monotonic() - wait_started >= limit:
                    if played_any:
                        self.status.emit("Speech cut short (network stall)")
                        return
                    raise TimeoutError(
                        f"Neural TTS produced no audio within {limit:.0f}s (network stall)"
                    )
                continue
            if path is None:
                break
            if stop_event.is_set():
                return
            self.status.emit(f"Playing at {self._speed:.2f}x...")
            try:
                self._play_vlc(path, stop_event)
            finally:
                try:
                    os.remove(path)  # per-chunk cleanup (M9)
                except OSError:
                    pass
            played_any = True
            wait_started = time.monotonic()

        if producer_error[0] and not played_any:
            raise producer_error[0]
        if producer_error[0] and played_any:
            self.status.emit("Speech cut short (synthesis error)")

    def _play_vlc(self, path, stop_event):
        """Play a complete audio file via VLC, honoring stop + live speed changes."""
        if not self._vlc_player:
            self._init_vlc()
        if not self._vlc_player:
            raise RuntimeError("VLC player not available")

        import vlc

        media = self._vlc_instance.media_new(path)
        self._vlc_player.set_media(media)
        media.release()  # player holds its own reference
        self._vlc_player.audio_set_volume(int(self._volume * 100))
        self._vlc_player.play()

        for _ in range(40):
            if self._vlc_player.is_playing():
                break
            time.sleep(0.025)
        self._vlc_player.set_rate(self._speed)

        while True:
            if stop_event.is_set():
                self._vlc_player.stop()
                return
            state = self._vlc_player.get_state()
            if state == vlc.State.Error:
                return
            if state in (vlc.State.Ended, vlc.State.Stopped):
                break
            time.sleep(0.05)

    def _speak_offline(self, text, stop_event):
        """Offline fallback using pyttsx3 SAPI."""
        if stop_event.is_set():
            return
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", int(BASE_RATE_WPM * self._speed))
        engine.setProperty("volume", self._volume)
        if self._voice_id:
            engine.setProperty("voice", self._voice_id)
        engine.say(text)
        engine.runAndWait()
        engine.stop()
