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
import logging

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _drop_applog_handlers(applog):
    """Close every cached handler before changing LOG_PATH.

    Setting ``applog._logger = None`` is not enough: logging.getLogger returns
    the same named Logger object, whose old RotatingFileHandler keeps writing
    to the real debug.log. Collection-time imports can initialize that handler
    before the per-test fixture begins, so remove it explicitly at both ends.
    """
    logger = logging.getLogger("voiceassistant")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    applog._logger = None


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """Redirect metrics AND logs into the test's tmp dir, always.

    autouse on purpose: any test that drives MainWindow records metrics and
    logs whether or not it is "about" either, so opting in per-test leaks.

    The log redirect matters as much as the metrics one. Fault-injection tests
    deliberately log things like "mic stream open failed: simulated: no capture
    device" and "worker 'test' job failed" -- 279 such lines had accumulated in
    the real debug.log, which is the file the user is told to read when
    dictation misbehaves. Test noise in a diagnostic log is worse than no log.
    """
    from voiceassistant import applog, metrics

    _drop_applog_handlers(applog)
    monkeypatch.setattr(metrics, "METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(applog, "LOG_PATH", str(tmp_path / "debug.log"))
    monkeypatch.setattr(applog, "CRASH_LOG_PATH", str(tmp_path / "crash.log"))
    # The logger is built once and cached against LOG_PATH, so it has to be
    # dropped for the redirect to take effect (and again afterwards, so the
    # next test rebuilds against ITS tmp dir).
    try:
        yield
    finally:
        _drop_applog_handlers(applog)
