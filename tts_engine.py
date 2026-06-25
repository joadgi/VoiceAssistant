"""Text-to-speech engine: edge-tts neural voices + VLC for real-time speed control."""

import threading
import asyncio
import tempfile
import os
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
                    self.status.emit("Streaming neural speech...")
                    self._stream_neural_to_vlc(text)
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

    def _stream_neural_to_vlc(self, text):
        """Stream edge-tts audio directly to file and start VLC playback ASAP."""
        import edge_tts
        import time as _time

        audio_path = os.path.join(self._temp_dir, "tts_stream.mp3")
        # Clear any old content
        open(audio_path, "wb").close()

        first_chunk = threading.Event()
        writer_done = threading.Event()
        writer_error = [None]

        def stream_writer():
            async def do_stream():
                communicate = edge_tts.Communicate(text, self._voice_id)
                with open(audio_path, "ab") as f:
                    async for chunk in communicate.stream():
                        if self._stop_event.is_set():
                            return
                        if chunk["type"] == "audio":
                            f.write(chunk["data"])
                            f.flush()
                            if not first_chunk.is_set():
                                first_chunk.set()

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(do_stream())
            except Exception as e:
                writer_error[0] = e
            finally:
                loop.close()
                writer_done.set()

        writer_thread = threading.Thread(target=stream_writer, daemon=True)
        writer_thread.start()

        # Wait for first audio chunk to arrive (max 5s)
        if not first_chunk.wait(timeout=5):
            if writer_error[0]:
                raise writer_error[0]
            raise RuntimeError("Timed out waiting for TTS audio")

        if self._stop_event.is_set():
            return

        # Give writer a tiny head-start so VLC has enough data to decode
        _time.sleep(0.08)

        self.status.emit(f"Playing at {self._speed:.2f}x...")
        self._play_vlc(audio_path, wait_for_writer=writer_done)

    def _play_vlc(self, path, wait_for_writer=None):
        """Play audio via VLC — supports real-time speed changes via set_rate.

        If wait_for_writer is provided, it's an Event signaled when streaming is
        complete. We need to keep the media "live" until then so VLC sees the
        growing file.
        """
        if not self._vlc_player:
            self._init_vlc()
        if not self._vlc_player:
            raise RuntimeError("VLC player not available")

        import vlc
        import time
        # :file-caching=50 reduces startup latency
        media = self._vlc_instance.media_new(path, ":file-caching=50")
        self._vlc_player.set_media(media)
        self._vlc_player.audio_set_volume(int(self._volume * 100))
        self._vlc_player.play()

        # Wait for playback to actually begin (fast path ~50ms)
        for _ in range(20):
            if self._vlc_player.is_playing():
                break
            time.sleep(0.025)
        self._vlc_player.set_rate(self._speed)

        # Playback loop — exit when stopped OR (ended AND writer done)
        while True:
            if self._stop_event.is_set():
                self._vlc_player.stop()
                return
            state = self._vlc_player.get_state()
            if state == vlc.State.Error:
                return
            if state in (vlc.State.Ended, vlc.State.Stopped):
                # If still streaming, reload media to pick up new data
                if wait_for_writer and not wait_for_writer.is_set():
                    pos_ms = self._vlc_player.get_time()
                    media2 = self._vlc_instance.media_new(path, ":file-caching=50")
                    self._vlc_player.set_media(media2)
                    self._vlc_player.play()
                    for _ in range(20):
                        if self._vlc_player.is_playing():
                            break
                        time.sleep(0.025)
                    if pos_ms > 0:
                        self._vlc_player.set_time(pos_ms)
                    self._vlc_player.set_rate(self._speed)
                    time.sleep(0.1)
                    continue
                break
            time.sleep(0.1)

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
