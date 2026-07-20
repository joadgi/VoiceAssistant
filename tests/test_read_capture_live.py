"""REAL end-to-end test for READ-ALOUD SELECTION CAPTURE (voiceassistant/window.py).

This exercises the untested "doorway" that feeds text to TTS:
`MainWindow._capture_selection()` (and its `_read_selection_job` wrapper). It
mocks nothing about the copy mechanism in the headline case: it stands up a
real, on-screen, focusable window with an editable text field, selects text in
it, brings it to the OS foreground, then drives `_capture_selection()` exactly
as the app's read-selection worker does — a synthetic Win32 Ctrl+C into the
focused window via the real clipboard sentinel protocol — and asserts the
selected text comes back. That proves Ctrl+C physically copied the selection.

WHY IT IS GATED AND MUST RUN ALONE
----------------------------------
It steals keyboard focus and rewrites the real clipboard, and a process gets
exactly ONE QApplication + one Qt platform for its lifetime. So:

  * Opt in with the env flag  RUN_READ_LIVE=1  (off by default).
  * It needs a REAL, interactive, unlocked desktop session. It SKIPS if Qt is
    running offscreen (QT_QPA_PLATFORM=offscreen): an offscreen window has no
    real HWND that can be the OS foreground target for a Win32 Ctrl+C.
  * Run this FILE BY ITSELF. tests/test_ui_smoke.py forces the offscreen Qt
    platform at import time; if it is collected first in this process, our
    windows would be offscreen and unusable. This file deliberately does NOT
    import test_ui_smoke — it replicates that harness's monkeypatches inline
    (CONFIG_FILE/CONFIG_DIR -> tmp, Transcriber/OCR load_model -> no-op,
    winapi.set_start_with_windows -> no-op, _setup_hotkeys/_setup_tray -> no-op)
    but WITHOUT forcing offscreen, so it gets a real MainWindow with real
    `_capture_selection` / `_read_selection_job` on a real display.

RUN COMMAND (PowerShell — the project's primary shell)
------------------------------------------------------
    $env:RUN_READ_LIVE="1"; venv\\Scripts\\python.exe -m pytest tests\\test_read_capture_live.py -v -s

Do NOT pass/leave QT_QPA_PLATFORM=offscreen. Do NOT run on a locked screen or a
disconnected RDP session (SetForegroundWindow + synthetic input are blocked
there — the live case will SKIP). Expect the foreground window to change and a
small text window to flash; the real clipboard is written and (mostly) restored.

WHAT EACH CASE PROVES
---------------------
  test_capture_returns_the_selected_text (LIVE, needs foreground)
      The full real path: a real focused QPlainTextEdit with a real selection,
      a real Win32 Ctrl+C, the real clipboard sentinel poll -> the selected
      text is returned. Also confirms Escape is NOT injected for a non-windows
      hotkey (the M2 contract — Escape would deselect the edit and break copy),
      and documents that the SUCCESS path leaves the selection on the clipboard
      (it does not restore the prior clipboard — see NOTES #3).

  test_escape_only_injected_for_windows_hotkey (M2 fix — the load-bearing one)
      Directly asserts the conditional at window.py:854. With send_escape and
      send_ctrl_c stubbed and the refocus skipped, a NON-windows read hotkey
      ("ctrl+m") injects Escape 0 times; a windows hotkey ("windows+m") injects
      it exactly once. This is the whole point of the M2 fix.

  test_original_clipboard_restored_when_nothing_captured
      Seeds a known real clipboard value; a capture that copies nothing (empty
      selection, modelled deterministically by stubbing send_ctrl_c to a no-op)
      must restore the user's original clipboard (window.py:892-898). Uses the
      REAL clipboard for save/restore; the copy mechanism itself is proven live
      in the first case.

  test_read_job_emits_captured_text_on_success / _emits_empty_on_exception
      `_read_selection_job` must ALWAYS emit `_sig_read_text_ready` (try/finally)
      so `_read_in_flight` can never wedge — once with the captured text, once
      with "" when `_capture_selection` raises. Also proves the emitted text is
      wired through to TTS.

NOTES / SUSPECTED HOLES SPOTTED WHILE READING _capture_selection
----------------------------------------------------------------
(Reported to the caller; NOT fixed here.)
  #1 window.py:862 — `winapi.set_foreground_window(target)` return value is
     IGNORED. That function was recently hardened to VERIFY the switch and
     return False under the Windows foreground lock (so the PASTE path can
     decline). The READ path ignores it and sends Ctrl+C regardless, so a failed
     refocus copies from whatever window is REALLY in front — reading aloud the
     WRONG window's selection (or nothing). Asymmetric with paste.
  #2 window.py:894 — restore is guarded by `if old_clipboard:`. If the user's
     clipboard was EMPTY and nothing is captured, the SENTINEL string (which
     contains NUL bytes) is LEFT on the clipboard instead of cleared.
  #3 window.py:865-899 — the SUCCESS path saves `old_clipboard` but never
     restores it; a successful read permanently replaces the user's clipboard
     with the captured selection. Possibly intended, but asymmetric with the
     paste subsystem's careful restore and with the "Save current clipboard"
     comment's implied intent.

RELIABILITY NOTES
-----------------
  * The Qt event loop MUST be pumped on the main thread while `_capture_selection`
    runs, or the synthetic Ctrl+C is never translated into the QPlainTextEdit's
    copy shortcut and nothing is copied. The target edit lives in THIS process
    (unlike the app, whose target is a separate app with its own loop), so the
    live case runs capture on a worker thread and pumps processEvents() on the
    main thread — mirroring the app (capture on `_read_worker`, GUI pumps).
  * HWND truncation: get_foreground_window() now declares restype=HWND, but we
    still compare identity with `_same_hwnd` (signed low-32) to match the proven
    paste-test harness and stay robust.
"""

