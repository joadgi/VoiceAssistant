"""Keyboard regression tests. No real hooks, clipboard, or input injection."""
import itertools
import threading
import time
from types import SimpleNamespace

import pytest

from voiceassistant import winapi, paste, selection
from voiceassistant.read_hotkey import ReadHotkey


class Keys:
    codes = {"ctrl": (29, 285), "alt": (56, 312), "shift": (42, 54),
             "windows": (91, 92), "alt gr": (312,), "m": (50,), "f9": (67,)}

    @classmethod
    def key_to_scan_codes(cls, key):
        return cls.codes[key]


def event(direction, code):
    return SimpleNamespace(event_type=direction, scan_code=code)


@pytest.mark.parametrize("ctrl,alt", list(itertools.product((29, 285), (56, 312))))
def test_modifier_only_read_chord_passes_every_edge_and_fires_once(ctrl, alt):
    for order in ((ctrl, alt), (alt, ctrl)):
        for release in ((ctrl, alt), (alt, ctrl)):
            fired = []
            hook = ReadHotkey("ctrl+alt", lambda: fired.append(1) or True, Keys)
            edges = [("down", k) for k in order]
            edges += [("down", order[-1])] * 4  # autorepeat
            edges += [("up", k) for k in release]
            assert all(hook(event(*edge)) is True for edge in edges)
            assert fired == [1]
            assert not hook._held and not hook._blocked
            assert hook(event("down", ctrl)) is True
            assert hook(event("down", alt)) is True
            assert fired == [1, 1]


def test_nonmodifier_read_blocks_only_its_paired_edges_even_for_truthy_callback():
    fired = []
    hook = ReadHotkey("ctrl+m", lambda: fired.append(1) or True, Keys)
    assert hook(event("down", 29)) is True
    assert hook(event("down", 50)) is False
    assert hook(event("down", 50)) is False
    assert hook(event("up", 29)) is True
    assert hook(event("up", 50)) is False
    assert fired == [1]
    assert hook(event("down", 50)) is True  # ordinary M is unchanged
    assert hook(event("up", 50)) is True


def test_modifier_pressed_last_never_gets_suppressed():
    fired = []
    hook = ReadHotkey("ctrl+m", lambda: fired.append(1), Keys)
    assert hook(event("down", 50)) is True
    assert hook(event("down", 29)) is True
    assert hook(event("up", 50)) is True
    assert hook(event("up", 29)) is True
    assert fired == [1]


@pytest.fixture
def fake_input(monkeypatch):
    batches, held = [], set()

    def send(count, inputs, size):
        assert size == winapi.ctypes.sizeof(winapi._INPUT)
        events = [(x.ki.wVk, bool(x.ki.dwFlags & 2)) for x in inputs]
        batches.append(events)
        for vk, up in events:
            if up:
                held.discard(vk)
            else:
                held.add(vk)
        return count

    monkeypatch.setattr(winapi, "modifiers_down", lambda: ())
    monkeypatch.setattr(winapi.user32, "SendInput", send)
    return batches, held


@pytest.mark.parametrize("sender,key", [("send_ctrl_v", 86), ("send_ctrl_c", 67)])
def test_shortcut_is_one_balanced_batch(fake_input, sender, key):
    batches, held = fake_input
    assert getattr(winapi, sender)() is True
    assert batches == [[(17, False), (key, False), (key, True), (17, True)]]
    assert not held


def test_held_modifier_prevents_all_injection(fake_input, monkeypatch):
    monkeypatch.setattr(winapi, "modifiers_down", lambda: (164,))
    assert winapi.send_ctrl_v() is False
    assert fake_input[0] == []


@pytest.mark.parametrize("accepted", [0, 1, 2, 3])
def test_partial_batch_releases_without_repeating_the_action(fake_input, monkeypatch, accepted):
    batches, held = fake_input
    original = winapi.user32.SendInput
    calls = []

    def partial(count, inputs, size):
        calls.append(count)
        if len(calls) == 1:
            original(accepted, list(inputs)[:accepted], size)
            return accepted
        return original(count, inputs, size)

    monkeypatch.setattr(winapi.user32, "SendInput", partial)
    assert winapi.send_ctrl_v() is False
    assert calls == ([4, 2] if accepted else [4])
    if accepted:
        assert batches[-1] == [(86, True), (17, True)]
    assert not held


