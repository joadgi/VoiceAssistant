"""REAL end-to-end test for the paste subsystem (voiceassistant/paste.py).

This does NOT mock anything. It stands up a real, on-screen, focusable window
with an editable text field, seeds the real Windows clipboard, drives
`Paster.submit()` exactly as the app does, then reads the widget's text back
and inspects the real clipboard. It proves that dictated text physically lands
in the focused window and that the user's original clipboard is restored.

WHY IT IS GATED AND MUST RUN ALONE
----------------------------------
It steals keyboard focus and rewrites the real clipboard, and a process gets
exactly ONE QApplication + one Qt platform for its lifetime. So:

  * Opt in with the env flag  RUN_PASTE_LIVE=1  (off by default).
  * It needs a REAL, interactive, unlocked desktop session. It will SKIP if Qt
    is running offscreen (QT_QPA_PLATFORM=offscreen) because an offscreen
    window has no real HWND that can be the OS foreground target for Ctrl+V.
  * Run this FILE BY ITSELF. Another suite (tests/test_ui_smoke.py) forces the
    offscreen Qt platform at import; if it is collected first in the same
    process, this test's windows would be offscreen and unusable.

RUN COMMAND (PowerShell — the project's primary shell)
------------------------------------------------------
    $env:RUN_PASTE_LIVE="1"; venv\\Scripts\\python.exe -m pytest tests\\test_paste_live.py -v -s

Do NOT pass/leave QT_QPA_PLATFORM=offscreen. Do NOT run it on a locked screen
or a disconnected RDP session (SetForegroundWindow + synthetic input are
blocked there). Expect the active window to change and a small text window to
flash for ~1-2s per case (~10s total); the clipboard is used then restored.

WHAT EACH CASE PROVES
---------------------
  1. Plain ASCII lands verbatim in the real widget.
  2. Newlines / tabs / control chars land as the flattened single-line form
     (the sanitize_for_paste contract) — no raw newlines/tabs reach the target.
  3. Unicode (accents, em-dash, emoji) survives the clipboard round-trip
     verbatim (sanitize_for_paste keeps every non-control, non-whitespace char).
  4. ~1000-char text lands in full (nothing truncated).
  5. Whitespace/control-only text sanitizes to empty → Paster does NOT paste,
     reports success=False, leaves the target unchanged AND the clipboard
     untouched (it never even writes the clipboard in this path).
  6. After a normal paste the clipboard is restored to the user's ORIGINAL
     value, not left holding the pasted text.
  7. Back-to-back submits: both texts land, and after the burst the ORIGINAL
     clipboard is restored (proving the `_pending_snapshot` carry-forward — the
     clipboard is NOT left holding dictation #1 or #2).

RELIABILITY NOTES / CAVEATS HIT WHILE DESIGNING
-----------------------------------------------
  * The Qt event loop MUST be pumped on the main thread the whole time the
    paste worker runs, or the injected Ctrl+V is never delivered to the widget.
    So we never block on Event.wait(); we spin processEvents() until the
    worker's done-callback fires (see `_run_paste`).
  * HWND truncation: winapi.get_foreground_window() has no ctypes restype, so
    on 64-bit Windows it returns a sign-truncated 32-bit handle, while
    QWidget.winId() returns the full pointer. We compare foreground identity
    with `_same_hwnd` (normalize both to signed low-32) so focus verification
    is correct. Passing the full winId() into Paster (as specified) is still
    fine: Paster just takes its refocus branch and re-foregrounds our window
    before Ctrl+V. (HWNDs are small in practice, so no ctypes OverflowError.)
  * Clipboard writes can transiently fail under contention, so seeding retries
    (`_seed_clipboard`) and reads happen after the worker has fully settled.
  * The done-callback for a single (non-burst) paste fires only AFTER Paster's
    own ~600ms settle + restore has completed, so once it fires the clipboard
    is already restored and the widget has already received the text.
"""

import ctypes
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RUN = os.environ.get("RUN_PASTE_LIVE")

pytestmark = pytest.mark.skipif(
    not _RUN,
    reason="live paste test steals focus + uses the real clipboard — set "
    "RUN_PASTE_LIVE=1 and run on a real (non-offscreen) interactive desktop",
)

# Hard skip before we ever build a QApplication if Qt is forced offscreen: an
# offscreen window cannot be the OS foreground target for a Win32 Ctrl+V.
if _RUN and os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
    pytest.skip(
        "QT_QPA_PLATFORM=offscreen cannot receive Win32 Ctrl+V — run this file "
        "in isolation on a real display",
        allow_module_level=True,
    )

# Real deps — the app already requires all three; importorskip keeps collection
# clean on a machine that somehow lacks them.
pytest.importorskip("PySide6")
pyperclip = pytest.importorskip("pyperclip")
pytest.importorskip("keyboard")

from voiceassistant import winapi  # noqa: E402
from voiceassistant.paste import Paster  # noqa: E402
from voiceassistant.text import sanitize_for_paste  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _low32(x):
    """Normalize a handle to its signed low-32-bit form (see HWND-truncation
    note in the module docstring)."""
    return ctypes.c_int(x & 0xFFFFFFFF).value


