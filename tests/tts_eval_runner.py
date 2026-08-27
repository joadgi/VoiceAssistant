r"""Objective, local-only evaluation harness for Kokoro read-aloud.

This is deliberately separate from the fast unit suite.  It loads the real
Kokoro model, renders through ``TTSEngine._speak_local`` (the production block
streaming path), and transcribes the resulting PCM with a cached local Whisper
model.  That catches swallowed, invented, or reordered words without relying
on a person listening for every regression.

Run through the pytest gate documented in tests/README.md.  The runner can also
be invoked directly to print a JSON report:

    venv\Scripts\python.exe tests\tts_eval_runner.py
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass

import numpy as np


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


USER_REGRESSION_TEXT = r"""The biggest mistake for the remainder of 2026 would be making AI strategy primarily about choosing the “best” model. The real work is deciding where AI will own a measurable business outcome and building the operating system around it.
1\. Pick the outcomes AI should own
Define two or three bounded workflows, not a broad “AI assistant.”
For each workflow, specify:
\- Trigger
\- Required inputs
\- Accountable owner
\- Verified outcome
\- Time and cost target
\- Failure consequence
\- Human approval boundary
Choose work that is frequent, painful, and objectively reviewable. Coding has moved quickly partly because correctness is comparatively verifiable. Ambiguous work creates much more supervision and recovery overhead. [Nate’s latest transcript (line 22)](</C:/Users/josh.g/Documents/AI Agent Operating System/harness/workspace/codex/nate-b-jones-youtube-knowledge-base/transcripts/20260826_IpEaSa7tgfc.md:22>)"""

PLAIN_SPEED_TEXT = (
    "Reliable speech must preserve every word in its original order. "
    "The engine should speak complete sentences without skipping ahead, "
    "repeating an earlier phrase, or inventing words between clauses. "
    "Playback speed may change, but the meaning and pitch must remain stable."
)

TECHNICAL_TEXT = (
    "Parent ASINs are non-buyable variation containers. Child ASIN B0CPMP4CVQ "
    "is discoverable, while the buyable offer belongs to the selected child. "
    "The offer price is $69.99 and fulfillment is handled by AMAZON_NA."
)

LONG_MULTIBATCH_TEXT = " ".join(
    [
        "Section one defines the trigger, required inputs, accountable owner, "
        "verified outcome, time target, failure consequence, and approval boundary.",
        "Section two checks that a long selection stays in one ordered stream. "
        "It must not jump forward when synthesis finishes ahead of playback.",
        "Section three checks punctuation pauses and transitions between native "
        "speech batches without adding an artificial seam.",
        "Section four confirms that later sentences remain available until their "
        "samples have actually reached the output device.",
        "Section five follows a customer request from intake through review and "
        "records the person responsible for the final decision.",
        "Section six preserves a measured cost target while describing what the "
        "system should do when a required input is missing.",
        "Section seven names the handoff between automated work and human approval "
        "so the reader can hear the complete operating boundary.",
        "Section eight verifies that commas, semicolons, and periods retain natural "
        "pauses even when the requested playback rate is high.",
        "Section nine covers a second workflow with different vocabulary to prevent "
        "a repeated paragraph from hiding an ordering defect.",
        "Section ten confirms that buffered audio cannot report completion while "
        "unheard samples remain queued behind the player.",
        "Section eleven checks that no earlier sentence is repeated after the final "
        "native speech batch has already been prepared.",
        "Section twelve closes the evaluation with a distinct ending that must be "
        "present in the recovered transcript.",
    ]
)


@dataclass(frozen=True)
class AudioAudit:
    case: str
    voice: str
    speed: float
    source_chars: int
    spoken_chars: int
    audio_seconds: float
    synthesis_seconds: float
    transcript: str
    reference_words: int
    transcript_words: int
    word_similarity: float
    word_error_rate: float


def _words(text: str) -> list[str]:
    """Normalize prose for ordered word comparison.

    Apostrophes and underscores are not meaningful boundaries for this gate;
    punctuation/Markdown presentation is evaluated separately.  Numbers remain
    visible so a missing year or price still lowers fidelity.
    """
    from voiceassistant.tts import prepare_text_for_speech

    normalized = (
        prepare_text_for_speech(str(text))
        .lower()
        .replace("’", "'")
        .replace("_", " ")
    )
    words = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", normalized)
    number_words = {
        "zero": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }
    return [number_words.get(word, word) for word in words]


def _edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, ref_word in enumerate(reference, start=1):
        current = [row]
        for column, hyp_word in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (ref_word != hyp_word),
                )
            )
        previous = current
    return previous[-1]


def word_scores(reference: str, hypothesis: str) -> tuple[float, float, int, int]:
    ref_words = _words(reference)
    hyp_words = _words(hypothesis)
    similarity = difflib.SequenceMatcher(
        None, ref_words, hyp_words, autojunk=False
    ).ratio()
    distance = _edit_distance(ref_words, hyp_words)
    error_rate = distance / max(1, len(ref_words))
    return similarity, error_rate, len(ref_words), len(hyp_words)


def resample_for_whisper(audio: np.ndarray, source_rate: int = 24_000) -> np.ndarray:
    """Convert production 24 kHz mono PCM to Whisper's 16 kHz float input."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not audio.size or source_rate == 16_000:
        return audio
    output_size = max(1, round(audio.size * 16_000 / source_rate))
    source_positions = np.arange(audio.size, dtype=np.float64)
    output_positions = np.linspace(0, audio.size - 1, output_size)
    return np.interp(output_positions, source_positions, audio).astype(np.float32)


