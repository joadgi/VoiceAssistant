"""Shared test guards.

The rule these enforce: a test run must never touch the user's real runtime
state. There is prior art for why — a live capture stream outliving a fixture
segfaulted the suite, so every MainWindow harness stubs `open_stream`. Metrics
are the same shape of problem: `MainWindow` records a metrics row at every
terminal state of a dictation, so simply running the dictation-flow tests wrote
dozens of synthetic rows into the real `metrics.jsonl` and corrupted the
`--report` baseline the user is meant to trust.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolate_metrics(tmp_path, monkeypatch):
    """Redirect metrics writes into the test's tmp dir, always.

    autouse on purpose: any test that drives MainWindow records metrics whether
    or not it is "about" metrics, so opting in per-test would silently leak.
    """
    from voiceassistant import metrics

    monkeypatch.setattr(metrics, "METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    yield
