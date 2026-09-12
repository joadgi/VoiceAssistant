"""Inline typing (Stage 2): the arithmetic, the policy, the worker, the wiring.

This feature types characters into whatever window the user is dictating into
and deletes characters to correct itself. The blast radius of a mistake is the
user's own document, so every rule in `inline_typist.py`'s safety contract is
pinned here, and each test names the damage it prevents:

- Deletions are computed ONLY from the app's own record of what it typed.
- An UNCERTAIN record (a partially-accepted SendInput batch) permanently stops
  corrections — backspacing past our own text eats the user's work.
- A refusal that sent nothing leaves the record intact; only a partial batch
  is unknown.
- Corrections are bounded; past the limit the app stops rather than issuing a
  long delete burst.
- It never runs with a modifier-holding hotkey (characters would arrive as
  shortcuts), never into a console, never into our own window.
- A dropped, silent, or hallucinated clip takes its own draft back.
- A session id keeps one dictation's queued jobs off the next one's state.
"""

import os
import sys
import tempfile

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from voiceassistant import paste as paste_mod  # noqa: E402
from voiceassistant.inline_typist import (  # noqa: E402
    MAX_FINAL_BACKSPACES, MAX_STREAM_BACKSPACES, block_reason, common_prefix_len,
    hotkey_holds_modifier, plan_edit, polish_stream_text, stream_target,
)
from voiceassistant.paste import (  # noqa: E402
    INLINE_NONE, INLINE_PARTIAL, INLINE_TYPED, Paster,
)

SR = 16000


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# 1. plan_edit — the arithmetic that decides what gets deleted
# --------------------------------------------------------------------------- #
class TestPlanEdit:
    def test_pure_append_deletes_nothing(self):
        assert plan_edit("Hello there", "Hello there world", 48) == (0, " world")

    def test_identical_is_a_no_op(self):
        assert plan_edit("Hello", "Hello", 48) == (0, "")

    def test_correction_deletes_only_the_divergent_tail(self):
        # "point is the first" was revised to "point follows from it".
        typed = "The second point is the first"
        target = "The second point follows from it"
        backspaces, to_type = plan_edit(typed, target, 48)
        assert backspaces == len("is the first")
        assert to_type == "follows from it"
        # The invariant that matters: applying the plan reproduces the target
        # and never removes more than the app itself typed.
        assert typed[:len(typed) - backspaces] + to_type == target
        assert backspaces <= len(typed)

    def test_deletion_never_exceeds_what_was_typed(self):
        typed = "abc"
        backspaces, _ = plan_edit(typed, "xyz totally different", 48)
        assert backspaces == len(typed), "a plan tried to delete past its own text"

    def test_shrinking_target_deletes_the_remainder(self):
        assert plan_edit("Hello world", "Hello", 48) == (6, "")

    def test_erase_everything(self):
        assert plan_edit("Hello", "", 48) == (5, "")

    def test_over_budget_is_refused(self):
        typed = "x" * 200
        assert plan_edit(typed, "y" * 200, MAX_STREAM_BACKSPACES) is None
        assert plan_edit(typed, "y" * 200, MAX_FINAL_BACKSPACES) is not None

    def test_from_empty(self):
        assert plan_edit("", "Hello", 48) == (0, "Hello")

    def test_common_prefix(self):
        assert common_prefix_len("abcd", "abxd") == 2
        assert common_prefix_len("", "abc") == 0
        assert common_prefix_len("abc", "abc") == 3


