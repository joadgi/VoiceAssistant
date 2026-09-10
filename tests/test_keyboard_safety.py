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


# --------------------------------------------------------------------------- #
# Stale cached key state (the half of the Ctrl problem the first fix missed).
#
# A hook only knows the events it is delivered, so ReadHotkey's `_held` set is
# not independent evidence that a key is physically down. `--report` confirms
# Windows really does drop key-ups on this machine, and CLAUDE.md documents
# both causes (hook dropped past LowLevelHooksTimeout; UAC/secure-desktop
# switches eating events).
#
# These model PHYSICAL state separately from DELIVERED events -- a lost key-up
# means the release happened but no event arrived. A harness that conflates the
# two cannot express the bug at all.
# --------------------------------------------------------------------------- #
CTRL, ALT, SHIFT, M = 29, 56, 42, 50
_MOD_CODES = {CTRL, 285, ALT, 312, SHIFT, 54, 91, 92}


class World:
    """Windows' keyboard state plus a ReadHotkey wired to probe it."""

    def __init__(self, combo):
        self.physical = set()
        self.fired = []
        self.hook = ReadHotkey(combo, lambda: self.fired.append(1) or True,
                               Keys, released_probe=self._probe)

    def _probe(self, codes):
        # Mirrors winapi.released_modifier_scan_codes: modifiers only, since
        # a consumed key never reaches Windows' accepted state.
        return {c for c in codes if c in _MOD_CODES and c not in self.physical}

    def press(self, code, deliver=True):
        self.physical.add(code)
        return self._edge("down", code, deliver)

    def release(self, code, deliver=True):
        self.physical.discard(code)
        return self._edge("up", code, deliver)

    def _edge(self, direction, code, deliver):
        if not deliver:
            return None
        return self.hook(event(direction, code))


def test_lost_modifier_keyup_does_not_leave_a_phantom_chord():
    """The shipped ctrl+alt chord: a dropped Ctrl key-up must not turn every
    later Alt press (Alt+Tab!) into a read-aloud trigger."""
    world = World("ctrl+alt")
    world.press(CTRL)
    assert world.press(ALT) is True          # genuine read
    world.release(ALT)
    world.release(CTRL, deliver=False)       # physically up, event dropped

    world.press(ALT)                         # user hits Alt+Tab
    world.release(ALT)
    assert world.fired == [1], "Alt alone fired read-aloud"
    assert not world.hook._held


def test_lost_modifier_keyup_does_not_swallow_an_ordinary_keystroke():
    """With a normal trigger, the stale-modifier bug ALSO ate the keystroke:
    a plain M fired read-aloud and never reached the document."""
    world = World("ctrl+m")
    world.press(CTRL)
    assert world.press(M) is False           # genuine trigger, consumed
    world.release(M)
    world.release(CTRL, deliver=False)       # event dropped

    assert world.press(M) is True, "ordinary M was swallowed"
    assert world.release(M) is True
    assert world.fired == [1], "ordinary M fired read-aloud"


def test_every_combo_key_stuck_does_not_kill_read_aloud_permanently():
    """If all combo keys latch, `active` can never go false, so `_latched`
    never resets and the action can never fire again -- silently dead until
    restart. Reconciliation must recover on the next genuine press."""
    world = World("ctrl+alt")
    world.press(CTRL)
    world.press(ALT)
    assert world.fired == [1]
    world.release(CTRL, deliver=False)
    world.release(ALT, deliver=False)

    world.press(CTRL)
    world.press(ALT)
    assert world.fired == [1, 1], "read-aloud went dead after lost key-ups"


def test_reconciliation_never_checks_the_current_events_own_key():
    """A low-level hook runs BEFORE the event reaches the rest of the system,
    so Windows' accepted state can still read "up" for the key being
    delivered. Checking it would drop that key and break every chord."""
    seen = []

    def probe(codes):
        seen.append(set(codes))
        return set(codes)        # claim everything is released

    hook = ReadHotkey("ctrl+alt", lambda: None, Keys, released_probe=probe)
    hook(event("down", CTRL))
    # Nothing else was held, so after exempting Ctrl there is nothing to
    # check and the probe is skipped outright -- the fast path.
    assert seen == [], "the current key must be exempt from reconciliation"

    hook(event("down", ALT))
    assert seen == [{CTRL}], "only the OTHER held key may be probed"
    # Ctrl was claimed released, so the chord must not have fired.
    assert hook._held == {ALT}