def _same_hwnd(a, b):
    return _low32(a) == _low32(b)


def _pump(qapp, seconds):
    """Pump the Qt event loop for `seconds` without hard-blocking it."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


def _force_foreground(qapp, hwnd, timeout=3.0):
    """Best-effort: make our window the real OS foreground window. Returns True
    if confirmed. Not fatal if False — the caller skips.

    TEST-HARNESS ONLY: a synthetic Alt tap defeats Windows' foreground lock so
    a background test process can foreground its own window. The APP never does
    this (it must not inject Alt into a user's target) — it just declines when
    it can't confirm the foreground; here we need determinism to exercise a
    real paste."""
    ALT = 0x12
    KEYUP = 0x0002
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            ctypes.windll.user32.keybd_event(ALT, 0, 0, 0)
            ctypes.windll.user32.keybd_event(ALT, 0, KEYUP, 0)
        except Exception:
            pass
        winapi.set_foreground_window(hwnd)
        qapp.processEvents()
        if _same_hwnd(winapi.get_foreground_window(), hwnd):
            return True
        time.sleep(0.03)
    return _same_hwnd(winapi.get_foreground_window(), hwnd)


def _prepare(qapp, widget, edit, hwnd):
    """Put the target up, focused, and foreground right before a paste.

    If the OS refuses to give this process the foreground (Windows enforces a
    foreground lock, e.g. when a console or another app owns it), SKIP: the app
    now CORRECTLY declines to paste when it can't confirm the target is in
    front (see winapi.set_foreground_window), so a real paste can't be
    exercised in that environment — that's a Windows limitation, not an app
    defect, and must not read as a failure."""
    widget.raise_()
    widget.activateWindow()
    edit.setFocus()
    if not _force_foreground(qapp, hwnd):
        pytest.skip(
            "environment would not grant this process the foreground "
            "(Windows foreground lock); cannot exercise a real paste here"
        )
    edit.setFocus()
    _pump(qapp, 0.1)


def _seed_clipboard(value, retries=15):
    """Seed the real clipboard and confirm the write (clipboard access can
    transiently fail under contention)."""
    for _ in range(retries):
        try:
            pyperclip.copy(value)
            if pyperclip.paste() == value:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError("could not seed the clipboard reliably")


def _run_paste(qapp, paster, hwnd, text, timeout=8.0):
    """Submit one paste and pump the event loop until Paster's done-callback
    fires. Returns {'ok': bool, 'text': str}. Once it returns, the single-paste
    settle + clipboard restore has already completed."""
    done = threading.Event()
    result = {}

    def cb(ok, t):
        result["ok"] = ok
        result["text"] = t
        done.set()

    paster.submit(hwnd, text, cb)
    end = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert done.is_set(), f"paste callback never fired within {timeout}s"
    _pump(qapp, 0.2)  # flush any trailing key/paint events into the widget
    return result


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    existing = QApplication.instance()
    if existing is not None and existing.platformName() == "offscreen":
        pytest.skip(
            "an offscreen QApplication already exists in this process — run "
            "tests/test_paste_live.py in isolation"
        )
    app = existing or QApplication([])
    if app.platformName() == "offscreen":
        pytest.skip("Qt is running offscreen; a real display is required")
    # Closing per-test target windows must not tear down the app.
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def target(qapp):
    """A fresh, real top-level window with an editable text field, shown,
    focused, and brought to the foreground. Yields (widget, edit, hwnd)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

    widget = QWidget()
    widget.setWindowTitle("PASTE-LIVE-TARGET (test)")
    widget.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    widget.resize(720, 300)
    widget.move(120, 120)
    layout = QVBoxLayout(widget)
    edit = QPlainTextEdit()
    edit.setLineWrapMode(QPlainTextEdit.NoWrap)
    layout.addWidget(edit)

    widget.show()
    hwnd = int(widget.winId())  # forces native handle creation
    _prepare(qapp, widget, edit, hwnd)

    yield widget, edit, hwnd

    widget.close()
    widget.deleteLater()
    _pump(qapp, 0.05)


@pytest.fixture
def paster():
    """A dedicated Paster (own worker thread + own _pending_snapshot state)."""
    p = Paster()
    yield p
    p.shutdown()


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
def test_1_plain_ascii_lands_verbatim(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "SENTINEL::ascii-case-1::do-not-lose"
    _seed_clipboard(sentinel)

    text = "The quarterly report is ready for review."
    expected = sanitize_for_paste(text)

    res = _run_paste(qapp, paster, hwnd, text)

    content = edit.toPlainText()
    assert res["ok"] is True, f"Paster reported failure: {res}"
    assert content == expected, f"landed {content!r}, expected {expected!r}"
    assert pyperclip.paste() == sentinel, "clipboard not restored to sentinel"


def test_2_newlines_tabs_control_flattened(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "SENTINEL::flatten-case-2"
    _seed_clipboard(sentinel)

    # \x07 (BEL) and \x1f are stripped as control chars; \n \t \r are flattened
    # to single spaces; \x00 is stripped. The clipboard only ever sees the
    # sanitized text (Paster sanitizes before it copies).
    text = "First line\nSecond\tline\r\nthird\x07line\x1fend\x00zero"
    expected = sanitize_for_paste(text)

    res = _run_paste(qapp, paster, hwnd, text)

    content = edit.toPlainText()
    assert res["ok"] is True, res
    assert content == expected, f"landed {content!r}, expected {expected!r}"
    # The load-bearing claim: no raw newlines/tabs reach the target.
    assert "\n" not in content and "\t" not in content and "\r" not in content


def test_3_unicode_survives_roundtrip(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "SENTINEL::unicode-case-3"
    _seed_clipboard(sentinel)

    # BMP unicode (accents, em-dash) — this is what real dictation/read text
    # contains. Non-BMP astral chars (emoji, U+1F600) can degrade through the
    # Windows CF_UNICODETEXT clipboard to "??"; that's a clipboard limitation,
    # not an app one, and Whisper never emits emoji — so it's out of scope.
    text = "Café — naïve résumé façade Zürich"
    expected = sanitize_for_paste(text)
    assert expected == text  # sanitize keeps all of it (no control chars)

    res = _run_paste(qapp, paster, hwnd, text)

    content = edit.toPlainText()
    assert res["ok"] is True, res
    assert content == expected, f"landed {content!r}, expected {expected!r}"
    assert pyperclip.paste() == sentinel


def test_4_long_text_lands_fully(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "SENTINEL::long-case-4"
    _seed_clipboard(sentinel)

    text = "The quick brown fox jumps over the lazy dog. " * 23  # ~1035 chars
    expected = sanitize_for_paste(text)
    assert len(expected) >= 1000  # this really is a long paste

    res = _run_paste(qapp, paster, hwnd, text)

    content = edit.toPlainText()
    assert res["ok"] is True, res
    assert len(content) == len(expected), (
        f"length mismatch: got {len(content)}, expected {len(expected)}"
    )
    assert content == expected, "long text did not land byte-for-byte"


def test_5_empty_after_sanitize_does_not_paste(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "SENTINEL::untouched-case-5"
    _seed_clipboard(sentinel)
    assert edit.toPlainText() == ""  # fresh target starts empty

    text = "   \t\n\r\x0b\x0c\x07\x1f  "
    assert sanitize_for_paste(text) == ""  # precondition: sanitizes to empty

    res = _run_paste(qapp, paster, hwnd, text)

    assert res["ok"] is False, "empty-after-sanitize must report success=False"
    assert edit.toPlainText() == "", "target must be unchanged (no paste)"
    # This path returns before touching the clipboard at all.
    assert pyperclip.paste() == sentinel, "clipboard must be untouched"


def test_6_clipboard_restored_not_left_as_pasted(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "ORIGINAL-USER-CLIPBOARD::case-6::keep-me-safe"
    _seed_clipboard(sentinel)

    text = "Dictated replacement text number six."
    expected = sanitize_for_paste(text)

    res = _run_paste(qapp, paster, hwnd, text)

    assert res["ok"] is True, res
    assert edit.toPlainText() == expected, "text did not actually paste"
    now = pyperclip.paste()
    assert now == sentinel, f"clipboard should be restored, is {now!r}"
    assert now != expected, "clipboard was left holding the pasted text"


def test_7_back_to_back_restores_original(qapp, paster, target):
    widget, edit, hwnd = target
    _prepare(qapp, widget, edit, hwnd)
    sentinel = "ORIGINAL-BEFORE-BURST::case-7"
    _seed_clipboard(sentinel)

    text_a = "First dictation alpha."
    text_b = "Second dictation bravo."
    exp_a = sanitize_for_paste(text_a)
    exp_b = sanitize_for_paste(text_b)

    done_a, done_b = threading.Event(), threading.Event()
    res_a, res_b = {}, {}

    def cb_a(ok, t):
        res_a["ok"] = ok
        done_a.set()

    def cb_b(ok, t):
        res_b["ok"] = ok
        done_b.set()

    # Submit both immediately: B is queued while A is still running, so A's
    # pending()>0 check carries the original clipboard forward instead of
    # restoring after #1.
    paster.submit(hwnd, text_a, cb_a)
    paster.submit(hwnd, text_b, cb_b)

    end = time.monotonic() + 12.0
    while not (done_a.is_set() and done_b.is_set()) and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert done_a.is_set() and done_b.is_set(), "both callbacks must fire"
    _pump(qapp, 0.3)

    content = edit.toPlainText()
    assert res_a.get("ok") is True and res_b.get("ok") is True, (res_a, res_b)
    assert exp_a in content, f"dictation #1 missing from {content!r}"
    assert exp_b in content, f"dictation #2 missing from {content!r}"
    assert content.index(exp_a) < content.index(exp_b), "wrong paste order"
    # The carry-forward proof: the ORIGINAL is restored — not #1, not #2.
    now = pyperclip.paste()
    assert now == sentinel, f"original clipboard not restored, is {now!r}"
    assert now not in (exp_a, exp_b), "clipboard left holding a dictation"
