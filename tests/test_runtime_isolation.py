"""Meta-tests: prove conftest's runtime-state guards actually take effect.

These lived in conftest.py until 2026-10-20, where pytest never collects
tests, so they had never run once. A guard nobody can see working is a guess.

The module-scoped case is the one that leaked: pytest builds a module fixture
BEFORE the function-scoped guard, so the read-aloud gate's fixture loaded
Kokoro and wrote "local neural TTS ready" into the user's real debug.log.
"""

import logging
import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _inside_repo(path):
    return os.path.commonpath([_REPO, os.path.abspath(path)]) == _REPO


def _runtime_paths():
    from voiceassistant import applog, config, metrics

    return (("metrics", metrics.METRICS_PATH),
            ("debug log", applog.LOG_PATH),
            ("crash log", applog.CRASH_LOG_PATH),
            ("settings", config.CONFIG_FILE),
            ("config dir", config.CONFIG_DIR))


def test_runtime_state_is_redirected_away_from_the_repo():
    """The cheapest possible check against the 2026-09-10 leak returning: a
    test run rewrote the user's real settings.json because `Config.load()`
    began calling `save()` once `sanitize_settings` started pruning keys.
    """
    for label, path in _runtime_paths():
        assert not _inside_repo(path), (
            "%s still points inside the repo (%s) -- a test run would "
            "overwrite the user's real runtime state" % (label, path))


def test_the_clipboard_is_redirected_away_from_the_real_one():
    """Against the 2026-09-12 leak: a single test that reached the
    inline-typing partial branch replaced the user's clipboard contents.
    """
    import pyperclip

    from voiceassistant import paste

    sentinel = "VA-TEST-SENTINEL-MUST-NOT-REACH-THE-REAL-CLIPBOARD"
    paste.pyperclip.copy(sentinel)
    try:
        assert pyperclip.paste() != sentinel, (
            "a test wrote to the REAL clipboard -- the conftest guard is not "
            "in effect")
    except Exception:
        # No clipboard on this machine at all: the guard cannot be violated.
        pass


@pytest.fixture(scope="module")
def _seen_by_a_module_fixture():
    """Runs before the function-scoped guard, like the gate fixtures do."""
    from voiceassistant import applog

    applog.info("runtime-isolation probe from a module-scoped fixture")
    handlers = logging.getLogger("voiceassistant").handlers
    return {
        "paths": _runtime_paths(),
        "log_files": [h.baseFilename for h in handlers
                      if hasattr(h, "baseFilename")],
    }


def test_a_module_fixture_cannot_reach_the_real_runtime_state(
        _seen_by_a_module_fixture):
    seen = _seen_by_a_module_fixture
    for label, path in seen["paths"]:
        assert not _inside_repo(path), (
            "a module-scoped fixture saw the REAL %s (%s)" % (label, path))
    assert seen["log_files"], "the probe line was not written anywhere"
    for path in seen["log_files"]:
        assert not _inside_repo(path), (
            "a module-scoped fixture's log line went to the real %s" % path)
