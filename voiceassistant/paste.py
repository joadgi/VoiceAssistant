"""Paster — the paste subsystem. One SerialWorker, zero GUI-thread blocking.

Every dictation paste runs as a job on this worker: the modifier-release
wait, refocus, Ctrl+V, settle delay, and the deferred clipboard restore all
happen OFF the GUI thread (the old inline version froze the whole UI for
0.4–2.4s per paste). Jobs are strictly serialized, so the clipboard
snapshot/restore logic is thread-confined and race-free by construction.
"""

import time

import pyperclip

from . import applog, winapi
from .inline_typist import (
    MAX_STREAM_BACKSPACES, plan_edit, plan_final_edit,
)
from .text import sanitize_for_paste
from .workers import SerialWorker

# Inline-typing outcomes reported back to the window.
INLINE_TYPED = "typed"        # the window holds the final text; nothing to paste
INLINE_PARTIAL = "partial"    # text was typed but could not be reconciled
INLINE_NONE = "none"          # nothing was typed; use the normal paste path


class _TypedState:
    """What inline typing believes it has put in the target window.

    WORKER-CONFINED, exactly like `_pending_snapshot`: it is read and written
    only by jobs on the one paste worker, so it is always consistent with the
    injection it describes. Computing it on the GUI thread would race with
    in-flight typing and produce backspace counts for text that had already
    changed — the failure mode that deletes the user's own words.
    """

    def __init__(self, hwnd, session):
        self.hwnd = hwnd
        self.session = session
        self.text = ""        # characters this app has typed into `hwnd`
        self.certain = True   # False once a batch was accepted only in part
        # True once INJECTION itself failed for this dictation, which is
        # permanent. A plan merely refused for being too large is NOT broken:
        # nothing was sent, so the record is still exact and the next draft
        # gets a fresh attempt. Conflating the two meant one oversized
        # revision — which a preview commit reliably produces on a long hold —
        # silently ended live typing for the rest of the dictation.
        self.broken = False


