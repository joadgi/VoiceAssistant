"""Live test: UI Automation really reads a selection, from a worker thread.

The headless suite mocks `uia.get_selection`, so this is the only thing proving
the real COM plumbing works. Two risks it covers that unit tests cannot:

  * COM is APARTMENT-THREADED. read-aloud calls get_selection on
    SelectionReader's SerialWorker, so a client created on the main thread is
    unusable there. The client is cached per-thread for exactly this reason.
  * The SEARCH ORDER. "Walk down for the first TextPattern" finds Chrome's
    address bar (measured: 31 characters). The selection lives on the focused
    element, with the window's descendants only as a fallback.

Opt in (launches and closes a Notepad window):

    set RUN_E2E=1 && venv\\Scripts\\python.exe -m pytest \\
        tests/integration/test_uia_selection_live.py -q -s
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="launches a real Notepad window and talks to COM; set RUN_E2E=1",
)

KNOWN = "Kilo lima mike november oscar papa quebec romeo sierra tango."
user32 = ctypes.windll.user32


def _find_hwnd(fragment):
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if fragment in buf.value.lower():
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


@pytest.fixture
def notepad_with_selection(tmp_path):
    """A real Notepad window whose whole line is selected (via UIA, so this
    needs no keyboard focus)."""
    from comtypes.client import CreateObject, GetModule

    GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA

    path = tmp_path / "va_uia_live.txt"
    path.write_text(KNOWN, encoding="utf-8")
    proc = subprocess.Popen(["notepad.exe", str(path)])
    time.sleep(3.0)
    hwnd = _find_hwnd("va_uia_live")
    if not hwnd:
        proc.terminate()
        pytest.skip("could not find the Notepad window")

    iuia = CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

    def deep_tp(el, depth=0):
        try:
            raw = el.GetCurrentPattern(UIA.UIA_TextPatternId)
            if raw:
                return raw.QueryInterface(UIA.IUIAutomationTextPattern)
        except Exception:
            pass
        if depth > 6:
            return None
        walker = iuia.RawViewWalker
        child = walker.GetFirstChildElement(el)
        while child:
            got = deep_tp(child, depth + 1)
            if got:
                return got
            child = walker.GetNextSiblingElement(child)
        return None

    tp = deep_tp(iuia.ElementFromHandle(ctypes.c_void_p(hwnd)))
    if tp is None:
        proc.terminate()
        pytest.skip("Notepad exposed no TextPattern on this build")
    rng = tp.DocumentRange.Clone()
    rng.ExpandToEnclosingUnit(UIA.TextUnit_Line)
    rng.Select()
    time.sleep(0.4)
    try:
        yield hwnd
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        h = _find_hwnd("va_uia_live")
        if h:
            user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE


def test_uia_reads_the_selection_without_focus_or_clipboard(notepad_with_selection):
    """No Ctrl+C, no clipboard, and no foreground switch — the exact conditions
    under which the old clipboard-only path gave up."""
    import pyperclip

    from voiceassistant import uia

    assert uia.is_available(), "UIA unavailable on this machine"
    before = pyperclip.paste()

    t0 = time.perf_counter()
    text = uia.get_selection(notepad_with_selection)
    ms = (time.perf_counter() - t0) * 1000

    print(f"\n  read {len(text)} chars in {ms:.0f} ms: {text.strip()[:60]!r}")
    assert "kilo" in text.lower(), f"selection not read: {text!r}"
    assert pyperclip.paste() == before, "clipboard was touched"
    assert ms < 2000, f"too slow to feel instant: {ms:.0f} ms"


def test_uia_works_on_the_serial_worker_thread(notepad_with_selection):
    """COM apartment check: read-aloud runs this on a SerialWorker, so a
    main-thread-only client would fail here."""
    from voiceassistant import uia
    from voiceassistant.workers import SerialWorker

    out, done = {}, threading.Event()

    def job():
        try:
            t0 = time.perf_counter()
            out["text"] = uia.get_selection(notepad_with_selection)
            out["ms"] = (time.perf_counter() - t0) * 1000
        finally:
            done.set()

    w = SerialWorker("uia-live-test")
    try:
        w.submit(job)
        assert done.wait(20), "worker never completed"
    finally:
        w.shutdown()

    print(f"  worker thread: {out.get('ms', -1):.0f} ms -> "
          f"{(out.get('text') or '').strip()[:60]!r}")
    assert "kilo" in (out.get("text") or "").lower(), (
        f"UIA failed on the worker thread (COM apartment): {out!r}"
    )


def test_no_selection_returns_empty_not_the_whole_document(tmp_path):
    """A false positive would read an entire document aloud when the user has
    nothing highlighted."""
    from voiceassistant import uia

    path = tmp_path / "va_uia_nosel.txt"
    path.write_text("Some text nobody selected.", encoding="utf-8")
    proc = subprocess.Popen(["notepad.exe", str(path)])
    try:
        time.sleep(3.0)
        hwnd = _find_hwnd("va_uia_nosel")
        if not hwnd:
            pytest.skip("could not find the Notepad window")
        text = uia.get_selection(hwnd)
        assert "nobody selected" not in text.lower(), (
            f"returned document text with nothing selected: {text!r}"
        )
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        h = _find_hwnd("va_uia_nosel")
        if h:
            user32.PostMessageW(h, 0x0010, 0, 0)
