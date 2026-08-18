"""Metrics: privacy, robustness, and that the report says something useful.

The point of this module is that "dictation feels unreliable" becomes a
measurable claim, so the tests that matter are: it never records transcript
text, it never breaks dictation when the disk misbehaves, and the summary
actually surfaces the failure modes.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voiceassistant import metrics  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(metrics, "METRICS_PATH", str(path))
    return path


# --------------------------------------------------------------------------- #
# Privacy — the same rule applog has: never write payloads.
# --------------------------------------------------------------------------- #
def test_never_writes_transcript_text(store):
    secret = "my bank password is hunter2"
    metrics.record(metrics.OUTCOME_PASTED, chars=len(secret), hold_s=1.0)
    body = store.read_text(encoding="utf-8")
    assert secret not in body
    assert "hunter2" not in body
    row = json.loads(body.strip())
    assert row["chars"] == len(secret), "character COUNT is what we keep"
    assert not any(isinstance(v, str) and " " in v for k, v in row.items()
                   if k != "outcome"), f"suspicious free text in {row}"


def test_record_never_raises_even_when_the_path_is_unwritable(monkeypatch, tmp_path):
    # A metrics failure must never break a dictation.
    monkeypatch.setattr(metrics, "METRICS_PATH", str(tmp_path / "nope" / "x.jsonl"))
    metrics.record(metrics.OUTCOME_PASTED, hold_s=1.0)  # must not raise


def test_corrupt_lines_are_skipped_not_fatal(store):
    metrics.record(metrics.OUTCOME_PASTED, hold_s=1.0)
    with open(store, "a", encoding="utf-8") as f:
        f.write("{not json at all\n\n")
    metrics.record(metrics.OUTCOME_NO_SPEECH, hold_s=0.4)
    rows = metrics.load()
    assert len(rows) == 2, "a corrupt line must not lose the good records"


def test_file_rolls_instead_of_growing_forever(store, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_BYTES", 400)
    for _ in range(120):
        metrics.record(metrics.OUTCOME_PASTED, hold_s=1.234, peak=0.25, chars=42)
    assert os.path.getsize(store) <= 400 + 512
    assert os.path.exists(str(store) + ".1"), "no rolled file — history lost"
    assert metrics.load(), "rolled history unreadable"


# --------------------------------------------------------------------------- #
# The summary has to surface the things worth acting on.
# --------------------------------------------------------------------------- #
def test_summary_separates_clean_successes_from_problems(store):
    for _ in range(7):
        metrics.record(metrics.OUTCOME_PASTED, transcribe_ms=400.0, hold_s=1.5, peak=0.3)
    metrics.record(metrics.OUTCOME_DROPPED_QUIET, peak=0.001, hold_s=1.0)
    metrics.record(metrics.OUTCOME_PASTE_FAILED, transcribe_ms=500.0)
    metrics.record(metrics.OUTCOME_PASTED, transcribe_ms=2000.0, keyup_lost=True)

    s = metrics.summarize(metrics.load())
    assert s["total"] == 10
    assert s["counts"][metrics.OUTCOME_PASTED] == 8
    assert 0.79 < s["success_rate"] < 0.81, s["success_rate"]
    assert s["lost_keyups_rescued"] == 1
    assert s["transcribe_ms_p95"] >= s["transcribe_ms_p50"]


def test_report_names_the_actual_problem(store):
    metrics.record(metrics.OUTCOME_DROPPED_QUIET, peak=0.001)
    metrics.record(metrics.OUTCOME_PASTE_FAILED)
    metrics.record(metrics.OUTCOME_PASTED, keyup_lost=True, transcribe_ms=300.0)
    text = metrics.format_report(metrics.load())
    assert "gain knob" in text, "quiet drops must point at the mic level"
    assert "clipboard" in text, "paste failure must say the text isn't lost"
    assert "keyboard hook" in text, "rescued keyups must be explained"
    assert text.isascii(), "report must be cp1252-safe for a Windows console"


def test_empty_report_is_not_an_error(store):
    assert "No dictation metrics" in metrics.format_report(metrics.load())