import ctypes
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RUN = os.environ.get("RUN_READ_LIVE")

pytestmark = pytest.mark.skipif(
    not _RUN,
    reason="live read-capture test steals focus + uses the real clipboard — set "
    "RUN_READ_LIVE=1 and run on a real (non-offscreen) interactive desktop",
)

# Hard skip before we ever build a QApplication if Qt is forced offscreen: an
# offscreen window cannot be the OS foreground target for a Win32 Ctrl+C.
if _RUN and os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
    pytest.skip(
        "QT_QPA_PLATFORM=offscreen cannot receive Win32 Ctrl+C — run this file "
        "in isolation on a real display",
        allow_module_level=True,
    )

# Real deps — the app already requires all three; importorskip keeps collection
# clean on a machine that somehow lacks them.
pytest.importorskip("PySide6")
pyperclip = pytest.importorskip("pyperclip")
pytest.importorskip("keyboard")

from voiceassistant import winapi  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers (mirrors tests/test_paste_live.py — the proven live-window harness)
# --------------------------------------------------------------------------- #
def _low32(x):
    """Normalize a handle to its signed low-32-bit form."""
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
    if confirmed.

    TEST-HARNESS ONLY: a synthetic Alt tap defeats Windows' foreground lock so
    a background test process can foreground its own window. The APP never does
    this."""
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
    """Put the target up, focused, and foreground. SKIP if the OS refuses the
    foreground (Windows foreground lock) — a real Ctrl+C can't be exercised
    there and that is an environment limitation, not an app defect (same policy
    as the live paste test)."""
    widget.raise_()
    widget.activateWindow()
    edit.setFocus()
    if not _force_foreground(qapp, hwnd):
        pytest.skip(
            "environment would not grant this process the foreground "
            "(Windows foreground lock); cannot exercise a real Ctrl+C capture here"
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


def _run_capture(qapp, mw, timeout=8.0):
    """Run `_capture_selection` on a worker thread (as the app does on its
    read-selection worker) while pumping the Qt event loop on the main thread,
    so the injected Ctrl+C is actually delivered to the in-process target edit.
    Returns the captured text."""
    out = {}
    done = threading.Event()

    def worker():
        try:
            out["text"] = mw._selection_reader._capture(mw.config["hotkey_read_aloud"], mw._read_target_hwnd)
        except Exception as e:  # surfaced to the test after join
            out["exc"] = e
        finally:
            done.set()

    threading.Thread(target=worker, name="read-capture-test", daemon=True).start()
    end = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert done.is_set(), f"capture did not finish within {timeout}s"
    _pump(qapp, 0.1)  # flush trailing key/clipboard events
    if "exc" in out:
        raise out["exc"]
    return out["text"]


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
            "tests/test_read_capture_live.py in isolation"
        )
    app = existing or QApplication([])
    if app.platformName() == "offscreen":
        pytest.skip("Qt is running offscreen; a real display is required")
    # Closing per-test windows / the pill must not tear down the app.
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def mw(qapp, monkeypatch):
    """A real, on-display MainWindow with heavy/side-effecting startup
    neutralized — the same monkeypatches as test_ui_smoke, but WITHOUT forcing
    offscreen, so `_capture_selection` / `_read_selection_job` are the real
    methods and the target can take focus + Ctrl+C."""
    import tempfile

    import voiceassistant.config as cfg
    import voiceassistant.ocr as ocr
    import voiceassistant.transcriber as tr
    from voiceassistant.window import MainWindow

    tmpdir = tempfile.mkdtemp()
    # Patch BOTH so every atomic config write stays inside the temp dir
    # (save() uses CONFIG_DIR for its mkstemp scratch file, CONFIG_FILE for the
    # final path — leaving CONFIG_DIR unpatched would litter the project root).
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmpdir)
    monkeypatch.setattr(cfg, "CONFIG_FILE", os.path.join(tmpdir, "settings.json"))
    monkeypatch.setattr(tr.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)

    w = MainWindow(entry_script="main.py")
    # Belt-and-braces: never let a read path actually hit the network/audio.
    # (The success read path calls tts.speak; individual tests may re-stub it.)
    monkeypatch.setattr(w.tts, "speak", lambda *a, **k: None)

    yield w

    # Tear down owned workers/timers/pill without touching closeEvent (which
    # references self.tray — never created because _setup_tray was a no-op).
    for teardown in (
        lambda: w._show_request_timer.stop(),
        lambda: w._read_worker.shutdown(),
        lambda: w.tts.shutdown(),
        lambda: w.paster.shutdown(),
        lambda: w.indicator.close(),
    ):
        try:
            teardown()
        except Exception:
            pass
    _pump(qapp, 0.05)


@pytest.fixture
def make_target(qapp):
    """Factory for a fresh, real top-level window holding a QPlainTextEdit.
    Yields make(text) -> (widget, edit, hwnd). All windows are closed on
    teardown."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

    created = []

    def _make(text):
        widget = QWidget()
        widget.setWindowTitle("READ-LIVE-TARGET (test)")
        widget.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        widget.resize(720, 240)
        widget.move(140, 140)
        layout = QVBoxLayout(widget)
        edit = QPlainTextEdit()
        edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        edit.setPlainText(text)
        layout.addWidget(edit)
        widget.show()
        hwnd = int(widget.winId())  # forces native handle creation
        created.append(widget)
        return widget, edit, hwnd

    yield _make

    for widget in created:
        widget.close()
        widget.deleteLater()
    _pump(qapp, 0.05)


