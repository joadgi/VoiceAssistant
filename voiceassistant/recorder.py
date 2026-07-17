"""VoiceRecorder — microphone capture into a numpy buffer.

Threading note: sounddevice owns the callback thread (OS-driven); this class
spawns no threads of its own. The callback only queues data and emits a
cross-thread Qt signal (safe).
"""

import queue

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal


class VoiceRecorder(QObject):
    """Records audio from the microphone into a numpy buffer."""

    recording_started = Signal()
    recording_stopped = Signal(np.ndarray)  # emits the audio array
    level_update = Signal(float)  # emits RMS level for a VU meter
    max_duration_reached = Signal()  # safety cap hit — GUI should stop us
    error = Signal(str)

    def __init__(self, sample_rate=16000, device=None, max_seconds=120.0):
        super().__init__()
        self.sample_rate = sample_rate
        self.device = device  # None = system default, int = device index
        self.max_seconds = max_seconds  # 0/None = no cap
        self._is_recording = False
        self._audio_queue = queue.Queue()
        self._stream = None
        self._frames_captured = 0
        self._max_frames = 0
        self._max_hit = False

    @property
    def is_recording(self):
        return self._is_recording

    def start(self):
        if self._is_recording:
            return
        self._is_recording = True
        self._audio_queue = queue.Queue()
        self._overflow_count = 0
        self._frames_captured = 0
        self._max_hit = False
        self._max_frames = int((self.max_seconds or 0) * self.sample_rate)

        try:
            dev = self.device if self.device and self.device >= 0 else None
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=1024,
                device=dev,
                callback=self._audio_callback,
            )
            self._stream.start()
            self.recording_started.emit()
        except Exception as e:
            self._is_recording = False
            self._stream = None
            self.error.emit(f"Microphone error: {e}")

    def stop(self):
        if not self._is_recording:
            return
        self._is_recording = False

        # Guarded teardown: PortAudio raises here if the device vanished
        # mid-recording (USB mic unplugged). That must never prevent the
        # recording_stopped emit below — an unemitted stop wedged the whole
        # dictation state machine (stuck "Recording" pill) and lost the audio.
        stream, self._stream = self._stream, None
        if stream is not None:
            err = None
            try:
                stream.stop()
            except Exception as e:
                err = e
            try:
                stream.close()  # must run even when stop() raised
            except Exception as e:
                err = err or e
            if err is not None:
                self.error.emit(f"Microphone stop error: {err}")

        if getattr(self, "_overflow_count", 0):
            from . import applog

            applog.error(
                f"audio input overflow x{self._overflow_count} during recording "
                "(dropped samples — system under load?)"
            )

        chunks = []
        while not self._audio_queue.empty():
            chunks.append(self._audio_queue.get())

        if chunks:
            audio = np.concatenate(chunks, axis=0).flatten()
            self.recording_stopped.emit(audio)
        else:
            self.recording_stopped.emit(np.array([], dtype="float32"))

    def _audio_callback(self, indata, frames, time_info, status):
        if self._is_recording:
            if status:
                # Input overflow = dropped samples (gappy audio). Count here
                # (audio-rate callback — never log per-block), surface at stop.
                self._overflow_count += 1
            self._audio_queue.put(indata.copy())
            rms = float(np.sqrt(np.mean(indata ** 2)))
            self.level_update.emit(rms)

            # Safety cap: a forgotten/stuck held hotkey would otherwise grow
            # the buffer forever (~64 KB/s). Signal the GUI thread to stop —
            # we must NOT call stop() here (closing the stream from inside its
            # own callback deadlocks PortAudio). Captured audio is preserved
            # and still transcribed; fires exactly once.
            self._frames_captured += frames
            if (self._max_frames and not self._max_hit
                    and self._frames_captured >= self._max_frames):
                self._max_hit = True
                self.max_duration_reached.emit()
