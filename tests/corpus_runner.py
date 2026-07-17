"""Golden-audio corpus runner — drives the REAL dictation pipeline end-to-end.

Replicates exactly what the app does to a recorded buffer:
  1. the recording gate (min_record_seconds / min_record_peak) from config,
  2. Transcriber._run_transcribe(vad=True), retry without VAD when empty
     (verbatim logic from Transcriber._transcribe_worker),
  3. the post-processing chain (_dedupe_repeated -> _light_cleanup).

Modes:
  python tests/corpus_runner.py baseline   # record current behavior -> baseline.json
  python tests/corpus_runner.py run        # run + print, no file written
Import `run_corpus()` from tests for assertions (Phase 2 corpus gate).
"""

import os
import sys
import json
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AUDIO_DIR = os.path.join(ROOT, "tests", "fixtures", "audio")
BASELINE_PATH = os.path.join(ROOT, "tests", "fixtures", "baseline.json")


def load_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build_transcriber():
    """Load the model the same way the app does (CUDA -> CPU int8 fallback)."""
    from voiceassistant.config import DEFAULTS
    from voiceassistant.transcriber import Transcriber

    t = Transcriber(
        model_size=DEFAULTS["whisper_model"],
        device=DEFAULTS["whisper_device"],
        compute_type=DEFAULTS["whisper_compute_type"],
        language=DEFAULTS["whisper_language"],
    )
    from faster_whisper import WhisperModel

    try:
        t._model = WhisperModel(t.model_size, device=t.device, compute_type=t.compute_type)
    except Exception as e:
        print(f"  GPU load failed ({e.__class__.__name__}); CPU int8 fallback")
        t._model = WhisperModel(t.model_size, device="cpu", compute_type="int8")
        t.device, t.compute_type = "cpu", "int8"
    return t


def app_pipeline(transcriber, audio):
    """The app's transcribe path, verbatim (worker logic + retry)."""
    t0 = time.perf_counter()
    text_vad = transcriber._run_transcribe(audio, use_vad=True)
    t1 = time.perf_counter()
    retried = False
    text_final = text_vad
    if not text_vad:
        retried = True
        text_final = transcriber._run_transcribe(audio, use_vad=False)
    t2 = time.perf_counter()
    return {
        "raw_vad": text_vad,
        "retried_no_vad": retried,
        "raw_final": text_final,  # "" when both passes found nothing
        "vad_pass_ms": round((t1 - t0) * 1000),
        "total_ms": round((t2 - t0) * 1000),
    }


def app_decision(entry):
    """Replicate _on_transcription_ready's verdict for this clip: what text
    (if any) would actually be pasted?"""
    from voiceassistant.text import clean_transcript, is_probable_hallucination
    from voiceassistant.transcriber import TranscriptionResult

    raw = entry["raw_final"]
    result = TranscriptionResult(
        text=raw,
        job_id=0,
        duration_s=entry["duration_s"],
        retried=entry["retried_no_vad"],
        no_speech=not raw,
    )
    if result.no_speech:
        return {"cleaned": None, "would_paste": False, "verdict": "no_speech"}
    cleaned = clean_transcript(raw, light=True)
    if is_probable_hallucination(result, cleaned):
        return {"cleaned": cleaned, "would_paste": False, "verdict": "suppressed_hallucination"}
    return {"cleaned": cleaned, "would_paste": bool(cleaned.strip()), "verdict": "paste"}


def gate_decision(audio):
    """The app's silent-drop gate from _on_recording_stopped."""
    from voiceassistant.config import DEFAULTS

    duration = len(audio) / 16000.0 if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
    passes = duration >= float(DEFAULTS["min_record_seconds"]) and peak >= float(
        DEFAULTS["min_record_peak"]
    )
    return {
        "duration_s": round(duration, 2),
        "peak": round(peak, 4),
        "rms": round(rms, 4),
        "passes_gate": passes,
    }


def run_corpus(transcriber=None):
    transcriber = transcriber or build_transcriber()
    results = {}
    fixtures = sorted(f for f in os.listdir(AUDIO_DIR) if f.endswith(".wav"))
    for name in fixtures:
        audio = load_wav(os.path.join(AUDIO_DIR, name))
        entry = gate_decision(audio)
        if entry["passes_gate"]:
            entry.update(app_pipeline(transcriber, audio))
            entry.update(app_decision(entry))
        else:
            entry.update(
                {"raw_final": None, "cleaned": None, "would_paste": False,
                 "verdict": "dropped_by_gate"}
            )
        results[name] = entry
        shown = entry["cleaned"] if entry["would_paste"] else f"({entry['verdict']})"
        print(f"  {name:38s} -> {shown!r}")
    meta = {
        "model": transcriber.model_size,
        "device": transcriber.device,
        "compute_type": transcriber.compute_type,
    }
    return {"meta": meta, "results": results}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    print("Loading Whisper model...")
    t = build_transcriber()
    print(f"Model: {t.model_size} on {t.device} ({t.compute_type})\n")
    data = run_corpus(t)
    if mode == "baseline":
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"\nBaseline written to {BASELINE_PATH}")


if __name__ == "__main__":
    main()