# --------------------------------------------------------------------------- #
# Case 1 — the full LIVE path: a real selection is really copied and returned
# --------------------------------------------------------------------------- #
def test_capture_returns_the_selected_text(qapp, mw, make_target, monkeypatch):
    # Escape must NOT be injected for a non-windows hotkey — here that is also
    # load-bearing for the test: injecting Escape would deselect the edit and
    # the copy would return nothing.
    esc = {"n": 0}
    monkeypatch.setattr(winapi, "send_escape",
                        lambda: esc.__setitem__("n", esc["n"] + 1))

    known = "The quarterly numbers look strong this April."
    widget, edit, hwnd = make_target(known)
    _prepare(qapp, widget, edit, hwnd)  # SKIPs on foreground lock

    # Select the text and take focus right before the copy.
    edit.selectAll()
    edit.setFocus()
    _pump(qapp, 0.05)

    mw.config.set("hotkey_read_aloud", "ctrl+shift+t")  # non-windows default
    mw._read_target_hwnd = hwnd

    seed = "PRIOR-CLIP::must-survive-a-read"
    _seed_clipboard(seed)

    captured = _run_capture(qapp, mw)

    assert captured.strip() == known, f"captured {captured!r}, expected {known!r}"
    # M2 contract: no Escape for a non-windows hotkey.
    assert esc["n"] == 0, "Escape was injected for a non-windows hotkey (M2 regression)"
    # FIXED (#3): read-aloud now RESTORES the user's clipboard on success — the
    # grabbed selection is not left sitting on the clipboard (matches paste).
    assert pyperclip.paste() == seed, "prior clipboard was not restored on success"


