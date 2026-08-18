"""SelectionReader — grab the current text selection from the focused window.

The read-aloud INPUT path, mirroring paste.Paster: one owned SerialWorker, all
the blocking work off the GUI thread. It reports WHICH mechanism produced the
text so the UI can say something true and `--report` can measure it.

TIER 1 — UI Automation (`uia.get_selection`). Reads the highlighted text
directly: no clipboard, no synthetic keystrokes, no focus switch. Works even
when the source window is not focused, which is exactly the case that used to
abort. Preferred whenever it returns anything.

TIER 2 — the clipboard sentinel (write sentinel -> Ctrl+C -> poll -> restore).
The historical path, kept because UIA coverage is not universal (a survey of 30
open windows found 20 exposing UIA text; Acrobat and some Electron apps did
not). Two hard rules: never send Ctrl+C into a CONSOLE window (there it means
interrupt, and would kill a running command), and never send it at all if the
refocus did not verifiably take (it would copy the wrong window's selection).

TIER 3 — OCR of the screen, for text that is not text (scanned PDFs, images,
DRM'd content). Not done here: `window.py` owns the capture + OCR engines, so
this module simply reports that it found nothing and lets the caller escalate.
"""

import time

import keyboard as kb
import pyperclip

from . import applog, uia, winapi
from .workers import SerialWorker

_SENTINEL = "\x00__VA_CLIP_SENTINEL__\x00"

# Why the capture produced no text — drives both the user-facing message and
# whether the caller should escalate to OCR.
SRC_UIA = "uia"
SRC_CLIPBOARD = "clipboard"
SRC_EMPTY = "empty"                    # nothing selected anywhere
SRC_CONSOLE_BLOCKED = "console_blocked"  # refused to send Ctrl+C into a terminal
SRC_REFOCUS_FAILED = "refocus_failed"


class SelectionReader:
    def __init__(self):
        self._worker = SerialWorker("read-selection")

    def capture(self, hotkey_combo, target_hwnd, done_cb):
        """Queue a selection capture. done_cb(text: str, source: str) is invoked
        from the worker thread — pass a Qt signal's emit so the UI update
        marshals to the GUI thread."""
        self._worker.submit(self._job, hotkey_combo, target_hwnd, done_cb)

    def shutdown(self):
        self._worker.shutdown()

    # ------------------------------------------------------------------ #
    def _job(self, hotkey_combo, target_hwnd, done_cb):
        try:
            text, source = self._capture(hotkey_combo, target_hwnd)
        except Exception:
            applog.exception("read-selection capture failed")
            text, source = "", SRC_EMPTY
        finally:
            # Always fire the callback (even on error) so the caller's
            # in-flight flag can never wedge.
            done_cb(text if isinstance(text, str) else "", source)

    def _capture(self, hotkey_combo, target_hwnd):
        combo = (hotkey_combo or "").lower()

        # --- TIER 1: UI Automation. No clipboard, no keystrokes, no focus. --
        # Deliberately BEFORE the modifier-release wait: nothing is injected,
        # so held keys cannot corrupt it.
        try:
            text = uia.get_selection(target_hwnd)
        except Exception:
            applog.exception("UIA selection read failed")
            text = ""
        if text.strip():
            return self._tidy(text), SRC_UIA

        # --- TIER 2: the clipboard sentinel -------------------------------- #
        # Wait for ALL keys in the hotkey combo to be released (up to ~1s), so
        # our Ctrl+C isn't corrupted into Ctrl+Shift+C etc.
        for _ in range(200):
            if not any(kb.is_pressed(k) for k in combo.split("+") if k):
                break
            time.sleep(0.005)

        # Escape ONLY when the hotkey involves the Windows key (to dismiss the
        # Start menu it opened). Injecting Esc into an ordinary target window
        # deselects the text (breaking the copy) and can trigger the Windows
        # "ding" — never send it otherwise.
        if "windows" in combo:
            time.sleep(0.05)
            winapi.send_escape()
            time.sleep(0.05)

        # A console reads Ctrl+C as INTERRUPT. Sending it would kill whatever
        # command is running — never worth a read-aloud. Escalate to OCR instead.
        if target_hwnd and winapi.is_console_window(target_hwnd):
            applog.dbg("read-aloud: target is a console; refusing to send Ctrl+C")
            return "", SRC_CONSOLE_BLOCKED

        # Refocus the source window; HONOR the verified return. If the switch
        # didn't take (Windows foreground lock), do NOT send Ctrl+C — it would
        # copy from whatever window is really in front and read the WRONG
        # selection aloud. Bail before touching the clipboard.
        if target_hwnd:
            if not winapi.set_foreground_window(target_hwnd):
                applog.dbg("read-aloud: refocus of source window failed; aborting capture")
                return "", SRC_REFOCUS_FAILED
            time.sleep(0.1)

        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            old_clipboard = ""
        try:
            pyperclip.copy(_SENTINEL)
        except Exception:
            pass

        time.sleep(0.05)
        winapi.send_ctrl_c()

        text = ""
        for _ in range(150):  # up to ~1.5s — Gmail/Chrome can be slow
            time.sleep(0.01)
            try:
                current = pyperclip.paste()
                if current != _SENTINEL:
                    text = current
                    break
            except Exception:
                pass

        # Always restore the user's clipboard — read speaks `text`; it never
        # needs the clipboard. This clears the sentinel even on an empty prior
        # clipboard and doesn't leave the grabbed selection behind (mirrors
        # the paste subsystem's restore).
        try:
            pyperclip.copy(old_clipboard)
        except Exception:
            pass

        if text.strip():
            return self._tidy(text), SRC_CLIPBOARD
        return "", SRC_EMPTY

    @staticmethod
    def _tidy(text):
        """Collapse the runs of whitespace that copied/UIA text is full of.

        UIA in particular returns layout whitespace from tables and PDFs; read
        aloud, that becomes long dead pauses.
        """
        return " ".join(text.split())
