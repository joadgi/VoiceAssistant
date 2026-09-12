"""Self-check diagnostic tests.

Guards: it returns a proper exit code, never crashes, and its output is
ASCII-safe (it runs in cp1252 Windows consoles where a stray non-ASCII
char would raise UnicodeEncodeError — a diagnostic must not die on print).
"""

import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voiceassistant.selfcheck import run_selfcheck, CHECKS


def test_returns_exit_code_and_runs():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_selfcheck(deep=False)
    assert rc in (0, 1)
    out = buf.getvalue()
    assert "self-check" in out
    assert "RESULT:" in out
    # Every check must appear in the report.
    for label, _required, _fn in CHECKS:
        assert label in out, f"missing check in report: {label}"


def test_output_is_ascii_safe():
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_selfcheck(deep=False)
    out = buf.getvalue()
    # Must encode to cp1252 (the default Windows console codepage) without error.
    out.encode("cp1252")  # raises UnicodeEncodeError if a stray char slipped in


def test_no_probe_raises():
    # Each probe must swallow its own failures and return (bool, str).
    for label, _required, fn in CHECKS:
        ok, detail = fn()
        assert isinstance(ok, bool), label
        assert isinstance(detail, str), label


class TestConfiguredMicrophoneIsChecked:
    """`--check` must validate the mic the user CHOSE, not just that some mic
    exists. A self-check that passes when the thing it checks is broken is
    worse than no self-check. (Audit finding, 2026-09-12.)"""

    @staticmethod
    def _devices():
        return [
            {"name": "Speakers", "max_input_channels": 0},
            {"name": "Yeti", "max_input_channels": 2},
        ]

    def _run(self, monkeypatch, chosen, devices=None):
        import sounddevice as sd

        from voiceassistant import config as cfg
        from voiceassistant import selfcheck

        monkeypatch.setattr(sd, "query_devices",
                            lambda: self._devices() if devices is None else devices)

        class _Cfg:
            def get(self, key, default=None):
                return chosen

        monkeypatch.setattr(cfg, "Config", _Cfg)
        return selfcheck._check_microphone()

    def test_system_default_passes(self, monkeypatch):
        ok, msg = self._run(monkeypatch, -1)
        assert ok and "system default" in msg

    def test_a_present_selected_device_passes_and_is_named(self, monkeypatch):
        ok, msg = self._run(monkeypatch, 1)
        assert ok and "Yeti" in msg

    def test_a_missing_selected_device_FAILS(self, monkeypatch):
        ok, msg = self._run(monkeypatch, 7)
        assert not ok, "reported PASS for a microphone that is gone"
        assert "no longer present" in msg

    def test_a_selected_output_only_device_FAILS(self, monkeypatch):
        ok, msg = self._run(monkeypatch, 0)
        assert not ok, "reported PASS for a device with no input channels"
        assert "input channels" in msg

    def test_no_input_devices_at_all_fails(self, monkeypatch):
        ok, msg = self._run(monkeypatch, -1,
                            devices=[{"name": "Speakers", "max_input_channels": 0}])
        assert not ok and "no input devices" in msg
