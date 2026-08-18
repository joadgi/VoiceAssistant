"""Headless tests for SelectionReader (read-aloud selection capture).

Fully mocked — no real clipboard, focus, keyboard, UIA or sleeps — so this runs
in the default suite and covers the capture LOGIC that previously lived only
in the (display-required, un-runnable-in-CI) live test.

The cascade under test:
  TIER 1  UI Automation      — must short-circuit everything else when it hits
  TIER 2  clipboard sentinel — Escape only for a Windows-key hotkey; refocus
                               failure aborts before Ctrl+C; clipboard always
                               restored; NEVER Ctrl+C into a console window
  TIER 3  (OCR)              — owned by window.py; here we only assert the
                               source code that tells the caller to escalate
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voiceassistant import selection as sel_mod
from voiceassistant.selection import (
    SRC_CLIPBOARD, SRC_CONSOLE_BLOCKED, SRC_EMPTY, SRC_REFOCUS_FAILED, SRC_UIA,
    SelectionReader,
)


class _FakeClip:
    """In-memory stand-in for pyperclip (never touches the real clipboard)."""

    def __init__(self, value=""):
        self.value = value

    def paste(self):
        return self.value

    def copy(self, v):
        self.value = v


@pytest.fixture
def env(monkeypatch):
    """Neutralize all side effects; return handles to drive/inspect them."""
    clip = _FakeClip("ORIGINAL-CLIP")
    esc = {"n": 0}
    monkeypatch.setattr(sel_mod, "pyperclip", clip)
    monkeypatch.setattr(sel_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(sel_mod.kb, "is_pressed", lambda *_a, **_k: False)
    monkeypatch.setattr(sel_mod.winapi, "set_foreground_window", lambda hwnd: True)
    monkeypatch.setattr(sel_mod.winapi, "is_console_window", lambda hwnd: False)
    monkeypatch.setattr(sel_mod.winapi, "send_escape",
                        lambda: esc.__setitem__("n", esc["n"] + 1))
    # Tier 1 finds nothing by default, so the tier-2 tests exercise Ctrl+C.
    monkeypatch.setattr(sel_mod.uia, "get_selection", lambda hwnd=None: "")
    # By default Ctrl+C "copies a selection" onto the (fake) clipboard.
    monkeypatch.setattr(sel_mod.winapi, "send_ctrl_c",
                        lambda: clip.copy("the selected text"))
    return {"clip": clip, "esc": esc, "monkeypatch": monkeypatch}


def _reader():
    r = SelectionReader()
    r.shutdown()  # we call _capture directly and synchronously; no worker needed
    return r


# --------------------------------------------------------------------------- #
# TIER 1 — UI Automation
# --------------------------------------------------------------------------- #
def test_uia_hit_short_circuits_the_clipboard_path(env):
    """The whole point of tier 1: no clipboard, no keystrokes, no focus switch.
    Those are what made read-aloud fail on copy-blocked content, and what made
    it dangerous with a terminal focused."""
    mp = env["monkeypatch"]
    touched = {"ctrl_c": 0, "refocus": 0}
    mp.setattr(sel_mod.uia, "get_selection", lambda hwnd=None: "highlighted words")
    mp.setattr(sel_mod.winapi, "send_ctrl_c",
               lambda: touched.__setitem__("ctrl_c", touched["ctrl_c"] + 1))
    mp.setattr(sel_mod.winapi, "set_foreground_window",
               lambda hwnd: touched.__setitem__("refocus", touched["refocus"] + 1) or True)

    text, source = _reader()._capture("ctrl+m", target_hwnd=1234)

    assert (text, source) == ("highlighted words", SRC_UIA)
    assert touched["ctrl_c"] == 0, "sent Ctrl+C despite UIA already having the text"
    assert touched["refocus"] == 0, "stole focus despite UIA already having the text"
    assert env["clip"].value == "ORIGINAL-CLIP", "clipboard was touched"


def test_uia_failure_falls_through_to_the_clipboard(env):
    """A broken/absent UIA must degrade, not break read-aloud."""
    def boom(hwnd=None):
        raise OSError("simulated UIA failure")

    env["monkeypatch"].setattr(sel_mod.uia, "get_selection", boom)
    text, source = _reader()._capture("ctrl+m", target_hwnd=1)
    assert (text, source) == ("the selected text", SRC_CLIPBOARD)


def test_uia_whitespace_is_collapsed(env):
    """UIA returns layout whitespace from tables/PDFs; read aloud that becomes
    long dead pauses."""
    env["monkeypatch"].setattr(
        sel_mod.uia, "get_selection",
        lambda hwnd=None: "line one\n\n\tline   two\r\n   line three  ")
    text, source = _reader()._capture("ctrl+m", target_hwnd=1)
    assert source == SRC_UIA
    assert text == "line one line two line three"


# --------------------------------------------------------------------------- #
# TIER 2 — clipboard sentinel
# --------------------------------------------------------------------------- #
def test_captures_selection_and_restores_clipboard(env):
    text, source = _reader()._capture("ctrl+shift+t", target_hwnd=1234)
    assert (text, source) == ("the selected text", SRC_CLIPBOARD)
    assert env["clip"].value == "ORIGINAL-CLIP"  # restored, selection not left behind
    assert env["esc"]["n"] == 0                   # no Escape for a non-windows hotkey


def test_escape_only_for_windows_hotkey(env):
    _reader()._capture("ctrl+m", target_hwnd=1)
    assert env["esc"]["n"] == 0
    _reader()._capture("windows+m", target_hwnd=1)
    assert env["esc"]["n"] == 1


def test_never_sends_ctrl_c_into_a_console(env):
    """In a terminal Ctrl+C means INTERRUPT. Read-aloud used to send it
    unconditionally, killing whatever command was running."""
    mp = env["monkeypatch"]
    ctrl_c = {"n": 0}
    mp.setattr(sel_mod.winapi, "is_console_window", lambda hwnd: True)
    mp.setattr(sel_mod.winapi, "send_ctrl_c",
               lambda: ctrl_c.__setitem__("n", ctrl_c["n"] + 1))

    text, source = _reader()._capture("ctrl+m", target_hwnd=42)

    assert ctrl_c["n"] == 0, "sent Ctrl+C into a console — would kill a command"
    assert (text, source) == ("", SRC_CONSOLE_BLOCKED)
    assert env["clip"].value == "ORIGINAL-CLIP", "clipboard touched anyway"


def test_refocus_failure_aborts_without_copy(env):
    env["monkeypatch"].setattr(sel_mod.winapi, "set_foreground_window", lambda hwnd: False)
    ctrl_c = {"n": 0}
    env["monkeypatch"].setattr(sel_mod.winapi, "send_ctrl_c",
                               lambda: ctrl_c.__setitem__("n", ctrl_c["n"] + 1))
    text, source = _reader()._capture("ctrl+shift+t", target_hwnd=999)
    assert (text, source) == ("", SRC_REFOCUS_FAILED)  # never read the wrong window
    assert ctrl_c["n"] == 0                            # Ctrl+C not sent
    assert env["clip"].value == "ORIGINAL-CLIP"        # clipboard untouched


def test_nothing_selected_returns_empty_and_restores(env):
    # Ctrl+C copies nothing -> the sentinel is never overwritten.
    env["monkeypatch"].setattr(sel_mod.winapi, "send_ctrl_c", lambda: None)
    text, source = _reader()._capture("ctrl+shift+t", target_hwnd=1)
    assert (text, source) == ("", SRC_EMPTY)
    assert env["clip"].value == "ORIGINAL-CLIP"  # sentinel cleared, original back


def test_empty_prior_clipboard_sentinel_cleared(env):
    env["clip"].value = ""  # user's clipboard was empty
    env["monkeypatch"].setattr(sel_mod.winapi, "send_ctrl_c", lambda: None)
    text, source = _reader()._capture("ctrl+shift+t", target_hwnd=1)
    assert (text, source) == ("", SRC_EMPTY)
    assert env["clip"].value == ""  # NUL sentinel not left behind


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
def test_job_always_calls_back_even_on_exception(env):
    # The wedge-prevention contract: if _capture raises, _job must still call
    # done_cb (with "") so the caller's in-flight flag can't get stuck.
    r = _reader()

    def boom(*_a):
        raise RuntimeError("capture blew up")

    env["monkeypatch"].setattr(r, "_capture", boom)
    got = []
    r._job("ctrl+shift+t", 1, lambda t, s: got.append((t, s)))
    assert got == [("", SRC_EMPTY)]


def test_capture_via_worker_invokes_callback(monkeypatch):
    # The public async path: capture() -> worker -> done_cb(text, source).
    import threading

    clip = _FakeClip("ORIG")
    monkeypatch.setattr(sel_mod, "pyperclip", clip)
    monkeypatch.setattr(sel_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(sel_mod.kb, "is_pressed", lambda *_a, **_k: False)
    monkeypatch.setattr(sel_mod.winapi, "set_foreground_window", lambda hwnd: True)
    monkeypatch.setattr(sel_mod.winapi, "is_console_window", lambda hwnd: False)
    monkeypatch.setattr(sel_mod.winapi, "send_escape", lambda: None)
    monkeypatch.setattr(sel_mod.uia, "get_selection", lambda hwnd=None: "")
    monkeypatch.setattr(sel_mod.winapi, "send_ctrl_c", lambda: clip.copy("worker sel"))

    got, done = [], threading.Event()
    r = SelectionReader()
    try:
        r.capture("ctrl+shift+t", 1,
                  lambda t, s: (got.append((t, s)), done.set()))
        assert done.wait(5), "capture callback never fired"
        assert got == [("worker sel", SRC_CLIPBOARD)]
    finally:
        r.shutdown()
