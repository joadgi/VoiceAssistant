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


def _redirect_runtime_state(mp, directory):
    """Point every piece of real runtime state at `directory` via `mp`."""
    from voiceassistant import applog, config, metrics, paste

    _drop_applog_handlers(applog)
    # THE CLIPBOARD IS THE FOURTH LEAK. `Paster._finalize_inline_job` copies the
    # accurate text to the clipboard whenever inline typing cannot be
    # reconciled, so every test that reaches that branch without patching
    # pyperclip overwrites whatever the user had copied. Proven during the
    # 2026-09-12 audit by putting a sentinel on the real clipboard and watching
    # a single test replace it with "yyyyyyyyyy". Same rule as metrics, logs
    # and settings: a test run must never touch real runtime state. Tests that
    # care what was copied patch `paste.pyperclip` themselves and still win,
    # because their monkeypatch applies after this one.
    mp.setattr(paste.pyperclip, "copy", lambda text: None, raising=False)
    mp.setattr(paste.pyperclip, "paste", lambda: "", raising=False)
    mp.setattr(metrics, "METRICS_PATH", os.path.join(directory, "metrics.jsonl"))
    mp.setattr(applog, "LOG_PATH", os.path.join(directory, "debug.log"))
    mp.setattr(applog, "CRASH_LOG_PATH", os.path.join(directory, "crash.log"))
    mp.setattr(config, "CONFIG_DIR", directory)
    mp.setattr(config, "CONFIG_FILE", os.path.join(directory, "settings.json"))


@pytest.fixture(autouse=True, scope="session")
def _isolate_runtime_state_for_the_session(tmp_path_factory):
    """The same redirect, in place before ANY fixture runs.

    pytest builds higher-scoped fixtures first, so a `scope="module"` fixture
    is set up BEFORE the function-scoped guard below and used to see the real
    paths. Found 2026-10-20: the read-aloud gate's module fixture loads Kokoro,
    and `TTSEngine._load_kokoro` logged "local neural TTS ready" into the
    user's real debug.log. Nineteen module/class-scoped fixtures exist under
    tests/, several of them driving real engines. This one covers them all; the
    per-test guard still gives every test its own fresh directory.
    """
    from voiceassistant import applog

    with pytest.MonkeyPatch.context() as mp:
        _redirect_runtime_state(mp, str(tmp_path_factory.mktemp("runtime-state")))
        try:
            yield
        finally:
            _drop_applog_handlers(applog)


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
    from voiceassistant import applog

    _redirect_runtime_state(monkeypatch, str(tmp_path))
    # The logger is built once and cached against LOG_PATH, so it has to be
    # dropped for the redirect to take effect (and again afterwards, so the
    # next test rebuilds against ITS tmp dir).
    try:
        yield
    finally:
        _drop_applog_handlers(applog)


# The meta-tests proving these guards work live in test_runtime_isolation.py.
# They used to be defined HERE, where pytest never collects tests, so for
# their whole life they never ran.
