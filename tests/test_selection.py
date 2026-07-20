"""Headless tests for SelectionReader (read-aloud selection capture).

Fully mocked — no real clipboard, focus, keyboard, or sleeps — so this runs
in the default suite and covers the capture LOGIC that previously lived only
in the (display-required, un-runnable-in-CI) live test:
  * Escape injected ONLY for a Windows-key hotkey (M2),
  * refocus-failure aborts before Ctrl+C (no wrong-window copy),
  * the clipboard is always restored (sentinel cleared / selection not left),
  * nothing-selected -> "".
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voiceassistant import selection as sel_mod
from voiceassistant.selection import SelectionReader


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
    monkeypatch.setattr(sel_mod.winapi, "send_escape",
                        lambda: esc.__setitem__("n", esc["n"] + 1))
    # By default Ctrl+C "copies a selection" onto the (fake) clipboard.
    monkeypatch.setattr(sel_mod.winapi, "send_ctrl_c",
                        lambda: clip.copy("the selected text"))
    return {"clip": clip, "esc": esc, "monkeypatch": monkeypatch}


def _reader():
    r = SelectionReader()
    r.shutdown()  # we call _capture directly and synchronously; no worker needed
    return r


def test_captures_selection_and_restores_clipboard(env):
    text = _reader()._capture("ctrl+shift+t", target_hwnd=1234)
    assert text == "the selected text"
    assert env["clip"].value == "ORIGINAL-CLIP"  # restored, selection not left behind
    assert env["esc"]["n"] == 0                   # no Escape for a non-windows hotkey


def test_escape_only_for_windows_hotkey(env):
    _reader()._capture("ctrl+m", target_hwnd=1)
    assert env["esc"]["n"] == 0
    _reader()._capture("windows+m", target_hwnd=1)
    assert env["esc"]["n"] == 1


def test_refocus_failure_aborts_without_copy(env):
    env["monkeypatch"].setattr(sel_mod.winapi, "set_foreground_window", lambda hwnd: False)
    ctrl_c = {"n": 0}
    env["monkeypatch"].setattr(sel_mod.winapi, "send_ctrl_c",
                               lambda: ctrl_c.__setitem__("n", ctrl_c["n"] + 1))
    text = _reader()._capture("ctrl+shift+t", target_hwnd=999)
    assert text == ""                    # bailed — never read the wrong window
    assert ctrl_c["n"] == 0              # Ctrl+C not sent
    assert env["clip"].value == "ORIGINAL-CLIP"  # clipboard untouched


def test_nothing_selected_returns_empty_and_restores(env):
    # Ctrl+C copies nothing -> the sentinel is never overwritten.
    env["monkeypatch"].setattr(sel_mod.winapi, "send_ctrl_c", lambda: None)
    text = _reader()._capture("ctrl+shift+t", target_hwnd=1)
    assert text == ""
    assert env["clip"].value == "ORIGINAL-CLIP"  # sentinel cleared, original back


def test_empty_prior_clipboard_sentinel_cleared(env):
    env["clip"].value = ""  # user's clipboard was empty
    env["monkeypatch"].setattr(sel_mod.winapi, "send_ctrl_c", lambda: None)
    text = _reader()._capture("ctrl+shift+t", target_hwnd=1)
    assert text == ""
    assert env["clip"].value == ""  # NUL sentinel not left behind


def test_job_always_calls_back_even_on_exception(env):
    # The wedge-prevention contract: if _capture raises, _job must still call
    # done_cb (with "") so the caller's in-flight flag can't get stuck.
    r = _reader()
    def boom(*_a):
        raise RuntimeError("capture blew up")
    env["monkeypatch"].setattr(r, "_capture", boom)
    got = []
    r._job("ctrl+shift+t", 1, got.append)
    assert got == [""]


def test_capture_via_worker_invokes_callback(monkeypatch):
    # The public async path: capture() -> worker -> done_cb(text).
    import threading
    clip = _FakeClip("ORIG")
    monkeypatch.setattr(sel_mod, "pyperclip", clip)
    monkeypatch.setattr(sel_mod.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(sel_mod.kb, "is_pressed", lambda *_a, **_k: False)
    monkeypatch.setattr(sel_mod.winapi, "set_foreground_window", lambda hwnd: True)
    monkeypatch.setattr(sel_mod.winapi, "send_escape", lambda: None)
    monkeypatch.setattr(sel_mod.winapi, "send_ctrl_c", lambda: clip.copy("worker sel"))

    got, done = [], threading.Event()
    r = SelectionReader()
    try:
        r.capture("ctrl+shift+t", 1, lambda t: (got.append(t), done.set()))
        assert done.wait(5), "capture callback never fired"
        assert got == ["worker sel"]
    finally:
        r.shutdown()
