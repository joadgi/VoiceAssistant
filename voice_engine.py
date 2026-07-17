"""Voice recording and Whisper transcription engine."""

import threading
import queue
from dataclasses import dataclass
from typing import Any

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal


@dataclass
class TranscriptionResult:
    """One completed transcription job.

    The job carries its own context (e.g. the target HWND captured at record
    time) so overlapping dictations can never read each other's state — the
    old shared `_target_hwnd` field was a confirmed wrong-window-paste race.
    """

    text: str
    job_id: int
    context: Any = None      # opaque app payload (dictation: target HWND or None)
    duration_s: float = 0.0
    retried: bool = False    # True if the no-VAD retry produced this text
    no_speech: bool = False  # True if both passes found nothing


class VoiceRecorder(QObject):
    """Records audio from the microphone into a numpy buffer."""

    recording_started = Signal()
    recording_stopped = Signal(np.ndarray)  # emits the audio array
    level_update = Signal(float)  # emits RMS level for a VU meter
    error = Signal(str)

    def __init__(self, sample_rate=16000, device=None):
        super().__init__()
        self.sample_rate = sample_rate
        self.device = device  # None = system default, int = device index
        self._is_recording = False
        self._audio_queue = queue.Queue()
        self._stream = None

    @property
    def is_recording(self):
        return self._is_recording

    def start(self):
        if self._is_recording:
            return
        self._is_recording = True
        self._audio_queue = queue.Queue()

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
            self._audio_queue.put(indata.copy())
            rms = float(np.sqrt(np.mean(indata ** 2)))
            self.level_update.emit(rms)


class Transcriber(QObject):
    """Loads faster-whisper and transcribes audio arrays."""

    model_loading = Signal(str)  # status message
    model_ready = Signal()
    transcription_ready = Signal(object)  # emits TranscriptionResult
    transcription_progress = Signal(str)  # partial results
    error = Signal(str)

    def __init__(self, model_size="medium", device="cuda", compute_type="float16", language="en"):
        super().__init__()
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model = None
        self._lock = threading.Lock()
        self._job_seq = 0  # monotonic job ids (replaces the id(audio) guard)

    @property
    def is_loaded(self):
        return self._model is not None

    def load_model(self):
        """Load the Whisper model in a background thread."""
        thread = threading.Thread(target=self._load_model_worker, daemon=True)
        thread.start()

    def _load_model_worker(self):
        try:
            self.model_loading.emit(f"Loading Whisper {self.model_size} model...")
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            self.model_ready.emit()
        except Exception as e:
            # Try CPU fallback
            try:
                self.model_loading.emit(f"GPU failed, falling back to CPU...")
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8",
                )
                self.device = "cpu"
                self.compute_type = "int8"
                self.model_ready.emit()
            except Exception as e2:
                self.error.emit(f"Failed to load model: {e2}")

    def transcribe(self, audio_data, context=None):
        """Transcribe audio in a background thread.

        `context` is carried through to the TranscriptionResult untouched —
        the dictation flow passes the target HWND captured at record time so
        the result can never paste into a window captured for a later job.
        """
        if not self.is_loaded:
            self.error.emit("Model not loaded yet")
            return
        if len(audio_data) == 0:
            self.error.emit("No audio recorded")
            return

        self._job_seq += 1
        thread = threading.Thread(
            target=self._transcribe_worker,
            args=(audio_data, context, self._job_seq),
            daemon=True,
        )
        thread.start()

    def _run_transcribe(self, audio_data, use_vad):
        """Run one transcription pass.

        With VAD on, silence is trimmed (kills repeat/junk hallucinations);
        with VAD off, nothing is ever dropped. The anti-hallucination guards
        (no_speech/compression thresholds + the per-segment no_speech_prob
        filter) apply to BOTH passes — the no-VAD retry fires on exactly the
        silence/short/quiet case, which is where Whisper invents text, so the
        retry needs the guards the most. (Confirmed live: unguarded retry
        turned a 0.35s breath/click into "you" — see tests/fixtures/baseline.)
        """
        kwargs = dict(
            language=self.language,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            word_timestamps=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
        )
        if use_vad:
            kwargs.update(
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=400),
            )
        segments, _info = self._model.transcribe(audio_data, **kwargs)
        text_parts = []
        for segment in segments:
            if getattr(segment, "no_speech_prob", 0.0) > 0.6:
                continue
            seg_text = segment.text.strip()
            if seg_text:
                text_parts.append(seg_text)
        return " ".join(text_parts).strip()

    def _transcribe_worker(self, audio_data, context, job_id):
        try:
            with self._lock:
                duration = len(audio_data) / 16000
                max_amp = float(np.max(np.abs(audio_data)))
                self.transcription_progress.emit(
                    f"Transcribing {duration:.1f}s audio (peak: {max_amp:.3f})..."
                )

                retried = False
                full_text = self._run_transcribe(audio_data, use_vad=True)
                if not full_text:
                    # VAD may have judged quiet/short speech as silence. Retry
                    # without it so real audio is never lost ("voice not
                    # working") — with the shared segment guards still active.
                    retried = True
                    full_text = self._run_transcribe(audio_data, use_vad=False)

                self.transcription_ready.emit(
                    TranscriptionResult(
                        text=full_text,
                        job_id=job_id,
                        context=context,
                        duration_s=duration,
                        retried=retried,
                        no_speech=not full_text,
                    )
                )
        except Exception as e:
            self.error.emit(f"Transcription error: {e}")

    def change_model(self, model_size, device=None, compute_type=None):
        """Switch to a different model size."""
        self.model_size = model_size
        if device:
            self.device = device
        if compute_type:
            self.compute_type = compute_type
        self._model = None
        self.load_model()
