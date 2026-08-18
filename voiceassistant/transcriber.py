"""Transcriber — faster-whisper transcription on one owned worker.

All model loads and transcription jobs serialize on a single SerialWorker
(the threading law) — the old per-job daemon threads plus a lock are gone.
"""

import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from .workers import SerialWorker

# Whisper's fixed input rate. Audio reaches this module already at 16 kHz mono
# (the recorder is pinned to it); every sample-count → seconds calc uses this.
SAMPLE_RATE = 16000


def _is_cache_miss(exc):
    """True when a local_files_only load failed only because nothing is cached.

    Anything else (a CUDA/cuDNN failure, a corrupt file) must NOT be treated as
    a cache miss, or we would fire off a pointless multi-GB download.
    """
    try:
        from huggingface_hub.errors import LocalEntryNotFoundError

        if isinstance(exc, LocalEntryNotFoundError):
            return True
    except Exception:
        pass
    if exc.__class__.__name__ in ("LocalEntryNotFoundError", "EntryNotFoundError"):
        return True
    text = str(exc).lower()
    return "local_files_only" in text or "cannot find the requested files" in text


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
    latency_ms: float = 0.0  # wall-clock decode time (fed to metrics)
    no_speech: bool = False  # True if both passes found nothing


class Transcriber(QObject):
    """Loads faster-whisper and transcribes audio arrays."""

    model_loading = Signal(str)  # status message
    model_ready = Signal()
    # Loaded, but in a materially worse mode than requested (CPU instead of
    # CUDA is 10-20x slower). A degraded session that only writes a log line
    # is experienced as "dictation got slow" with no explanation, so this is
    # surfaced LOUDLY (tray balloon + persistent label), not just logged.
    degraded = Signal(str)
    transcription_ready = Signal(object)  # emits TranscriptionResult
    transcription_progress = Signal(str)  # partial results
    error = Signal(str)

    def __init__(self, model_size="medium", device="cuda", compute_type="float16",
                 language="en", initial_prompt=""):
        super().__init__()
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.initial_prompt = initial_prompt or ""
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

    def _open_model(self, device, compute_type):
        """Open the model on `device`, preferring the local cache.

        WHY local_files_only FIRST: faster-whisper otherwise revalidates the
        model against huggingface.co on EVERY launch. Measured on this machine
        with large-v3 already fully cached: **176.3s with the network check vs
        7.0s from cache** — i.e. ~3 minutes after every boot during which
        pressing the dictate hotkey only says "model still loading" and the
        words are lost. It was also an undocumented external call in an app
        whose whole premise is that dictation stays on your machine. The network
        is now touched exactly once per model: the first time it is needed.
        """
        from faster_whisper import WhisperModel

        try:
            return WhisperModel(
                self.model_size, device=device, compute_type=compute_type,
                local_files_only=True,
            )
        except Exception as e:
            if not _is_cache_miss(e):
                raise

        from . import applog

        applog.info(f"whisper model {self.model_size} not cached; downloading once")
        self.model_loading.emit(
            f"Downloading Whisper {self.model_size} (one-time)..."
        )
        return WhisperModel(
            self.model_size, device=device, compute_type=compute_type
        )

    def _load_job(self):
        from . import applog

        self.model_loading.emit(f"Loading Whisper {self.model_size} model...")
        if self.device == "cuda":
            self._add_nvidia_dll_dirs()

        want_device = self.device
        try:
            self._model = self._open_model(want_device, self.compute_type)
            self.model_ready.emit()
            return
        except Exception as e:
            # Record WHY the requested device failed so a silent slow-CPU
            # session is diagnosable.
            applog.error(f"model load on {want_device} failed ({e}); trying CPU int8")

        # The device fallback is deliberately SEPARATE from the cache/download
        # decision inside _open_model: a CUDA failure must never be mistaken for
        # a cache miss, and a download must never silently pin us to CPU.
        try:
            self.model_loading.emit("GPU unavailable - falling back to CPU...")
            self._model = self._open_model("cpu", "int8")
            self.device, self.compute_type = "cpu", "int8"
            self.model_ready.emit()
            if want_device != "cpu":
                self.degraded.emit(
                    "Whisper is running on the CPU, not the GPU - dictation will be "
                    "much slower than usual. See debug.log for the GPU error."
                )
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
        return self._job_seq  # so callers can correlate metrics to the job

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
        # beam_size/best_of=5 + the temperature ladder are faster-whisper's own
        # defaults, and they are here on purpose. This used to run greedy
        # (beam_size=1, best_of=1, temperature=0.0), which is the fastest and
        # least accurate configuration there is: greedy decoding commits to the
        # first token every step, and pinning temperature to 0.0 DISABLES the
        # fallback ladder — so when a decode tripped the compression/logprob
        # thresholds there was no retry, the bad text just shipped. On a 3070
        # the beam search costs tens of milliseconds on dictation-length clips;
        # that is a trade worth making for a tool that has to be trusted.
        kwargs = dict(
            language=self.language,
            beam_size=5,
            best_of=5,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            condition_on_previous_text=False,
            word_timestamps=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
        )
        # Optional vocabulary/style bias (proper nouns, jargon, casing). Off by
        # default: a prompt can leak into the output, so it is opt-in per user.
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
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
            t0 = time.perf_counter()
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
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
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