# --------------------------------------------------------------------------- #
# 2. Policy — when inline typing must not run at all
# --------------------------------------------------------------------------- #
class TestPolicy:
    OK = dict(enabled=True, hotkey="caps lock", hwnd=42,
              is_own_window=False, is_console=False)

    def test_allowed_for_a_plain_key_and_a_normal_window(self):
        assert block_reason(**self.OK) is None

    def test_disabled_setting(self):
        assert block_reason(**{**self.OK, "enabled": False})

    def test_modifier_hotkey_is_refused(self):
        """With ctrl held for the whole utterance every typed character is a
        shortcut, not text."""
        for combo in ("ctrl+shift+r", "ctrl+alt", "alt+f9", "windows+r"):
            reason = block_reason(**{**self.OK, "hotkey": combo})
            assert reason and "modifier" in reason.lower(), combo

    def test_plain_keys_are_accepted(self):
        for key in ("caps lock", "scroll lock", "f9", "insert", "num lock"):
            assert not hotkey_holds_modifier(key)
            assert block_reason(**{**self.OK, "hotkey": key}) is None

    def test_console_is_refused(self):
        assert block_reason(**{**self.OK, "is_console": True})

    def test_own_window_is_refused(self):
        assert block_reason(**{**self.OK, "is_own_window": True})

    def test_missing_target_is_refused(self):
        assert block_reason(**{**self.OK, "hwnd": 0})

    def test_stream_target_matches_the_final_texts_capitalisation(self):
        # finish_transcript capitalizes; matching it keeps the reconciliation
        # a zero-backspace append instead of a full retype.
        assert stream_target("hello there") == "Hello there"
        assert polish_stream_text("  spaced   out  ") == "Spaced out"
        assert stream_target("") == ""
        assert stream_target(None) == ""


# --------------------------------------------------------------------------- #
# 3. The worker state machine (fake Win32 injection)
# --------------------------------------------------------------------------- #
class _FakeWindow:
    """A text box: applies exactly what the injection layer reports sending."""

    def __init__(self, existing=""):
        self.content = existing
        self.user_text_len = len(existing)

    def backspace(self, n):
        self.content = self.content[:len(self.content) - n] if n else self.content

    def type(self, text):
        self.content += text

    @property
    def user_text_intact(self):
        return self.user_text_len == 0 or len(self.content) >= self.user_text_len


class _FakeInput:
    """Stands in for winapi's injection, with scriptable failures.

    `fail_text_after` / `fail_backspace_after` produce a PARTIAL batch (an
    unknown count), which is the state the contract forbids correcting from.
    `refuse` produces a clean refusal (nothing sent).
    """

    def __init__(self, window):
        self.window = window
        self.refuse = False
        # Which window Windows reports as focused. Defaults to the dictation
        # target, so a test only has to say so when it cares about focus.
        self.foreground = TestWorkerState.HWND
        self.fail_text_after = None       # chars to deliver, then report unknown
        self.fail_backspace_after = None
        self.typed_calls = []
        self.backspace_calls = []

    def send_text(self, text, expected_hwnd=None):
        self.typed_calls.append(text)
        if self.refuse:
            return False, 0
        if self.fail_text_after is not None:
            n = self.fail_text_after
            self.window.type(text[:n])
            self.fail_text_after = None
            return False, None
        self.window.type(text)
        return True, len(text)

    def send_backspaces(self, count, expected_hwnd=None):
        self.backspace_calls.append(count)
        if self.refuse:
            return False, 0
        if self.fail_backspace_after is not None:
            n = self.fail_backspace_after
            self.window.backspace(n)
            self.fail_backspace_after = None
            return False, None
        self.window.backspace(count)
        return True, count


@pytest.fixture
def worker(monkeypatch):
    """A real Paster with its Win32 injection faked, driven synchronously.

    Focus is modelled too: the final edit refocuses the target (see
    `_refocus_target`), so without this every test would be fighting the real
    foreground window of whatever is on screen.
    """
    window = _FakeWindow()
    fake = _FakeInput(window)
    monkeypatch.setattr(paste_mod.winapi, "send_text", fake.send_text)
    monkeypatch.setattr(paste_mod.winapi, "send_backspaces", fake.send_backspaces)
    monkeypatch.setattr(paste_mod.winapi, "get_foreground_window",
                        lambda: fake.foreground)
    monkeypatch.setattr(paste_mod.winapi, "wait_for_modifiers_released",
                        lambda timeout=2.0: True)
    monkeypatch.setattr(paste_mod.winapi, "set_foreground_window",
                        lambda hwnd: True)
    p = Paster()
    yield p, fake, window
    p.shutdown()


def _drain(p):
    """Run queued jobs the way the worker would, in order, synchronously."""
    while p._worker.pending():
        pass


