"""The corpus GATE — the objective definition of "dictation is trustworthy".

Runs the real Whisper pipeline over the golden fixtures and asserts the
product-level contract:
  * noise / silence / breath NEVER produces pasteable text,
  * real speech (even quiet) is NEVER dropped,
  * repeat semantics: artifacts collapse, intentional repeats survive.

Local-only (needs the cached model; GPU preferred). Opt in explicitly:

    $env:RUN_CORPUS="1"; venv\\Scripts\\python.exe -m pytest tests/test_corpus_gate.py -v

Speech assertions are tolerance-based (contains-words), never exact-match —
Whisper on GPU is not bit-deterministic.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_CORPUS"),
    reason="corpus gate is a local, model-loading run — set RUN_CORPUS=1",
)


@pytest.fixture(scope="module")
def corpus():
    from tests.corpus_runner import build_transcriber, run_corpus

    return run_corpus(build_transcriber())["results"]


# --- Noise must never paste --------------------------------------------------
def test_silence_dropped_by_gate(corpus):
    assert corpus["silence_2s.wav"]["verdict"] == "dropped_by_gate"


@pytest.mark.parametrize(
    "fixture",
    ["breath_click_0p35s.wav", "room_noise_3s.wav", "room_noise_quiet_3s.wav"],
)
def test_noise_never_pastes(corpus, fixture):
    entry = corpus[fixture]
    assert not entry["would_paste"], (
        f"{fixture} produced pasteable text {entry['cleaned']!r} "
        f"(verdict={entry['verdict']}) — hallucination gate failed"
    )


# --- Real speech must never be lost ------------------------------------------
def test_quiet_speech_not_dropped(corpus):
    entry = corpus["quiet_yes.wav"]
    assert entry["would_paste"], f"quiet real speech was dropped: {entry}"
    assert "yes" in entry["cleaned"].lower()


def test_whisper_level_speech_not_dropped(corpus):
    # Review-F2 guard: speech just above the record gate must survive the
    # whole pipeline (VAD may miss it; the retry must bring it back and the
    # segment filters must not zero it).
    entry = corpus["whisper_quiet_yes.wav"]
    assert entry["would_paste"], f"whisper-level speech was dropped: {entry}"
    assert "yes" in entry["cleaned"].lower()


def test_quiet_thank_you_not_eaten_by_denylist(corpus):
    # Review-F2 guard: "thank you" is real dictation, not a hallucination.
    entry = corpus["quiet_thank_you.wav"]
    assert entry["would_paste"], (
        f"real 'thank you' was suppressed (verdict={entry['verdict']}) — "
        f"denylist regression"
    )
    assert "thank you" in entry["cleaned"].lower()


def test_normal_sentence_transcribed(corpus):
    entry = corpus["normal_sentence.wav"]
    assert entry["would_paste"]
    assert "quarterly report" in entry["cleaned"].lower()


def test_numbers_dates_transcribed(corpus):
    entry = corpus["numbers_dates.wav"]
    assert entry["would_paste"]
    low = entry["cleaned"].lower()
    assert "meeting" in low and "july" in low


def test_long_paragraph_no_hallucinated_tail(corpus):
    entry = corpus["long_paragraph_trailing_silence.wav"]
    assert entry["would_paste"]
    low = entry["cleaned"].lower()
    assert "first point" in low and "third point" in low
    for artifact in ("thanks for watching", "thank you", "subscribe"):
        assert artifact not in low, f"hallucinated tail: {artifact!r} in {low!r}"


# --- Repeat semantics ---------------------------------------------------------
def test_phrase_double_collapsed(corpus):
    entry = corpus["repeat_phrase_x2.wav"]
    assert entry["would_paste"]
    assert entry["cleaned"].lower().count("send") == 1, (
        f"phrase repeat not collapsed: {entry['cleaned']!r}"
    )


def test_intentional_triple_preserved(corpus):
    entry = corpus["intentional_no_no_no.wav"]
    assert entry["would_paste"]
    assert entry["cleaned"].lower().count("no") >= 3, (
        f"intentional repeat was eaten: {entry['cleaned']!r}"
    )


# --- Latency (informational bound, generous for CPU fallback) -----------------
def test_latency_within_budget(corpus):
    for name, entry in corpus.items():
        if entry.get("total_ms") is None:
            continue
        assert entry["total_ms"] < 15000, f"{name} took {entry['total_ms']}ms"
