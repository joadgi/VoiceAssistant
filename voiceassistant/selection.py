"""SelectionReader — grab the current text selection from the focused window.

The read-aloud INPUT path, mirroring paste.Paster: one owned SerialWorker, all
the blocking clipboard/focus work off the GUI thread. Uses the Windows
clipboard-sentinel trick (write a sentinel → send Ctrl+C → poll until the
clipboard changes) and always restores the user's prior clipboard afterward.
"""

import time

import keyboard as kb
import pyperclip

from . import applog, winapi
from .workers import SerialWorker

_SENTINEL = "\x00__VA_CLIP_SENTINEL__\x00"


class SelectionReader:
    def __init__(self):
        self._worker = SerialWorker("read-selection")

    def capture(self, hotkey_combo, target_hwnd, done_cb):
        """Queue a selection capture. done_cb(text: str) is invoked from the
        worker thread with the captured selection (or "" if nothing/aborted) —
        pass a Qt signal's emit so the UI update marshals to the GUI thread."""
        self._worker.submit(self._job, hotkey_combo, target_hwnd, done_cb)

    def shutdown(self):
        self._worker.shutdown()

    # ------------------------------------------------------------------ #
    def _job(self, hotkey_combo, target_hwnd, done_cb):
        try:
            text = self._capture(hotkey_combo, target_hwnd)
        except Exception:
            applog.exception("read-selection capture failed")
            text = ""
        finally:
            # Always fire the callback (even on error) so the caller's
            # in-flight flag can never wedge.
            done_cb(text if isinstance(text, str) else "")

    def _capture(self, hotkey_combo, target_hwnd):
        combo = (hotkey_combo or "").lower()
        # Wait for ALL keys in the hotkey combo to be released (up to ~1s).
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

        # Refocus the source window; HONOR the verified return. If the switch
        # didn't take (Windows foreground lock), do NOT send Ctrl+C — it would
        # copy from whatever window is really in front and read the WRONG
        # selection aloud. Bail before touching the clipboard.
        if target_hwnd:
            if not winapi.set_foreground_window(target_hwnd):
                applog.dbg("read-aloud: refocus of source window failed; aborting capture")
                return ""
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
        return text if text.strip() else ""