class TestWorkerState:
    HWND = 4242

    def _type(self, p, text, session=1):
        p._type_to_job(self.HWND, session, text)

    @staticmethod
    def _collector(done):
        # done_cb is called as (outcome, hwnd, text) from the worker thread.
        return lambda outcome, hwnd, text: done.append(outcome)

    def test_streaming_types_only_the_delta(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Here is")
        self._type(p, "Here is the first")
        assert win.content == "Here is the first"
        assert fake.typed_calls == ["Here is", " the first"], "the whole draft was retyped"
        assert fake.backspace_calls == []

    def test_streaming_correction_replaces_only_the_changed_words(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "The second point is the first")
        self._type(p, "The second point follows from it")
        assert win.content == "The second point follows from it"
        assert fake.backspace_calls == [len("is the first")]

    def test_finalize_corrects_to_the_final_text(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Here is the first point")
        done = []
        p._finalize_inline_job(self.HWND, 1, "Here is the first point.", self._collector(done))
        assert win.content == "Here is the first point."
        assert done == [INLINE_TYPED]
        assert p._typed is None, "the session outlived its dictation"

    def test_finalize_with_nothing_typed_falls_back_to_paste(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        done = []
        p._finalize_inline_job(self.HWND, 1, "Some text.", self._collector(done))
        assert done == [INLINE_NONE]
        assert fake.typed_calls == []

    def test_no_session_falls_back_to_paste(self, worker):
        p, fake, win = worker
        done = []
        p._finalize_inline_job(self.HWND, 1, "Some text.", self._collector(done))
        assert done == [INLINE_NONE]

    def test_partial_batch_stops_corrections_forever(self, worker, monkeypatch):
        """THE cardinal rule: after an unknown count, never delete again."""
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        fake.fail_text_after = 4
        self._type(p, "Hello world")
        assert p._typed.certain is False
        before = win.content
        # Every later operation must leave the window alone.
        self._type(p, "Hello world again")
        assert win.content == before
        assert fake.backspace_calls == [], "a backspace was issued against an unknown count"
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world again.", self._collector(done))
        assert done == [INLINE_PARTIAL]
        assert win.content == before
        assert fake.backspace_calls == []

    def test_partial_backspace_stops_corrections_forever(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello world")
        fake.fail_backspace_after = 2
        self._type(p, "Hello there")
        assert p._typed.certain is False
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello there.", self._collector(done))
        assert done == [INLINE_PARTIAL]

    def test_refusal_sends_nothing_and_keeps_the_record_exact(self, worker):
        """A refusal (focus moved, modifier held) is CERTAIN: nothing changed,
        so the session must stay correctable once conditions are good again."""
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello")
        fake.refuse = True
        self._type(p, "Hello world")
        assert win.content == "Hello"
        assert p._typed.certain is True
        # ...though typing stops for this dictation, the final text is still
        # reconcilable because the record is exact.
        fake.refuse = False
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world.", self._collector(done))
        assert done == [INLINE_TYPED]
        assert win.content == "Hello world."

    def test_oversized_stream_revision_stops_typing_but_finalizes(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        long_draft = "A" + "b" * (MAX_STREAM_BACKSPACES + 20)
        self._type(p, long_draft)
        self._type(p, "A" + "c" * (MAX_STREAM_BACKSPACES + 20))
        assert win.content == long_draft, "a huge mid-flight delete burst was issued"
        assert p._typed.broken is True
        done = []
        p._finalize_inline_job(self.HWND, 1, "A totally different sentence.", self._collector(done))
        assert done == [INLINE_TYPED]
        assert win.content == "A totally different sentence."

    def test_final_correction_over_budget_leaves_the_draft(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "x" * (MAX_FINAL_BACKSPACES + 10))
        done = []
        p._finalize_inline_job(self.HWND, 1, "y" * 10, self._collector(done))
        assert done == [INLINE_PARTIAL]
        assert win.content == "x" * (MAX_FINAL_BACKSPACES + 10)

    def test_cancel_with_erase_takes_back_only_our_own_text(self, worker):
        p, fake, win = worker
        win.content = "user's existing note. "
        win.user_text_len = len(win.content)
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Here is a draft")
        p._cancel_inline_job(1, erase=True)
        assert win.content == "user's existing note. "
        assert win.user_text_intact, "erase deleted the user's own text"
        assert p._typed is None

    def test_cancel_without_erase_leaves_the_window(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "draft")
        p._cancel_inline_job(1, erase=False)
        assert win.content == "draft"
        assert p._typed is None

    def test_cancel_never_erases_an_uncertain_draft(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        fake.fail_text_after = 3
        self._type(p, "draft")
        p._cancel_inline_job(1, erase=True)
        assert fake.backspace_calls == [], "erased against an unknown count"

    def test_streaming_never_steals_focus_back(self, worker, monkeypatch):
        """If the user looks away mid-sentence, typing stops — it does NOT drag
        the window back in front of them. Only the final edit may refocus."""
        p, fake, win = worker
        calls = []
        monkeypatch.setattr(paste_mod.winapi, "set_foreground_window",
                            lambda hwnd: calls.append(hwnd) or True)
        monkeypatch.setattr(paste_mod.winapi, "get_foreground_window", lambda: 999)
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello")
        assert calls == [], "streaming pulled the target window to the front"

    def test_finalize_refocuses_the_target_before_correcting(self, worker, monkeypatch):
        """Alt-tabbing away before releasing the key must still land the text.

        Without this the dictation ended as a truncated draft in the document
        with the real text only on the clipboard — the paste path has always
        refocused for exactly this reason.
        """
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello")
        fg = [self.HWND]
        monkeypatch.setattr(paste_mod.winapi, "get_foreground_window", lambda: fg[0])
        monkeypatch.setattr(paste_mod.winapi, "wait_for_modifiers_released",
                            lambda timeout=2.0: True)

        def refocus(hwnd):
            fg[0] = hwnd
            return True

        monkeypatch.setattr(paste_mod.winapi, "set_foreground_window", refocus)
        monkeypatch.setattr(paste_mod.time, "sleep", lambda s: None)
        fg[0] = 999  # the user looked away
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world.", self._collector(done))
        assert done == [INLINE_TYPED]
        assert win.content == "Hello world."

    def test_finalize_gives_up_when_the_window_refuses_focus(self, worker, monkeypatch):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello")
        monkeypatch.setattr(paste_mod.winapi, "get_foreground_window", lambda: 999)
        monkeypatch.setattr(paste_mod.winapi, "wait_for_modifiers_released",
                            lambda timeout=2.0: True)
        monkeypatch.setattr(paste_mod.winapi, "set_foreground_window", lambda hwnd: False)
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world.", self._collector(done))
        assert done == [INLINE_PARTIAL]
        assert win.content == "Hello", "the window was edited without focus"

    def test_finalize_does_not_fight_a_held_modifier(self, worker, monkeypatch):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello")
        monkeypatch.setattr(paste_mod.winapi, "get_foreground_window", lambda: 999)
        monkeypatch.setattr(paste_mod.winapi, "wait_for_modifiers_released",
                            lambda timeout=2.0: False)
        focus_calls = []
        monkeypatch.setattr(paste_mod.winapi, "set_foreground_window",
                            lambda hwnd: focus_calls.append(hwnd) or True)
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world.", self._collector(done))
        assert done == [INLINE_PARTIAL]
        assert focus_calls == []

    def test_typing_resumes_at_finalize_after_a_mid_sentence_refusal(self, worker, monkeypatch):
        """A refusal during streaming truncates the draft but must not lose the
        rest of the dictation: the final edit appends what was missed."""
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Here is the first point.")
        fake.refuse = True
        self._type(p, "Here is the first point. Finally,")
        assert win.content == "Here is the first point."
        fake.refuse = False
        done = []
        p._finalize_inline_job(
            self.HWND, 1, "Here is the first point. Finally, the third.",
            self._collector(done))
        assert done == [INLINE_TYPED]
        assert win.content == "Here is the first point. Finally, the third."

    def test_a_stale_job_cannot_touch_the_next_dictation(self, worker):
        """Back-to-back dictations usually share an HWND, so the session id is
        the only thing keeping one dictation's queued jobs off the next."""
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "first dictation")
        p._begin_inline_job(self.HWND, 2)      # a new hold starts
        self._type(p, "stale text", session=1)  # a job queued by the old one
        assert "stale" not in win.content
        done = []
        p._finalize_inline_job(self.HWND, 1, "old final", self._collector(done))
        assert done == [INLINE_NONE], "an old job finalized the new session"

    def test_wrong_window_is_never_typed_into(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        p._type_to_job(9999, 1, "text for another window")
        assert fake.typed_calls == []

    def test_partial_finalize_puts_the_accurate_text_on_the_clipboard(self, worker, monkeypatch):
        p, fake, win = worker
        copied = []
        monkeypatch.setattr(paste_mod.pyperclip, "copy", copied.append)
        p._begin_inline_job(self.HWND, 1)
        fake.fail_text_after = 2
        self._type(p, "Hello")
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world.", self._collector(done))
        assert done == [INLINE_PARTIAL]
        assert copied == ["Hello world."], "the user was left with no accurate text"

    def test_finalize_never_raises_out(self, worker, monkeypatch):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        self._type(p, "Hello")
        monkeypatch.setattr(paste_mod, "plan_edit",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        done = []
        p._finalize_inline_job(self.HWND, 1, "Hello world.", self._collector(done))
        assert done == [INLINE_PARTIAL], "a crash in finalize lost the dictation"


# --------------------------------------------------------------------------- #
# 4. winapi injection contract (no real SendInput)
# --------------------------------------------------------------------------- #
class TestInjectionContract:
    def test_send_text_reports_exact_counts(self, monkeypatch):
        from voiceassistant import winapi

        monkeypatch.setattr(winapi, "_guarded", lambda hwnd: True)
        monkeypatch.setattr(winapi, "_unicode_batch",
                            lambda chunk: (len(chunk) * 2, len(chunk) * 2))
        assert winapi.send_text("hello", expected_hwnd=1) == (True, 5)
        assert winapi.send_text("", expected_hwnd=1) == (True, 0)

    def test_partial_batch_reports_unknown(self, monkeypatch):
        from voiceassistant import winapi

        monkeypatch.setattr(winapi, "_guarded", lambda hwnd: True)
        monkeypatch.setattr(winapi, "_unicode_batch", lambda chunk: (2, len(chunk) * 2))
        ok, sent = winapi.send_text("hello", expected_hwnd=1)
        assert ok is False and sent is None, "a partial batch claimed a known count"

    def test_refusal_reports_zero_not_unknown(self, monkeypatch):
        from voiceassistant import winapi

        monkeypatch.setattr(winapi, "_guarded", lambda hwnd: False)
        assert winapi.send_text("hello", expected_hwnd=1) == (False, 0)
        assert winapi.send_backspaces(3, expected_hwnd=1) == (False, 0)

    def test_backspaces_report_exact_counts(self, monkeypatch):
        from voiceassistant import winapi

        monkeypatch.setattr(winapi, "_guarded", lambda hwnd: True)
        monkeypatch.setattr(winapi, "_input_batch", lambda events: len(events))
        assert winapi.send_backspaces(5, expected_hwnd=1) == (True, 5)
        assert winapi.send_backspaces(0, expected_hwnd=1) == (True, 0)

    def test_partial_backspace_batch_reports_unknown(self, monkeypatch):
        from voiceassistant import winapi

        monkeypatch.setattr(winapi, "_guarded", lambda hwnd: True)
        monkeypatch.setattr(winapi, "_input_batch", lambda events: 2)
        ok, sent = winapi.send_backspaces(5, expected_hwnd=1)
        assert ok is False and sent is None

    def test_exception_reports_unknown(self, monkeypatch):
        from voiceassistant import winapi

        def boom(*a):
            raise OSError("SendInput exploded")

        monkeypatch.setattr(winapi, "_guarded", lambda hwnd: True)
        monkeypatch.setattr(winapi, "_unicode_batch", boom)
        assert winapi.send_text("hi", expected_hwnd=1) == (False, None)

    def test_guard_requires_the_target_window_and_no_modifiers(self, monkeypatch):
        from voiceassistant import winapi

        monkeypatch.setattr(winapi, "get_foreground_window", lambda: 7)
        monkeypatch.setattr(winapi, "modifiers_down", lambda: ())
        assert winapi._guarded(7) is True
        assert winapi._guarded(8) is False, "typed into a window that is not the target"
        monkeypatch.setattr(winapi, "modifiers_down", lambda: (0xA2,))
        assert winapi._guarded(7) is False, "typed while a modifier was held"

    def test_unicode_batch_pairs_events_per_character(self, monkeypatch):
        from voiceassistant import winapi

        sent = {}

        def fake_sendinput(n, inputs, size):
            sent["n"] = n
            sent["scans"] = [inputs[i].ki.wScan for i in range(n)]
            sent["flags"] = [inputs[i].ki.dwFlags for i in range(n)]
            return n

        monkeypatch.setattr(winapi.user32, "SendInput", fake_sendinput)
        inserted, expected = winapi._unicode_batch("hi")
        assert inserted == expected == 4  # two chars, down+up each
        assert sent["scans"] == [ord("h"), ord("h"), ord("i"), ord("i")]
        # Every event carries the UNICODE flag; no virtual key, no modifier.
        assert all(f & winapi.KEYEVENTF_UNICODE for f in sent["flags"])


# --------------------------------------------------------------------------- #
# 5. MainWindow wiring
# --------------------------------------------------------------------------- #
@pytest.fixture
def mw(qapp, monkeypatch):
    import voiceassistant.config as cfg
    import voiceassistant.ocr as ocr
    import voiceassistant.recorder as rec_mod
    import voiceassistant.transcriber as trm
    import voiceassistant.tts as tts
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    monkeypatch.setattr(cfg, "CONFIG_FILE",
                        os.path.join(tempfile.mkdtemp(), "settings.json"))
    monkeypatch.setattr(trm.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(tts.TTSEngine, "_load_kokoro", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)
    monkeypatch.setattr(rec_mod.VoiceRecorder, "open_stream", lambda self: True)
    monkeypatch.setattr(winapi, "is_console_window", lambda hwnd: False)
    w = MainWindow(entry_script="main.py")
    w.transcriber._model = object()
    w.config.set("inline_typing", True)
    w.config.set("hotkey_record", "caps lock")
    w.live_preview._timer.stop()
    calls = []
    monkeypatch.setattr(w.paster, "begin_inline",
                        lambda hwnd, s: calls.append(("begin", hwnd, s)))
    monkeypatch.setattr(w.paster, "type_to",
                        lambda hwnd, s, text: calls.append(("type", hwnd, s, text)))
    monkeypatch.setattr(w.paster, "cancel_inline",
                        lambda s=None, erase=False: calls.append(("cancel", s, erase)))
    monkeypatch.setattr(w.paster, "finalize_inline",
                        lambda hwnd, s, text, cb: calls.append(("finalize", hwnd, s, text)))
    monkeypatch.setattr(w.paster, "submit",
                        lambda hwnd, text, cb: calls.append(("paste", hwnd, text)))
    w._inline_calls = calls
    yield w
    for closer in (w._show_request_timer.stop, w.live_preview.shutdown,
                   w.tts.shutdown, w.paster.shutdown,
                   w.transcriber._worker.shutdown, w._selection_reader.shutdown):
        try:
            closer()
        except Exception:
            pass


def _loud(seconds=1.0):
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _result(job_id, text, hwnd, duration=1.0, retried=False):
    from voiceassistant.transcriber import TranscriptionResult

    return TranscriptionResult(text=text, job_id=job_id, context=hwnd,
                               duration_s=duration, retried=retried,
                               no_speech=not text)


class TestWindowWiring:
    HWND = 777

    def _start(self, mw):
        mw._pending_target_hwnd = self.HWND
        mw._on_recording_started()

    def test_session_opens_and_drafts_are_typed(self, mw):
        self._start(mw)
        assert ("begin", self.HWND, 1) in mw._inline_calls
        mw._on_preview_draft("hello there", "friend")
        # Only the STABLE half is typed; the moving tail stays on the pill.
        assert ("type", self.HWND, 1, "Hello there") in mw._inline_calls

    def test_disabled_setting_types_nothing(self, mw):
        mw.config.set("inline_typing", False)
        self._start(mw)
        assert mw._inline_target is None
        mw._on_preview_draft("hello", "")
        assert not any(c[0] in ("begin", "type") for c in mw._inline_calls)

    def test_modifier_hotkey_types_nothing(self, mw):
        mw.config.set("hotkey_record", "ctrl+shift+r")
        self._start(mw)
        assert mw._inline_target is None
        mw._on_preview_draft("hello", "")
        assert not any(c[0] == "type" for c in mw._inline_calls)

    def test_console_target_types_nothing(self, mw, monkeypatch):
        import voiceassistant.winapi as winapi
        monkeypatch.setattr(winapi, "is_console_window", lambda hwnd: True)
        self._start(mw)
        assert mw._inline_target is None

    def test_own_window_types_nothing(self, mw):
        mw._own_hwnds.add(self.HWND)
        self._start(mw)
        assert mw._inline_target is None

    def test_final_text_is_reconciled_not_pasted(self, mw):
        self._start(mw)
        mw._on_preview_draft("hello there", "")
        mw._on_recording_stopped(_loud())
        job_id = max(mw._inline_jobs) if mw._inline_jobs else None
        assert job_id is not None, "the inline session was not bound to the job"
        mw._on_transcription_ready(_result(job_id, "Hello there.", self.HWND))
        assert ("finalize", self.HWND, 1, "Hello there.") in mw._inline_calls
        assert not any(c[0] == "paste" for c in mw._inline_calls), "pasted a second copy"

    def test_without_a_session_the_normal_paste_still_runs(self, mw):
        mw.config.set("inline_typing", False)
        self._start(mw)
        mw._on_recording_stopped(_loud())
        mw._on_transcription_ready(_result(1, "Hello there.", self.HWND))
        assert any(c[0] == "paste" for c in mw._inline_calls)

    def test_dropped_short_clip_takes_its_draft_back(self, mw):
        self._start(mw)
        mw._on_preview_draft("hello", "")
        mw._on_recording_stopped(_loud(0.15))  # under min_record_seconds
        assert ("cancel", 1, True) in mw._inline_calls

    def test_dropped_quiet_clip_takes_its_draft_back(self, mw):
        self._start(mw)
        mw._on_preview_draft("hello", "")
        quiet = (_loud() * 0.0005).astype(np.float32)
        mw._on_recording_stopped(quiet)
        assert ("cancel", 1, True) in mw._inline_calls

    def test_no_speech_result_takes_its_draft_back(self, mw):
        self._start(mw)
        mw._on_preview_draft("hello", "")
        mw._on_recording_stopped(_loud())
        job_id = max(mw._inline_jobs)
        mw._on_transcription_ready(_result(job_id, "", self.HWND))
        assert ("cancel", 1, True) in mw._inline_calls

    def test_hallucination_takes_its_draft_back(self, mw):
        self._start(mw)
        mw._on_preview_draft("thanks for watching", "")
        mw._on_recording_stopped(_loud())
        job_id = max(mw._inline_jobs)
        mw._on_transcription_ready(
            _result(job_id, "Thanks for watching", self.HWND,
                    duration=0.8, retried=True))
        assert ("cancel", 1, True) in mw._inline_calls

    def test_outcomes_are_recorded(self, mw):
        from voiceassistant import metrics
        self._start(mw)
        mw._metrics_awaiting_paste.append({"chars": 5})
        mw._on_inline_done(INLINE_TYPED, self.HWND, "Hello.")
        rows = metrics.load()
        assert rows and rows[-1]["outcome"] == metrics.OUTCOME_INLINE_TYPED
        mw._metrics_awaiting_paste.append({"chars": 5})
        mw._on_inline_done(INLINE_PARTIAL, self.HWND, "Hello.")
        rows = metrics.load()
        assert rows[-1]["outcome"] == metrics.OUTCOME_INLINE_PARTIAL
        # A partial run must tell the user where the accurate text went.
        assert "clipboard" in mw.status_bar.currentMessage().lower()

    def test_the_worker_callback_matches_the_signal_it_is_given(self, mw):
        """The worker calls done_cb(outcome, hwnd, text) and the window hands it
        a Qt signal's emit. An arity or type mismatch there only ever shows up
        at runtime, on the real path, after the dictation is already spoken —
        so emit the REAL signal with the REAL argument shape here.
        """
        received = []
        mw._sig_inline_done.connect(lambda o, h, t: received.append((o, h, t)))
        mw._metrics_awaiting_paste.append({"chars": 4})
        mw._sig_inline_done.emit(INLINE_NONE, self.HWND, "Yes.")
        assert received == [(INLINE_NONE, self.HWND, "Yes.")]
        assert ("paste", self.HWND, "Yes.") in mw._inline_calls

    def test_nothing_typed_still_pastes_the_dictation(self, mw):
        """A hold too short to produce a draft must still land its text.

        Regression: INLINE_NONE was treated as a failure, so short dictations
        were silently lost — caught on the first live dictation after release.
        """
        self._start(mw)
        mw._metrics_awaiting_paste.append({"chars": 4})
        mw._on_inline_done(INLINE_NONE, self.HWND, "Yes.")
        assert ("paste", self.HWND, "Yes.") in mw._inline_calls
        assert len(mw._metrics_awaiting_paste) == 1, "the metrics row was dropped"

    def test_partial_outcome_is_counted_as_a_bad_dictation(self):
        from voiceassistant import metrics
        s = metrics.summarize([{"outcome": metrics.OUTCOME_INLINE_PARTIAL},
                               {"outcome": metrics.OUTCOME_INLINE_TYPED}])
        assert s["success_rate"] == 0.5
        assert "Inline-typed drafts" in metrics.format_report(
            [{"outcome": metrics.OUTCOME_INLINE_PARTIAL}])


# --------------------------------------------------------------------------- #
# 7. Audit regressions (2026-09-12) — every one of these was a real defect
# --------------------------------------------------------------------------- #
class TestAuditRegressions:
    """Each test here names a defect found by the full audit and the damage it
    did to the user's document. None of these are hypothetical."""

    HWND = TestWorkerState.HWND

    def test_a_new_dictation_reclaims_an_unfinished_drafts_characters(self, worker):
        """Chaining two dictations used to leave the first draft orphaned and
        then paste its text underneath: "Hello therHello there."

        Nothing can correct a draft whose session is being replaced, so the
        last moment we still know what those characters were is right here.
        """
        p, fake, win = worker
        win.content = "user note. "
        win.user_text_len = len(win.content)
        p._begin_inline_job(self.HWND, 1)
        p._type_to_job(self.HWND, 1, "Hello ther")
        assert win.content == "user note. Hello ther"
        # The user releases and immediately holds again; the first decode has
        # not come back yet.
        p._begin_inline_job(self.HWND, 2)
        assert win.content == "user note. ", "the orphaned draft was left behind"
        assert win.user_text_intact
        assert p._typed.session == 2 and p._typed.text == ""

    def test_reclaim_never_deletes_an_uncertain_draft(self, worker):
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        fake.fail_text_after = 3
        p._type_to_job(self.HWND, 1, "Hello")
        before = win.content
        p._begin_inline_job(self.HWND, 2)
        assert win.content == before, "erased against an unknown count"

    def test_a_broken_session_still_gives_its_characters_back(self, worker):
        """`broken` means typing STOPPED, not that the record is wrong. Skipping
        the erase left a half-sentence in the document with no explanation."""
        p, fake, win = worker
        p._begin_inline_job(self.HWND, 1)
        p._type_to_job(self.HWND, 1, "A draft")
        p._typed.broken = True
        p._cancel_inline_job(1, erase=True)
        assert win.content == "", "a broken session kept its characters"