class Paster:
    def __init__(self):
        self._worker = SerialWorker("paste")
        # Original user clipboard awaiting restore. Worker-confined: only
        # paste jobs (serialized on the one worker) read/write it.
        self._pending_snapshot = None
        # Inline typing state for the dictation in progress (worker-confined).
        self._typed = None

    def submit(self, hwnd, text, done_cb):
        """Queue a paste. done_cb(success: bool, text: str) is called from the
        worker thread — pass a Qt signal's emit so the UI update marshals back
        to the GUI thread."""
        self._worker.submit(self._job, hwnd, text, done_cb)

    def shutdown(self):
        self._worker.shutdown()

    # ------------------------------------------------------------------ #
    # Inline typing (see inline_typist.py for the contract)
    # ------------------------------------------------------------------ #
    def begin_inline(self, hwnd, session):
        """Start a fresh inline-typing session for `hwnd`.

        `session` is a monotonic id from the window. Every later call carries
        it, so a job queued by one dictation can never act on the state of the
        next — back-to-back dictations usually share an HWND, which makes the
        window handle alone useless as an identity.
        """
        self._worker.submit(self._begin_inline_job, hwnd, session)

    def type_to(self, hwnd, session, desired):
        """Make the window hold `desired`, typing or correcting the difference."""
        self._worker.submit(self._type_to_job, hwnd, session, desired)

    def finalize_inline(self, hwnd, session, final_text, done_cb):
        """Reconcile what was typed with the final transcription.

        done_cb(outcome, hwnd, text) runs on the worker thread — pass a
        signal's emit. Outcome is INLINE_TYPED / INLINE_PARTIAL / INLINE_NONE;
        the window falls back to a normal paste on INLINE_NONE, which is why
        the HWND travels back with the answer rather than being remembered on
        the GUI side, where an overlapping dictation could overwrite it.
        """
        self._worker.submit(self._finalize_inline_job, hwnd, session,
                            final_text, done_cb)

    def cancel_inline(self, session=None, erase=False):
        """End the session. With `erase`, remove the draft this app typed.

        Erasing is what keeps a dropped or suppressed clip from leaving an
        orphaned draft in the user's document: the app typed those characters,
        so the app takes them back. It only ever runs against a certain record.
        """
        self._worker.submit(self._cancel_inline_job, session, erase)

    def _begin_inline_job(self, hwnd, session):
        # A previous session that still has characters in a window never got
        # finalized: the decode raised, the mic died, or the user started a
        # new hold before the last result arrived. Nothing will ever correct
        # those characters now, and the old state is about to be overwritten,
        # so this is the last moment we still know what they were. Take them
        # back before starting fresh.
        #
        # Without this, chaining two dictations into the same box produced
        # "Hello therHello there." — the first draft orphaned, then its final
        # text pasted underneath it.
        previous = self._typed
        if previous is not None and previous.text:
            applog.info("inline typing: reclaiming a draft from an unfinished dictation")
            self._cancel_inline_job(previous.session, erase=True)
        self._typed = _TypedState(hwnd, session)

    def _cancel_inline_job(self, session=None, erase=False):
        state = self._typed
        if state is None:
            return
        if session is not None and state.session != session:
            return
        if erase and not state.certain:
            # Something may be in the window that never reached the record, so
            # there is no count we are allowed to delete. Say so: an orphaned
            # draft with no explanation is worse than one with a log line.
            applog.info("inline draft left in place: injection count was unknown")
        elif erase and state.text:
            # `broken` is deliberately NOT a reason to skip: it means typing
            # STOPPED, not that the record is wrong. The record is still exact
            # (that is what `certain` tracks), so those characters are still
            # ours to take back. Skipping them here left a half-sentence in the
            # document with no log line and no message.
            plan = plan_final_edit(state.text, "")
            if self._refocus_target(state):
                self._apply_plan(state, plan)
        self._typed = None

    def _type_to_job(self, hwnd, session, desired):
        state = self._typed
        # A job queued for a dictation that has since ended or moved on must
        # never touch the window.
        if state is None or state.hwnd != hwnd or state.session != session:
            applog.dbg("inline type skipped: stale job (session=%s)" % session)
            return
        if state.broken or not state.certain:
            applog.dbg("inline type skipped: broken=%s certain=%s"
                       % (state.broken, state.certain))
            return
        plan = plan_edit(state.text, desired, MAX_STREAM_BACKSPACES)
        applog.dbg("inline type: have=%d want=%d plan=%s"
                   % (len(state.text), len(desired),
                      "refused" if plan is None else "back=%d type=%d" % (plan[0], len(plan[1]))))
        if plan is None:
            # The draft revised more than a correction should chase mid-flight.
            # Skip THIS DRAFT only: nothing was sent, so the record is still
            # exact and the next draft is judged fresh against it. Measured
            # 2026-09-16: a preview commit re-decodes the live window from a
            # new offset, which shifts words near the seam and produced exactly
            # one oversized revision — 3 of 4 commits in the log. Latching
            # `broken` here meant that single tick stopped live typing for the
            # REST of the hold, so a long dictation went dead partway through
            # and everything after it arrived only at finalize.
            applog.dbg("inline type skipped: revision exceeded the streaming limit")
            return
        self._apply_plan(state, plan)

    def _refocus_target(self, state):
        """Bring the dictation target back to the front for the FINAL edit.

        Streaming deliberately does NOT do this: if the user looks away
        mid-sentence, stealing focus back would fight them, so typing simply
        stops. The final edit is different — it is the dictation landing, and
        the normal paste path has always refocused for exactly that reason.
        Without it, alt-tabbing away before releasing the key left a truncated
        draft in the document and the real text only on the clipboard.
        """
        if winapi.get_foreground_window() == state.hwnd:
            return True
        if not winapi.wait_for_modifiers_released(2.0):
            applog.info("inline finalize deferred: modifiers still held")
            return False
        if not winapi.set_foreground_window(state.hwnd):
            applog.info("inline finalize deferred: target window refused focus")
            return False
        time.sleep(0.12)
        return winapi.get_foreground_window() == state.hwnd

    def _apply_plan(self, state, plan):
        """Run (backspaces, to_type) against the window, keeping the record exact.

        The lock spans BOTH halves so a correction is never split by another
        clipboard/input transaction — a delete that lands without its
        replacement is a hole in the user's sentence.
        """
        backspaces, to_type = plan
        with winapi.clipboard_input_lock:
            if backspaces:
                ok, sent = winapi.send_backspaces(backspaces, expected_hwnd=state.hwnd)
                if sent is None:
                    state.certain = False
                    state.broken = True
                    return False
                if sent:
                    state.text = state.text[:len(state.text) - sent]
                if not ok:
                    state.broken = True
                    return False
            if to_type:
                ok, sent = winapi.send_text(to_type, expected_hwnd=state.hwnd)
                if sent is None:
                    state.certain = False
                    state.broken = True
                    return False
                if sent:
                    state.text += to_type[:sent]
                if not ok:
                    state.broken = True
                    return False
        return True

    def _finalize_inline_job(self, hwnd, session, final_text, done_cb):
        state = self._typed
        self._typed = None
        try:
            if state is None or state.hwnd != hwnd or state.session != session:
                # Not our session: the normal paste path is both correct and
                # better tested, so use it.
                applog.dbg(
                    "inline finalize: not our session (state=%s want hwnd=%s "
                    "session=%s)" % (
                        "none" if state is None
                        else "hwnd=%s session=%s" % (state.hwnd, state.session),
                        hwnd, session))
                outcome = INLINE_NONE
            elif not state.certain:
                # ORDER MATTERS: this is checked BEFORE "did we type anything",
                # because a partially-accepted batch can put characters in the
                # window that never reached the record. Reading the empty
                # record first would report "nothing typed" and the app would
                # paste a second copy underneath the orphaned characters.
                applog.info("inline typing could not be reconciled: injection was partial")
                outcome = INLINE_PARTIAL
            elif not state.text:
                # Nothing of ours is in the window, and we know that for sure.
                applog.dbg("inline finalize: nothing had been typed "
                           "(broken=%s) -> falling back to paste" % state.broken)
                outcome = INLINE_NONE
            elif not self._refocus_target(state):
                outcome = INLINE_PARTIAL
            else:
                plan = plan_final_edit(state.text, sanitize_for_paste(final_text))
                if self._apply_plan(state, plan):
                    outcome = INLINE_TYPED
                else:
                    outcome = INLINE_PARTIAL
            if outcome == INLINE_PARTIAL:
                # The user's window holds a draft we cannot finish correcting.
                # Put the accurate text where they can place it themselves.
                try:
                    pyperclip.copy(sanitize_for_paste(final_text))
                except Exception:
                    applog.exception("clipboard write failed after partial inline typing")
        except Exception:
            applog.exception("inline finalize failed")
            outcome = INLINE_PARTIAL
        try:
            done_cb(outcome, hwnd, final_text)
        except Exception:
            applog.exception("inline finalize done_cb failed")

    # ------------------------------------------------------------------ #
    def _job(self, hwnd, text, done_cb):
        try:
            ok = self._paste(hwnd, text)
        except Exception:
            applog.exception("paste failed; text retained in the app")
            ok = False
        try:
            done_cb(ok, text)
        except Exception:
            applog.exception("paste done_cb failed")

    def _paste(self, hwnd, text):
        with winapi.clipboard_input_lock:
            return self._paste_locked(hwnd, text)

    def _paste_locked(self, hwnd, text):
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

        if not winapi.wait_for_modifiers_released(2.0):
            applog.info("paste deferred: modifiers held; text retained for manual paste")
            self._pending_snapshot = None
            return False

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

        if winapi.get_foreground_window() != hwnd or winapi.send_ctrl_v() is False:
            applog.info("paste deferred: focus/input changed; text retained for manual paste")
            self._pending_snapshot = None
            return False
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
