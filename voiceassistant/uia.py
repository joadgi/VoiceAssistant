"""UIA selection reader — read highlighted text WITHOUT touching the clipboard.

WHY THIS EXISTS (2026-08-18): read-aloud had exactly ONE way to obtain text —
write a sentinel to the clipboard, send Ctrl+C, poll for a change. That fails on
copy-blocked pages, on apps that remap Ctrl+C, whenever Windows refuses the
focus switch, and it is actively dangerous with a console focused (Ctrl+C there
interrupts whatever is running).

Windows UI Automation exposes the selection directly. Measured on this machine:
a full sentence read out of a real Notepad window **while focus was elsewhere**,
with no clipboard involvement at all. With nothing highlighted it returns an
empty string rather than a false positive.

Coverage is not universal (a survey of 30 open windows found 20 exposing UIA
text; Acrobat and some Electron apps did not), so this is TIER 1 of a cascade —
`selection.py` falls back to Ctrl+C and then OCR.

THREADING: COM is apartment-threaded. The client object must be created on the
thread that uses it, so it is cached per-thread and CoInitialize is called
there. This runs on SelectionReader's SerialWorker, never the GUI thread.

SEARCH ORDER matters. Do NOT "walk down from the window for the first
TextPattern" — in Chrome that finds the address bar (measured: 31 characters).
The selection lives on the FOCUSED element, so that is tried first; the window's
descendants are only a fallback (Notepad/Word keep TextPattern on a child
control, so the top-level window alone finds nothing).
"""

import ctypes
import threading
import time

from . import applog

# Hard ceiling on tree traversal. UIA calls cross a process boundary and can
# block on a busy app; read-aloud must never hang waiting for one.
_BUDGET_S = 1.2
_MAX_DEPTH = 8
_MAX_CANDIDATES = 8

_tls = threading.local()
_import_failed = False


def _uia_module():
    """Generate/return the UIAutomationClient comtypes module (process-wide)."""
    global _import_failed
    if _import_failed:
        return None
    try:
        from comtypes.client import GetModule

        GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        return UIA
    except Exception as e:
        _import_failed = True
        applog.error(f"UIA unavailable ({e.__class__.__name__}: {e}); "
                     "read-aloud will use the clipboard path")
        return None


def _client():
    """Per-thread (IUIAutomation, UIA module) pair, or (None, None)."""
    cached = getattr(_tls, "pair", None)
    if cached is not None:
        return cached
    UIA = _uia_module()
    if UIA is None:
        _tls.pair = (None, None)
        return _tls.pair
    try:
        import comtypes
        from comtypes.client import CreateObject

        try:
            comtypes.CoInitialize()
        except Exception:
            pass  # already initialised on this thread
        iuia = CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        _tls.pair = (iuia, UIA)
    except Exception as e:
        applog.error(f"UIA client creation failed ({e.__class__.__name__}); "
                     "falling back to the clipboard path")
        _tls.pair = (None, None)
    return _tls.pair


def is_available():
    return _client()[0] is not None


def _text_pattern(iuia, UIA, el):
    try:
        raw = el.GetCurrentPattern(UIA.UIA_TextPatternId)
        if raw:
            return raw.QueryInterface(UIA.IUIAutomationTextPattern)
    except Exception:
        pass
    return None


def _selection_text(iuia, UIA, el):
    """Selected text on `el`, or "" — never raises."""
    tp = _text_pattern(iuia, UIA, el)
    if tp is None:
        return ""
    try:
        sel = tp.GetSelection()
        if not sel or not sel.Length:
            return ""
        parts = []
        for i in range(min(sel.Length, 8)):  # multi-range (column) selections
            try:
                parts.append(sel.GetElement(i).GetText(-1) or "")
            except Exception:
                continue
        return "".join(parts)
    except Exception:
        return ""


def _descendants_with_text(iuia, UIA, el, deadline, depth=0, out=None):
    if out is None:
        out = []
    if depth >= _MAX_DEPTH or len(out) >= _MAX_CANDIDATES or time.monotonic() > deadline:
        return out
    try:
        walker = iuia.RawViewWalker
        child = walker.GetFirstChildElement(el)
    except Exception:
        return out
    while child is not None and len(out) < _MAX_CANDIDATES:
        if time.monotonic() > deadline:
            break
        if _text_pattern(iuia, UIA, child) is not None:
            out.append(child)
        else:
            _descendants_with_text(iuia, UIA, child, deadline, depth + 1, out)
        try:
            child = walker.GetNextSiblingElement(child)
        except Exception:
            break
    return out


def get_selection(target_hwnd=None):
    """The user's highlighted text, or "" if UIA cannot see one.

    Never raises and never blocks longer than ~_BUDGET_S.
    """
    iuia, UIA = _client()
    if iuia is None:
        return ""
    deadline = time.monotonic() + _BUDGET_S

    # 1. The focused element — where a selection actually lives.
    try:
        focused = iuia.GetFocusedElement()
    except Exception:
        focused = None

    if focused is not None:
        text = _selection_text(iuia, UIA, focused)
        if text.strip():
            applog.dbg("read-aloud: selection from UIA focused element")
            return text
        # 2. Ancestors — a focused child inside a larger document view.
        try:
            walker = iuia.ControlViewWalker
            parent, hops = walker.GetParentElement(focused), 0
            while parent is not None and hops < 5 and time.monotonic() < deadline:
                text = _selection_text(iuia, UIA, parent)
                if text.strip():
                    applog.dbg(f"read-aloud: selection from UIA ancestor+{hops + 1}")
                    return text
                parent = walker.GetParentElement(parent)
                hops += 1
        except Exception:
            pass

    # 3. The target window and its descendants. Needed because Notepad/Word
    #    keep TextPattern on a child control, and because this path works even
    #    when the window does NOT have focus.
    if target_hwnd and time.monotonic() < deadline:
        try:
            root = iuia.ElementFromHandle(ctypes.c_void_p(int(target_hwnd)))
        except Exception:
            root = None
        if root is not None:
            text = _selection_text(iuia, UIA, root)
            if text.strip():
                applog.dbg("read-aloud: selection from UIA window element")
                return text
            for el in _descendants_with_text(iuia, UIA, root, deadline):
                if time.monotonic() > deadline:
                    break
                text = _selection_text(iuia, UIA, el)
                if text.strip():
                    applog.dbg("read-aloud: selection from UIA window descendant")
                    return text
    return ""