# --------------------------------------------------------------------------- #
# Case 2 — the M2 fix: Escape only for a windows-key hotkey (window.py:854)
# --------------------------------------------------------------------------- #
def test_escape_only_injected_for_windows_hotkey(qapp, mw, monkeypatch):
    calls = {"escape": 0}
    monkeypatch.setattr(winapi, "send_escape",
                        lambda: calls.__setitem__("escape", calls["escape"] + 1))
    # Make the sentinel poll resolve immediately with a known value, so the
    # function returns fast without needing a real focused target.
    monkeypatch.setattr(winapi, "send_ctrl_c",
                        lambda: pyperclip.copy("READ-LIVE-SEL"))
    mw._read_target_hwnd = 0  # skip the real refocus; isolate the conditional

    # NON-windows hotkey -> Escape must NOT be injected.
    mw.config.set("hotkey_read_aloud", "ctrl+m")
    assert mw._selection_reader._capture(mw.config["hotkey_read_aloud"], mw._read_target_hwnd) == "READ-LIVE-SEL"
    assert calls["escape"] == 0, "Escape injected for a non-windows hotkey (M2 regression)"

    # Windows-key hotkey -> Escape IS injected (dismiss the Start menu).
    calls["escape"] = 0
    mw.config.set("hotkey_read_aloud", "windows+m")
    assert mw._selection_reader._capture(mw.config["hotkey_read_aloud"], mw._read_target_hwnd) == "READ-LIVE-SEL"
    assert calls["escape"] == 1, "Escape NOT injected for a windows-key hotkey (M2 broken)"


# --------------------------------------------------------------------------- #
# Case 3 — clipboard restore when nothing is captured (window.py:892-898)
# --------------------------------------------------------------------------- #
def test_original_clipboard_restored_when_nothing_captured(qapp, mw, monkeypatch):
    # "No selection" modelled deterministically: Ctrl+C copies nothing, so the
    # sentinel is never overwritten. This exercises the REAL clipboard
    # save/restore path with the real clipboard; the copy mechanism itself is
    # proven live in case 1.
    monkeypatch.setattr(winapi, "send_ctrl_c", lambda: None)
    mw._read_target_hwnd = 0
    mw.config.set("hotkey_read_aloud", "ctrl+shift+t")  # non-windows, no Escape

    seed = "USER-ORIGINAL-CLIPBOARD::read-restore-case"
    _seed_clipboard(seed)

    out = mw._selection_reader._capture(mw.config["hotkey_read_aloud"], mw._read_target_hwnd)  # ~1.5s sentinel-poll timeout, then restore

    assert out == "", f"expected empty capture, got {out!r}"
    assert pyperclip.paste() == seed, "user's original clipboard was not restored"


# --------------------------------------------------------------------------- #
# Case 4 — the capture result is wired through to TTS and clears in-flight.
# (SelectionReader._job always calls back; MainWindow's _on_read_text_ready
# slot must speak the text and reset _read_in_flight — even on a capture error.)
# --------------------------------------------------------------------------- #
def test_read_result_wired_to_tts_on_success(qapp, mw, monkeypatch):
    from PySide6.QtCore import Qt

    got = []
    mw._sig_read_text_ready.connect(got.append, Qt.ConnectionType.DirectConnection)
    spoken = []
    monkeypatch.setattr(mw.tts, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(mw._selection_reader, "_capture",
                        lambda combo, hwnd: "hello from selection")

    mw._read_in_flight = True
    mw._selection_reader._job(mw.config["hotkey_read_aloud"],
                              mw._read_target_hwnd, mw._sig_read_text_ready.emit)

    assert got == ["hello from selection"], got
    assert spoken == ["hello from selection"], "captured text not wired through to TTS"
    assert mw._read_in_flight is False, "read-in-flight flag left wedged"


def test_read_result_empty_on_capture_exception(qapp, mw, monkeypatch):
    from PySide6.QtCore import Qt

    got = []
    mw._sig_read_text_ready.connect(got.append, Qt.ConnectionType.DirectConnection)

    def boom(combo, hwnd):
        raise RuntimeError("capture blew up")

    monkeypatch.setattr(mw._selection_reader, "_capture", boom)

    mw._read_in_flight = True
    mw._selection_reader._job(mw.config["hotkey_read_aloud"],
                              mw._read_target_hwnd, mw._sig_read_text_ready.emit)

    assert got == [""], got
    assert mw._read_in_flight is False, "read-in-flight flag left wedged after an error"
