"""Text-to-speech engine: edge-tts neural voices + VLC for real-time speed control."""

import threading
import asyncio
import tempfile
import time
import os
import re
import queue as _queue
from PySide6.QtCore import QObject, Signal

# Modern Microsoft neural voices (available via edge-tts)
# Name, voice_id, description
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


class TTSEngine(QObject):
    """Neural TTS with real-time speed control via VLC."""

    speaking_started = Signal()
    speaking_finished = Signal()
    status = Signal(str)
    error = Signal(str)

    def __init__(self, rate=175, volume=1.0):
        super().__init__()
        self._rate = rate  # kept for backwards compat; we now use speed multiplier
        self._speed = 1.0  # playback speed multiplier (0.5 to 3.0)
        self._volume = volume
        self._speaking = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._voice_id = "en-US-AndrewNeural"  # default: warm male neural
        self._temp_dir = tempfile.mkdtemp(prefix="voiceassist_")
        self._current_text = None

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
            self._pyttsx_engine.setProperty("rate", int(175 * self._speed))
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
        # Live speed change via VLC (no regeneration needed)
        if self._vlc_player and self._speaking:
            try:
                self._vlc_player.set_rate(self._speed)
            except Exception:
                pass
        # Also update pyttsx3 rate for offline mode
        if self._pyttsx_engine:
            self._pyttsx_engine.setProperty("rate", int(175 * self._speed))

    def speak(self, text):
        """Generate and play text."""
        if not text.strip():
            return
        if self._speaking:
            self.stop()
            return
        self._stop_event.clear()
        self._speaking = True
        self._current_text = text
        thread = threading.Thread(target=self._speak_worker, args=(text,), daemon=True)
        thread.start()

    def _speak_worker(self, text):
        with self._lock:
            self.speaking_started.emit()
            try:
                if self._stop_event.is_set():
                    return
                if self._use_offline:
                    self.status.emit("Using offline TTS...")
                    self._speak_offline(text)
                else:
                    self.status.emit("Generating neural speech...")
                    self._synthesize_and_play(text)
                self.status.emit("Speech complete")
            except Exception as e:
                if not self._stop_event.is_set():
                    if not self._use_offline and self._pyttsx_engine:
                        try:
                            self.status.emit("Neural speech failed; using offline voice...")
                            self._speak_offline(text)
                            self.status.emit("Speech complete")
                        except Exception as e2:
                            self.error.emit(f"TTS error: {e2}")
                    else:
                        self.error.emit(f"TTS error: {e}")
            finally:
                self._speaking = False
                self.speaking_finished.emit()

    def _split_for_streaming(self, text):
        """Break text into small chunks (sentences) so the first plays fast."""
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
        # Hard-split any oversized chunk (e.g. no punctuation) on word boundaries.
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

    def _synthesize_and_play(self, text):
        """Play the first sentence as soon as it downloads; fetch the rest in the
        background. Each chunk is a COMPLETE file, so there's no mid-clip starvation
        (the old glitch), and time-to-first-audio is just the first short sentence.
        """
        import edge_tts

        chunks = self._split_for_streaming(text)
        if not chunks:
            return

        audio_q = _queue.Queue()
        producer_error = [None]

        def producer():
            async def gen():
                for i, chunk in enumerate(chunks):
                    if self._stop_event.is_set():
                        return
                    path = os.path.join(self._temp_dir, f"tts_{i}.mp3")
                    communicate = edge_tts.Communicate(chunk, self._voice_id)
                    with open(path, "wb") as f:
                        async for ck in communicate.stream():
                            if self._stop_event.is_set():
                                return
                            if ck["type"] == "audio":
                                f.write(ck["data"])
                    if os.path.getsize(path) > 0:
                        audio_q.put(path)

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(gen())
            except Exception as e:
                producer_error[0] = e
            finally:
                loop.close()
                audio_q.put(None)  # sentinel: no more chunks

        threading.Thread(target=producer, daemon=True).start()

        # Consume with a timeout so a stalled network can NEVER wedge this worker:
        # edge-tts can connect and then hang without raising, in which case the
        # producer's sentinel never arrives. A bounded get() + stop_event check
        # keeps stop() responsive and lets the first-audio timeout raise into
        # _speak_worker's fallback (pyttsx3) instead of blocking forever.
        FIRST_AUDIO_TIMEOUT = 6.0   # no first chunk in time -> raise -> offline fallback
        STALL_TIMEOUT = 30.0        # mid-stream stall -> truncate, don't re-read from top
        played_any = False
        wait_started = time.monotonic()
        while not self._stop_event.is_set():
            try:
                path = audio_q.get(timeout=0.25)
            except _queue.Empty:
                limit = STALL_TIMEOUT if played_any else FIRST_AUDIO_TIMEOUT
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
            if self._stop_event.is_set():
                return
            self.status.emit(f"Playing at {self._speed:.2f}x...")
            self._play_vlc(path)
            played_any = True
            wait_started = time.monotonic()  # reset stall clock after each chunk

        if producer_error[0] and not played_any:
            raise producer_error[0]
        if producer_error[0] and played_any:
            # Some sentences played but synthesis died mid-stream — say so
            # instead of reporting a clean "Speech complete".
            self.status.emit("Speech cut short (synthesis error)")

    def _play_vlc(self, path):
        """Play a complete audio file via VLC, honoring stop + live speed changes."""
        if not self._vlc_player:
            self._init_vlc()
        if not self._vlc_player:
            raise RuntimeError("VLC player not available")

        import vlc
        import time

        media = self._vlc_instance.media_new(path)
        self._vlc_player.set_media(media)
        self._vlc_player.audio_set_volume(int(self._volume * 100))
        self._vlc_player.play()

        # Wait for playback to actually begin, then apply the speed multiplier.
        for _ in range(40):
            if self._vlc_player.is_playing():
                break
            time.sleep(0.025)
        self._vlc_player.set_rate(self._speed)

        while True:
            if self._stop_event.is_set():
                self._vlc_player.stop()
                return
            state = self._vlc_player.get_state()
            if state == vlc.State.Error:
                return
            if state in (vlc.State.Ended, vlc.State.Stopped):
                break
            time.sleep(0.05)

    def _speak_offline(self, text):
        """Offline fallback using pyttsx3 SAPI."""
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", int(175 * self._speed))
        engine.setProperty("volume", self._volume)
        if self._voice_id:
            engine.setProperty("voice", self._voice_id)
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    def stop(self):
        """Stop playback immediately."""
        self._stop_event.set()
        if self._vlc_player:
            try:
                self._vlc_player.stop()
            except Exception:
                pass
        self._speaking = False
