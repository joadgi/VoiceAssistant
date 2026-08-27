r"""Slow objective gate for local read-aloud quality and consistency.

Opt in explicitly because this loads both real local models and performs many
syntheses.  It is silent: PCM is captured before VLC and scored through local
Whisper, so it does not play through the speakers.

    $env:RUN_TTS_EVAL="1"
    venv\Scripts\python.exe -m pytest tests/test_tts_eval.py -v -s
"""

import os
import re
import statistics

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_TTS_EVAL"),
    reason="local model-loading gate; set RUN_TTS_EVAL=1",
)


@pytest.fixture(scope="module")
def report():
    from tests.tts_eval_runner import run_evaluation

    result = run_evaluation(repeats=int(os.environ.get("TTS_EVAL_REPEATS", "3")))
    print("\n[TTS word-fidelity audit]")
    for row in result["audits"]:
        print(
            f"  {row['case']:28s} speed={row['speed']:.2f}x "
            f"similarity={row['word_similarity']:.3f} "
            f"WER={row['word_error_rate']:.3f} "
            f"audio={row['audio_seconds']:.2f}s "
            f"synth={row['synthesis_seconds']:.2f}s"
        )
    print(f"  bundled voices rendered: {len(result['voice_smoke'])}")
    return result


def _audit(report, case):
    return next(item for item in report["audits"] if item["case"] == case)


def test_user_report_is_cleaned_before_phonemization():
    from tests.tts_eval_runner import USER_REGRESSION_TEXT
    from voiceassistant.tts import prepare_text_for_speech

    spoken = prepare_text_for_speech(USER_REGRESSION_TEXT)
    assert "\\" not in spoken
    assert "C:/Users/" not in spoken
    assert "private-source" not in spoken
    assert "Nate’s latest transcript (line 22)" in spoken
    for required in (
        "Pick the outcomes AI should own",
        "Trigger.",
        "Required inputs.",
        "Human approval boundary.",
        "objectively reviewable",
    ):
        assert required in spoken


def test_user_report_repeats_preserve_words_consistently(report):
    rows = [
        item
        for item in report["audits"]
        if item["case"].startswith("user_regression_repeat_")
    ]
    expected_repeats = int(os.environ.get("TTS_EVAL_REPEATS", "3"))
    assert len(rows) == expected_repeats
    for row in rows:
        assert row["word_similarity"] >= 0.95, row
        assert row["word_error_rate"] <= 0.08, row
        assert row["audio_seconds"] > 5.0, row
    scores = [row["word_similarity"] for row in rows]
    assert max(scores) - min(scores) <= 0.02, scores


@pytest.mark.parametrize("speed", [1.0, 1.5, 1.98, 2.6])
def test_every_supported_speed_preserves_plain_prose(report, speed):
    row = _audit(report, f"speed_{speed:.2f}")
    threshold = 0.93 if speed == 2.6 else 0.96
    assert row["word_similarity"] >= threshold, row
    assert row["word_error_rate"] <= (0.12 if speed == 2.6 else 0.07), row


def test_generated_duration_tracks_requested_speed(report):
    baseline = _audit(report, "speed_1.00")["audio_seconds"]
    for speed in (1.5, 1.98, 2.6):
        duration = _audit(report, f"speed_{speed:.2f}")["audio_seconds"]
        effective_speed = baseline / duration
        assert abs(effective_speed - speed) / speed <= 0.14, (
            f"requested {speed:.2f}x but PCM duration measured {effective_speed:.2f}x"
        )


def test_technical_and_long_inputs_remain_ordered(report):
    technical = _audit(report, "technical_identifiers")
    long_read = _audit(report, "long_multibatch")
    # Whisper rewrites spelled identifiers and currency nondeterministically
    # ("B zero"/"B0", "four"/"for", spoken units/"$69.99"). Test the
    # technical payload semantically instead of applying a false exact-token
    # penalty to those valid representations.
    transcript = technical["transcript"].lower()
    anchors = [
        "parent",
        "variation containers",
        "child",
        "discoverable",
        "offer belongs",
        "selected child",
        "offer price",
        "fulfillment",
        "amazon na",
    ]
    positions = [transcript.find(anchor) for anchor in anchors]
    assert all(position >= 0 for position in positions), technical
    assert positions == sorted(positions), technical

    identifier_match = re.search(
        r"child\s+(?:a[\s-]?sin|asin)\s+(.+?)\s+is\s+discoverable",
        transcript,
    )
    assert identifier_match, technical
    identifier = identifier_match.group(1)
    for word, digit in {
        "zero": "0",
        "oh": "0",
        "one": "1",
        "two": "2",
        "to": "2",
        "three": "3",
        "four": "4",
        "for": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }.items():
        identifier = re.sub(rf"\b{word}\b", digit, identifier)
    assert re.sub(r"[^a-z0-9]", "", identifier) == "b0cpmp4cvq", technical
    assert "$69.99" in transcript or (
        "69" in transcript and "99" in transcript
    ), technical

    assert long_read["word_similarity"] >= 0.96, long_read
    assert long_read["word_error_rate"] <= 0.07, long_read
    assert long_read["audio_seconds"] > 20.0, long_read


def test_every_bundled_local_voice_renders_valid_audio(report):
    from voiceassistant.tts import LOCAL_NEURAL_VOICES

    rows = report["voice_smoke"]
    assert {row["voice"] for row in rows} == {
        voice for _label, voice in LOCAL_NEURAL_VOICES
    }
    for row in rows:
        assert 1.0 < row["audio_seconds"] < 20.0, row
        assert 0.01 < row["peak"] <= 1.0, row
    # A failed voice lookup often returns the same default waveform. Natural
    # voices can have similar lengths, but all six should not be identical.
    assert len({round(row["audio_seconds"], 3) for row in rows}) >= 4


def test_repeat_metrics_are_not_suspiciously_variable(report):
    rows = [
        item
        for item in report["audits"]
        if item["case"].startswith("user_regression_repeat_")
    ]
    durations = [row["audio_seconds"] for row in rows]
    assert statistics.pstdev(durations) <= 0.05, durations
