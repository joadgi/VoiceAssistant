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

    settings.json is the third of these, found 2026-09-10: `test_selfcheck.py`
    runs the real self-check, whose whole job is to validate the user's REAL
    config, so it builds `Config()` against the live path. That was harmless
    only while `load()` never wrote anything back. The moment
    `sanitize_settings` began pruning obsolete keys, `sanitized != _data`
    became true on load and `load()` called `save()` -- so simply running the
    fast suite rewrote the user's settings.json. A latent leak became a live
    one because of a change nowhere near the tests. Redirect it for every
    test; the handful that patch CONFIG_FILE themselves still win, since their
    own monkeypatch applies later. CONFIG_DIR must move too: `save()` puts its
    temp file there before `os.replace`, so leaving it pointed at the repo
    littered `settings.*.tmp` next to the real file.
    """
    from voiceassistant import applog, config, metrics

    _drop_applog_handlers(applog)
    monkeypatch.setattr(metrics, "METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(applog, "LOG_PATH", str(tmp_path / "debug.log"))
    monkeypatch.setattr(applog, "CRASH_LOG_PATH", str(tmp_path / "crash.log"))
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "settings.json"))
    # The logger is built once and cached against LOG_PATH, so it has to be
    # dropped for the redirect to take effect (and again afterwards, so the
    # next test rebuilds against ITS tmp dir).
    try:
        yield
    finally:
        _drop_applog_handlers(applog)


def test_runtime_state_is_redirected_away_from_the_repo():
    """Meta-test: prove the autouse redirect actually took effect.

    Without this the guard is unverifiable from inside the suite. It is the
    cheapest possible check against the 2026-09-10 leak returning: a test run
    rewrote the user's real settings.json because `Config.load()` began
    calling `save()` once `sanitize_settings` started pruning obsolete keys.
    """
    import os

    from voiceassistant import applog, config, metrics

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for label, path in (("metrics", metrics.METRICS_PATH),
                        ("debug log", applog.LOG_PATH),
                        ("crash log", applog.CRASH_LOG_PATH),
                        ("settings", config.CONFIG_FILE),
                        ("config dir", config.CONFIG_DIR)):
        assert os.path.commonpath([repo, os.path.abspath(path)]) != repo, (
            "%s still points inside the repo (%s) -- a test run would "
            "overwrite the user's real runtime state" % (label, path))
