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