class LocalTTSEvaluator:
    """Own the real local TTS and ASR models for one evaluation session."""

    def __init__(self, whisper_model: str = "base"):
        from voiceassistant import applog
        from voiceassistant.tts import TTSEngine

        # The gate deliberately triggers failure paths and detailed TTS timing
        # logs. Keep those out of the user's real diagnostic files whether this
        # runs under pytest or through the documented standalone entry point.
        self._runtime_dir = tempfile.TemporaryDirectory(prefix="voiceassist_tts_eval_")
        self._old_log_paths = (
            applog.LOG_PATH,
            applog.CRASH_LOG_PATH,
        )
        self._reset_applog()
        applog.LOG_PATH = os.path.join(self._runtime_dir.name, "debug.log")
        applog.CRASH_LOG_PATH = os.path.join(self._runtime_dir.name, "crash.log")
        self.engine = TTSEngine(volume=0.0)
        self.engine._kokoro = self.engine._load_kokoro()
        self._whisper_name = whisper_model
        self._whisper = None

    def close(self):
        from voiceassistant import applog

        try:
            self.engine.shutdown()
        finally:
            self._reset_applog()
            applog.LOG_PATH, applog.CRASH_LOG_PATH = self._old_log_paths
            self._runtime_dir.cleanup()

    @staticmethod
    def _reset_applog():
        from voiceassistant import applog

        logger = logging.getLogger("voiceassistant")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        applog._logger = None

    def _load_whisper(self):
        if self._whisper is not None:
            return self._whisper
        from faster_whisper import WhisperModel

        # Never turn an eval into an undocumented model download. Setup and the
        # dictation corpus are responsible for populating this local cache.
        self._whisper = WhisperModel(
            self._whisper_name,
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
        return self._whisper

    def render(self, text: str, voice: str, speed: float) -> tuple[str, np.ndarray, float]:
        """Capture bytes from the exact production local block-stream path."""
        from voiceassistant.tts import prepare_text_for_speech

        spoken = prepare_text_for_speech(text)
        self.engine._use_local = True
        self.engine._use_offline = False
        self.engine._local_voice_id = voice
        self.engine._speed = speed
        captured = []

        original_start = self.engine._start_vlc_pcm_stream
        original_wait = self.engine._wait_vlc_stream

        def capture_start(stream, _stop_event, progress):
            captured.append(stream)
            progress.append("eval-captured-local-stream")

        def capture_wait(stream, _stop_event):
            # _speak_local calls this only after every native Kokoro batch was
            # generated and stream.finish() was set, so draining cannot block.
            while True:
                chunk = stream.read(1 << 20)
                if chunk is None:
                    raise RuntimeError("local eval stream was cancelled")
                if not chunk:
                    return
                captured.append(chunk)

        self.engine._start_vlc_pcm_stream = capture_start
        self.engine._wait_vlc_stream = capture_wait
        started = time.perf_counter()
        try:
            self.engine._speak_local(spoken, threading.Event(), [])
        finally:
            self.engine._start_vlc_pcm_stream = original_start
            self.engine._wait_vlc_stream = original_wait
        synthesis_seconds = time.perf_counter() - started

        if not captured or not hasattr(captured[0], "read"):
            raise RuntimeError("production local path did not open one PCM stream")
        pcm_chunks = [item for item in captured[1:] if isinstance(item, bytes)]
        if not pcm_chunks:
            raise RuntimeError("production local path returned no PCM")
        pcm = np.frombuffer(b"".join(pcm_chunks), dtype="<i2")
        audio = pcm.astype(np.float32) / 32768.0
        if not np.isfinite(audio).all() or not np.max(np.abs(audio)) > 0.001:
            raise RuntimeError("production local path returned silent or invalid PCM")
        return spoken, audio, synthesis_seconds

    def transcribe(self, audio: np.ndarray) -> str:
        model = self._load_whisper()
        segments, _info = model.transcribe(
            resample_for_whisper(audio),
            language="en",
            beam_size=5,
            best_of=5,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def audit(self, case: str, text: str, voice: str, speed: float) -> AudioAudit:
        spoken, audio, synth_s = self.render(text, voice, speed)
        transcript = self.transcribe(audio)
        similarity, wer, ref_count, hyp_count = word_scores(spoken, transcript)
        return AudioAudit(
            case=case,
            voice=voice,
            speed=speed,
            source_chars=len(text),
            spoken_chars=len(spoken),
            audio_seconds=audio.size / 24_000.0,
            synthesis_seconds=synth_s,
            transcript=transcript,
            reference_words=ref_count,
            transcript_words=hyp_count,
            word_similarity=similarity,
            word_error_rate=wer,
        )


def run_evaluation(repeats: int = 3) -> dict:
    from voiceassistant.tts import LOCAL_NEURAL_VOICES

    evaluator = LocalTTSEvaluator(
        whisper_model=os.environ.get("TTS_EVAL_WHISPER_MODEL", "base")
    )
    audits = []
    smoke = []
    try:
        for iteration in range(repeats):
            audits.append(
                evaluator.audit(
                    f"user_regression_repeat_{iteration + 1}",
                    USER_REGRESSION_TEXT,
                    "am_michael",
                    1.98,
                )
            )

        for speed in (1.0, 1.5, 1.98, 2.6):
            audits.append(
                evaluator.audit(
                    f"speed_{speed:.2f}", PLAIN_SPEED_TEXT, "am_michael", speed
                )
            )

        audits.append(
            evaluator.audit(
                "technical_identifiers", TECHNICAL_TEXT, "am_michael", 1.98
            )
        )
        audits.append(
            evaluator.audit(
                "long_multibatch", LONG_MULTIBATCH_TEXT, "am_michael", 1.98
            )
        )

        # All bundled voices must synthesize valid, non-silent PCM. ASR scoring
        # every voice adds noise from accent/style to a machinery smoke test, so
        # the detailed word gate stays on the user's selected Michael voice.
        for _label, voice in LOCAL_NEURAL_VOICES:
            spoken, audio, synth_s = evaluator.render(
                "Every bundled local voice must render this complete sentence.",
                voice,
                1.0,
            )
            smoke.append(
                {
                    "voice": voice,
                    "spoken_chars": len(spoken),
                    "audio_seconds": audio.size / 24_000.0,
                    "synthesis_seconds": synth_s,
                    "peak": float(np.max(np.abs(audio))),
                }
            )
    finally:
        evaluator.close()

    return {
        "whisper_model": os.environ.get("TTS_EVAL_WHISPER_MODEL", "base"),
        "repeat_count": repeats,
        "audits": [asdict(item) for item in audits],
        "voice_smoke": smoke,
    }


def main():
    report = run_evaluation(int(os.environ.get("TTS_EVAL_REPEATS", "3")))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