def test_reconciliation_keeps_a_consumed_down_up_pair_matched():
    """Dropping a modifier mid-hold must not leak an unmatched key-up into the
    target window: pairing is driven by `_blocked`, which stays untouched."""
    world = World("ctrl+m")
    world.press(CTRL)
    assert world.press(M) is False
    world.release(CTRL, deliver=False)       # reconciled away before the M up
    assert world.release(M) is False, "consumed M leaked an unmatched key-up"


def test_probe_failure_falls_back_to_cached_state_without_breaking_the_hook():
    """A probe error must never propagate: an exception mid-handler would
    leak a down event whose key-up is consumed."""
    def boom(codes):
        raise OSError("GetAsyncKeyState unavailable")

    fired = []
    hook = ReadHotkey("ctrl+alt", lambda: fired.append(1), Keys,
                      released_probe=boom)
    assert hook(event("down", CTRL)) is True
    assert hook(event("down", ALT)) is True
    assert fired == [1], "must still work on cached state"
    assert "GetAsyncKeyState unavailable" in hook.last_error


def test_matcher_without_a_probe_keeps_its_previous_behaviour():
    """The probe is optional, so the matcher stays hermetically testable."""
    fired = []
    hook = ReadHotkey("ctrl+alt", lambda: fired.append(1), Keys)
    assert hook(event("down", CTRL)) is True
    assert hook(event("down", ALT)) is True
    assert fired == [1]


def test_released_probe_reports_modifiers_only(monkeypatch):
    """winapi's probe must ignore non-modifiers, because a key this app
    consumed never reaches Windows' accepted state. It must also reject codes
    whose MapVirtualKeyW result is not a real modifier VK -- measured, ctrl's
    57629 maps to VK_PAUSE, the Windows key's 91/92 to 0xF1/0xEA."""
    vk_for = {CTRL: 0xA2, ALT: 0xA4, M: 0x4D, 57629: 0x13, 91: 0xF1}
    monkeypatch.setattr(winapi.user32, "MapVirtualKeyW",
                        lambda code, kind: vk_for.get(code, 0))
    monkeypatch.setattr(winapi.user32, "GetAsyncKeyState", lambda vk: 0)

    released = winapi.released_modifier_scan_codes({CTRL, ALT, M, 57629, 91})
    assert released == {CTRL, ALT}, released


def test_released_probe_fails_open_on_error(monkeypatch):
    def boom(code, kind):
        raise OSError("no user32")

    monkeypatch.setattr(winapi.user32, "MapVirtualKeyW", boom)
    assert winapi.released_modifier_scan_codes({CTRL}) == set()


# --------------------------------------------------------------------------- #
# The shipped configuration: dictation on SUPPRESSED `caps lock`, read-aloud on
# `ctrl+alt`. ReadHotkey is a GLOBAL blocking hook, so it sees Caps Lock too.
# --------------------------------------------------------------------------- #
CAPS = 58


def test_reconciliation_never_judges_a_suppressed_key(monkeypatch):
    """Caps Lock is suppressed, so it never reaches Windows' accepted state.
    Reconciling it would call a physically-held key released. The boundary
    holds because VK_CAPITAL is not a modifier VK -- assert that, so a future
    widening of _MODIFIER_VKS cannot silently break it."""
    vk = winapi.user32.MapVirtualKeyW(CAPS, 3)
    assert vk == 0x14, vk                     # VK_CAPITAL
    assert vk not in winapi._MODIFIER_VKS
    monkeypatch.setattr(winapi.user32, "GetAsyncKeyState", lambda v: 0)
    assert winapi.released_modifier_scan_codes({CAPS}) == set()


def test_caps_lock_dictation_passes_through_the_read_hook():
    """A dictation hold must not fire read-aloud, and the read hook must never
    consume the Caps Lock event -- the dictation hook owns that key."""
    world = World("ctrl+alt")
    for direction in ("down", "down", "down", "up"):   # hold with autorepeat
        assert world._edge(direction, CAPS, True) is True
    assert world.fired == []


def test_read_chord_still_works_while_caps_lock_is_held():
    """Dictating and reading are independent; holding one must not block the
    other."""
    world = World("ctrl+alt")
    world.press(CAPS)
    world.press(CTRL)
    world.press(ALT)
    assert world.fired == [1]
    assert world.release(CAPS) is True