def test_exception_after_ctrl_down_attempts_cleanup(fake_input, monkeypatch):
    original = winapi.user32.SendInput
    calls = []

    def broken(count, inputs, size):
        calls.append(count)
        if len(calls) == 1:
            original(1, list(inputs)[:1], size)
            raise OSError("simulated interrupted delivery")
        return original(count, inputs, size)

    monkeypatch.setattr(winapi.user32, "SendInput", broken)
    assert winapi.send_ctrl_c() is False
    assert not fake_input[1]
    assert calls == [4, 2]


class Clip:
    def __init__(self):
        self.value = "original"

    def copy(self, value):
        self.value = value

    def paste(self):
        return self.value


@pytest.fixture
def io(monkeypatch):
    clip, sent = Clip(), []
    monkeypatch.setattr(paste, "pyperclip", clip)
    monkeypatch.setattr(selection, "pyperclip", clip)
    monkeypatch.setattr(winapi, "get_foreground_window", lambda: 123)
    monkeypatch.setattr(winapi, "set_foreground_window", lambda hwnd: True)
    monkeypatch.setattr(winapi, "is_console_window", lambda hwnd: False)
    monkeypatch.setattr(winapi, "send_ctrl_v", lambda: sent.append("paste") or True)
    monkeypatch.setattr(winapi, "send_ctrl_c", lambda: sent.append("copy") or True)
    monkeypatch.setattr(winapi, "wait_for_modifiers_released", lambda timeout: True)
    monkeypatch.setattr(selection.uia, "get_selection", lambda hwnd: "")
    return clip, sent


def test_paste_timeout_keeps_text_without_sending_shortcuts(io, monkeypatch):
    monkeypatch.setattr(winapi, "wait_for_modifiers_released", lambda timeout: False)
    p = object.__new__(paste.Paster)
    p._pending_snapshot = None
    p._worker = SimpleNamespace(pending=lambda: 0)
    assert p._paste(123, "kept dictation") is False
    assert io[0].value == "kept dictation" and io[1] == []


def test_copy_timeout_does_not_touch_clipboard(io, monkeypatch):
    monkeypatch.setattr(winapi, "wait_for_modifiers_released", lambda timeout: False)
    reader = object.__new__(selection.SelectionReader)
    assert reader._capture("ctrl+alt", 123) == ("", selection.SRC_INPUT_BUSY)
    assert io[0].value == "original" and io[1] == []


def test_copy_failure_restores_clipboard(io, monkeypatch):
    def broken():
        raise OSError("simulated copy failure")
    monkeypatch.setattr(winapi, "send_ctrl_c", broken)
    reader = object.__new__(selection.SelectionReader)
    with pytest.raises(OSError):
        reader._capture("ctrl+alt", 123)
    assert io[0].value == "original"


def test_paste_exception_still_notifies_caller(io, monkeypatch):
    p = object.__new__(paste.Paster)
    def broken(*args):
        raise OSError("simulated failure")
    monkeypatch.setattr(p, "_paste", broken)
    got = []
    p._job(123, "kept words", lambda ok, text: got.append((ok, text)))
    assert got == [(False, "kept words")]


def test_copy_and_paste_share_one_transaction_lock(io):
    entered, release, copied = threading.Event(), threading.Event(), threading.Event()
    p = object.__new__(paste.Paster)
    reader = object.__new__(selection.SelectionReader)
    def hold(*args):
        entered.set()
        assert release.wait(2)
        return True
    p._paste_locked = hold
    reader._capture_clipboard = lambda *args: copied.set() or ("", "empty")
    one = threading.Thread(target=lambda: p._paste(123, "test"))
    two = threading.Thread(target=lambda: reader._capture("ctrl+alt", 123))
    one.start()
    assert entered.wait(2)
    two.start()
    try:
        assert not copied.wait(0.1)
    finally:
        release.set()
        one.join(2)
        two.join(2)
    assert copied.is_set()


def test_native_wait_reads_current_state_and_stops_at_timeout(monkeypatch):
    states = iter([(162,), (162,), ()])
    monkeypatch.setattr(winapi, "modifiers_down", lambda: next(states))
    assert winapi.wait_for_modifiers_released(0.1) is True
    monkeypatch.setattr(winapi, "modifiers_down", lambda: (162,))
    assert winapi.wait_for_modifiers_released(0) is False


def test_callback_exception_preserves_release_pairing():
    def broken():
        raise RuntimeError("disposed Qt receiver")
    hook = ReadHotkey("ctrl+m", broken, Keys)
    assert hook(event("down", 29)) is True
    assert hook(event("down", 50)) is False
    assert hook(event("up", 50)) is False
    assert hook(event("up", 29)) is True
    assert hook.last_error == "disposed Qt receiver"
    assert not hook._held and not hook._blocked
