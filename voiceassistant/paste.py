"""Paster — the paste subsystem. One SerialWorker, zero GUI-thread blocking.

Every dictation paste runs as a job on this worker: the modifier-release
wait, refocus, Ctrl+V, settle delay, and the deferred clipboard restore all
happen OFF the GUI thread (the old inline version froze the whole UI for
0.4–2.4s per paste). Jobs are strictly serialized, so the clipboard
snapshot/restore logic is thread-confined and race-free by construction.
"""

import time

import keyboard as kb
import pyperclip

from . import applog, winapi
from .text import sanitize_for_paste
from .workers import SerialWorker


class Paster:
    def __init__(self):
        self._worker = SerialWorker("paste")
        # Original user clipboard awaiting restore. Worker-confined: only
        # paste jobs (serialized on the one worker) read/write it.
        self._pending_snapshot = None

    def submit(self, hwnd, text, done_cb):
        """Queue a paste. done_cb(success: bool, text: str) is called from the
        worker thread — pass a Qt signal's emit so the UI update marshals back
        to the GUI thread."""
        self._worker.submit(self._job, hwnd, text, done_cb)

    def shutdown(self):
        self._worker.shutdown()

    # ------------------------------------------------------------------ #
    def _job(self, hwnd, text, done_cb):
        ok = self._paste(hwnd, text)
        try:
            done_cb(ok, text)
        except Exception:
            applog.exception("paste done_cb failed")

    def _paste(self, hwnd, text):
        applog.dbg(f"paste ENTER target_hwnd={hwnd} fg={winapi.get_foreground_window()}")
        text = sanitize_for_paste(text)
        if not text:
            applog.dbg("  nothing left after sanitize, skipping paste")
            return False

        # Snapshot the user's clipboard — or, when a previous paste's restore
        # is still pending (back-to-back dictations), carry the true original
        # forward instead of snapshotting our own leftovers.
        if self._pending_snapshot is not None:
            old_clipboard = self._pending_snapshot
        else:
            try:
                old_clipboard = pyperclip.paste()
            except Exception:
                old_clipboard = ""

        try:
            pyperclip.copy(text)
        except Exception as e:
            applog.error(f"clipboard write failed: {e}")
            return False
        applog.dbg(f"  clipboard set ({len(text)} chars)")

        # Wait until the user has released modifier keys (their hotkey), so
        # our Ctrl+V isn't corrupted into Ctrl+Shift+V etc.
        for i in range(100):
            if not (kb.is_pressed("ctrl") or kb.is_pressed("shift")
                    or kb.is_pressed("alt") or kb.is_pressed("windows")):
                applog.dbg(f"  mods released after {i * 20}ms")
                break
            time.sleep(0.02)

        # If focus drifted off the target, refocus it. We deliberately do NOT
        # inject Escape — sending Esc into the target app is what produced the
        # audible Windows beep on many controls.
        current_fg = winapi.get_foreground_window()
        if current_fg != hwnd:
            applog.dbg(f"  focus on {current_fg}, refocusing target (no Esc)")
            if not winapi.set_foreground_window(hwnd):
                applog.dbg("  refocus FAILED — leaving text on clipboard for manual paste")
                # Deliberate: the dictation stays on the clipboard as the
                # manual-paste fallback, and any stale pending snapshot is
                # dropped (restoring it later would clobber that fallback).
                self._pending_snapshot = None
                return False
            time.sleep(0.12)
        else:
            time.sleep(0.05)

        winapi.send_ctrl_v()
        applog.dbg("paste DONE")

        # Deferred clipboard restore. If another paste is already queued,
        # skip — the next job carries the original forward and restores when
        # the burst ends. Otherwise wait for the target to consume the paste,
        # then restore only if the clipboard still holds OUR text (never
        # clobber the read-aloud sentinel, the Copy button, or the user).
        if not isinstance(old_clipboard, str) or not old_clipboard:
            self._pending_snapshot = None
            time.sleep(0.3)
            return True

        if self._worker.pending() > 0:
            self._pending_snapshot = old_clipboard
            time.sleep(0.3)
            return True

        self._pending_snapshot = None
        time.sleep(0.6)
        try:
            if pyperclip.paste() == text:
                pyperclip.copy(old_clipboard)
                applog.dbg("  clipboard restored")
        except Exception:
            pass
        return True
