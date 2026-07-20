"""Transcriber — faster-whisper transcription on one owned worker.

All model loads and transcription jobs serialize on a single SerialWorker
(the threading law) — the old per-job daemon threads plus a lock are gone.
"""

from dataclasses import dataclass
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from .workers import SerialWorker

# Whisper's fixed input rate. Audio reaches this module already at 16 kHz mono
# (the recorder is pinned to it); every sample-count → seconds calc uses this.
SAMPLE_RATE = 16000


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
        self._job_seq = 0  # monotonic job ids (replaces the id(audio) guard)
        self._worker = SerialWorker("transcriber")

    @property
    def is_loaded(self):
        return self._model is not None

    def load_model(self):
        """Load the Whisper model on the transcriber worker."""
        self._worker.submit(self._load_job)

    @staticmethod
    def _add_nvidia_dll_dirs():
        """Make CTranslate2's CUDA path work WITHOUT PyTorch installed.

        Historically the cuBLAS/cuDNN DLLs came along for the ride with the
        ~3GB torch install (which only EasyOCR needed). With the native OCR
        backend, new installs get the slim NVIDIA runtime wheels instead
        (nvidia-cublas-cu12 / nvidia-cudnn-cu12) — this registers their bin
        dirs. Harmless no-op when the wheels (or torch) provide DLLs already.
        """
        import importlib.util
        import os

        for mod in ("nvidia.cublas", "nvidia.cudnn"):
            try:
                spec = importlib.util.find_spec(mod)
            except (ImportError, ModuleNotFoundError, ValueError):
                continue
            if spec and spec.submodule_search_locations:
                for loc in spec.submodule_search_locations:
                    bin_dir = os.path.join(loc, "bin")
                    if os.path.isdir(bin_dir):
                        try:
                            os.add_dll_directory(bin_dir)
                        except OSError:
                            pass

    def _load_job(self):
        try:
            self.model_loading.emit(f"Loading Whisper {self.model_size} model...")
            if self.device == "cuda":
                self._add_nvidia_dll_dirs()
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            self.model_ready.emit()
        except Exception as e:
            # Try CPU fallback — and record WHY the GPU path failed so a
            # silent slow-CPU session is diagnosable.
            from . import applog

            applog.error(f"GPU model load failed ({e}); falling back to CPU int8")
            try:
                self.model_loading.emit("GPU failed, falling back to CPU...")
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
        """Transcribe audio on the worker.

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
        self._worker.submit(self._transcribe_job, audio_data, context, self._job_seq)

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

    def _transcribe_job(self, audio_data, context, job_id):
        try:
            duration = len(audio_data) / SAMPLE_RATE
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

    def shutdown(self):
        """Stop the transcription worker (bounded). Called on app exit."""
        self._worker.shutdown()
