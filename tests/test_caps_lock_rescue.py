"""Caps Lock must never be left stuck on.

Binding Caps Lock as push-to-talk means the app SWALLOWS the key, so Windows
never sees it — which also means the user can no longer press it to turn caps
off. If caps is on for any reason the app did not cause (it was pressed while
the app was not running, a crash, or a restart between the key's down and up),
the user is stuck in capitals with no way out but quitting the app. That
happened on Josh's machine during development, which is why these exist.

Three guarantees:
  1. Startup clears a stuck Caps Lock, BEFORE the hooks go back on — otherwise
     the app swallows its own fix.
  2. There is an escape hatch while it is running, on both menus, and it uses
     `keyboard.send` because that marks its events as replayed so our
     suppressing hook passes them through.
  3. Quitting clears it too, after the hooks are removed.
  4. It rescues ITSELF while running - on the lost keyup that strands it, and
     on a periodic check for the strandings no keyup event covers. Guarantee 1
     only ever fires at startup, which is no help to a process that stays up
     for days (measured 2026-09-19: stuck for three of them).
  5. Closing the window says, once, that the app is still running and where
     Quit is - a user who believes they quit cannot know what is still eating
     their Caps Lock.

Num Lock and Scroll Lock are deliberately NOT cleared: Num Lock off breaks the
numeric keypad, and Scroll Lock is harmless.
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from voiceassistant import winapi  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class TestClearCapsLock:
    def test_taps_the_key_only_when_caps_is_on(self, monkeypatch):
        taps = []
        monkeypatch.setattr(winapi, "lock_key_is_on", lambda vk=winapi.VK_CAPITAL: True)
        monkeypatch.setattr(winapi.user32, "keybd_event",
                            lambda vk, scan, flags, extra: taps.append((vk, flags)))
        assert winapi.clear_caps_lock() is True
        # A press AND a release: a held-down virtual key would be far worse
        # than the problem it fixes.
        assert [vk for vk, _ in taps] == [winapi.VK_CAPITAL, winapi.VK_CAPITAL]
        assert taps[0][1] == 0 and taps[1][1] == winapi.KEYEVENTF_KEYUP

    def test_does_nothing_when_caps_is_already_off(self, monkeypatch):
        taps = []
        monkeypatch.setattr(winapi, "lock_key_is_on", lambda vk=winapi.VK_CAPITAL: False)
        monkeypatch.setattr(winapi.user32, "keybd_event",
                            lambda *a: taps.append(a))
        assert winapi.clear_caps_lock() is False
        assert taps == [], "toggled caps ON for a user who did not have it on"

    def test_never_raises(self, monkeypatch):
        def boom(*a):
            raise OSError("nope")

        monkeypatch.setattr(winapi, "lock_key_is_on", lambda vk=winapi.VK_CAPITAL: True)
        monkeypatch.setattr(winapi.user32, "keybd_event", boom)
        assert winapi.clear_caps_lock() is False


@pytest.fixture
def mw(qapp, monkeypatch):
    import voiceassistant.config as cfg
    import voiceassistant.ocr as ocr
    import voiceassistant.recorder as rec_mod
    import voiceassistant.transcriber as trm
    import voiceassistant.tts as tts
    import voiceassistant.window as win_mod
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
    w = MainWindow(entry_script="main.py")
    yield w, win_mod
    for closer in (w._show_request_timer.stop, w.live_preview.shutdown,
                   w.tts.shutdown, w.paster.shutdown,
                   w.transcriber._worker.shutdown, w._selection_reader.shutdown):
        try:
            closer()
        except Exception:
            pass


class TestEscapeHatch:
    def test_menu_action_uses_keyboard_send_not_raw_injection(self, mw, monkeypatch):
        """A raw SendInput tap would be eaten by our own suppressing hook.

        `keyboard.send` marks its events as replayed, so the hook passes them
        through — that is the only reason the escape hatch works at all while
        the app is running.
        """
        w, win_mod = mw
        sent = []
        monkeypatch.setattr(winapi, "lock_key_is_on", lambda vk=winapi.VK_CAPITAL: True)
        monkeypatch.setattr(win_mod.kb, "send", lambda key: sent.append(key))
        raw = []
        monkeypatch.setattr(winapi, "clear_caps_lock", lambda: raw.append(1))
        w._on_clear_caps()
        assert sent == ["caps lock"]
        assert raw == [], "used raw injection, which our own hook would swallow"
        assert "off" in w.status_bar.currentMessage().lower()

    def test_menu_action_is_a_no_op_when_caps_is_off(self, mw, monkeypatch):
        w, win_mod = mw
        sent = []
        monkeypatch.setattr(winapi, "lock_key_is_on", lambda vk=winapi.VK_CAPITAL: False)
        monkeypatch.setattr(win_mod.kb, "send", lambda key: sent.append(key))
        w._on_clear_caps()
        assert sent == [], "turned caps ON for a user who did not have it on"

    def test_menu_action_survives_a_failure(self, mw, monkeypatch):
        w, win_mod = mw

        def boom(key):
            raise OSError("nope")

        monkeypatch.setattr(winapi, "lock_key_is_on", lambda vk=winapi.VK_CAPITAL: True)
        monkeypatch.setattr(win_mod.kb, "send", boom)
        w._on_clear_caps()  # must not raise out into the GUI thread

    def test_the_escape_hatch_is_reachable_from_the_pill(self, mw):
        w, _ = mw
        labels = [a.text() for a in w._build_pill_menu().actions()]
        assert any("caps lock" in t.lower() for t in labels), (
            "no way to clear caps lock from the pill — the app is tray-first, "
            "so a user with caps stuck has to quit it")


class TestStartupClear:
    def test_startup_clears_caps_before_hooks_are_registered(self, qapp, monkeypatch):
        """Order matters: clearing AFTER the hooks go on means the app swallows
        its own fix, because the hook suppresses every Caps Lock event."""
        import voiceassistant.config as cfg
        import voiceassistant.ocr as ocr
        import voiceassistant.recorder as rec_mod
        import voiceassistant.transcriber as trm
        import voiceassistant.tts as tts
        import voiceassistant.window as win_mod
        from voiceassistant.window import MainWindow

        monkeypatch.setattr(cfg, "CONFIG_FILE",
                            os.path.join(tempfile.mkdtemp(), "settings.json"))
        monkeypatch.setattr(trm.Transcriber, "load_model", lambda self: None)
        monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
        monkeypatch.setattr(tts.TTSEngine, "_load_kokoro", lambda self: None)
        monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
        monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)
        monkeypatch.setattr(rec_mod.VoiceRecorder, "open_stream", lambda self: True)

        order = []
        monkeypatch.setattr(winapi, "clear_caps_lock",
                            lambda: order.append("clear") or True)
        monkeypatch.setattr(win_mod.kb, "unhook_all", lambda: None)
        monkeypatch.setattr(win_mod.kb, "on_press_key",
                            lambda *a, **k: order.append("hook"))
        monkeypatch.setattr(win_mod.kb, "on_release_key", lambda *a, **k: None)
        monkeypatch.setattr(win_mod.kb, "hook", lambda *a, **k: None)

        w = MainWindow(entry_script="main.py")
        try:
            w.config.set("hotkey_record", "caps lock")
            order.clear()
            w._setup_hotkeys()
            assert "clear" in order, "startup never clears a stuck caps lock"
            assert order.index("clear") < order.index("hook"), (
                "cleared caps AFTER hooking the key — the hook swallows the fix")
        finally:
            for closer in (w._show_request_timer.stop, w.live_preview.shutdown,
                           w.tts.shutdown, w.paster.shutdown,
                           w.transcriber._worker.shutdown,
                           w._selection_reader.shutdown):
                try:
                    closer()
                except Exception:
                    pass

    def test_a_non_caps_binding_does_not_touch_caps(self, qapp, monkeypatch, mw):
        """Scroll Lock and F-keys have no business toggling the user's caps."""
        w, win_mod = mw
        calls = []
        monkeypatch.setattr(winapi, "clear_caps_lock", lambda: calls.append(1))
        monkeypatch.setattr(win_mod.kb, "unhook_all", lambda: None)
        monkeypatch.setattr(win_mod.kb, "on_press_key", lambda *a, **k: None)
        monkeypatch.setattr(win_mod.kb, "on_release_key", lambda *a, **k: None)
        monkeypatch.setattr(win_mod.kb, "hook", lambda *a, **k: None)
        from voiceassistant.window import MainWindow

        for combo in ("scroll lock", "f9", "ctrl+shift+r"):
            w.config.set("hotkey_record", combo)
            MainWindow._setup_hotkeys(w)
        assert calls == [], "cleared caps lock for a binding that never swallows it"


class TestWindowAppName:
    """`get_window_app` feeds the metrics' "which app" attribution."""

    def test_invalid_handles_return_empty_never_raise(self):
        assert winapi.get_window_app(0) == ""
        assert winapi.get_window_app(None) == ""
        assert winapi.get_window_app(999999999) == ""

    def test_a_real_window_resolves_to_an_executable_name(self):
        hwnd = winapi.get_foreground_window()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        name = winapi.get_window_app(hwnd)
        assert isinstance(name, str)
        # Either an exe or the window-class fallback; never a window TITLE,
        # which would carry document names and URLs into the metrics file.
        assert "\n" not in name and len(name) < 80

    def test_falls_back_to_the_window_class_when_the_process_is_opaque(self, monkeypatch):
        monkeypatch.setattr(winapi.kernel32, "OpenProcess", lambda *a: 0)
        monkeypatch.setattr(winapi, "get_window_class", lambda hwnd: "ConsoleWindowClass")
        assert winapi.get_window_app(1234) == "ConsoleWindowClass"

    def test_survives_a_failing_win32_call(self, monkeypatch):
        def boom(*a):
            raise OSError("nope")

        monkeypatch.setattr(winapi.user32, "GetWindowThreadProcessId", boom)
        monkeypatch.setattr(winapi, "get_window_class", lambda hwnd: "")
        assert winapi.get_window_app(1234) == ""


class TestMutedMicrophoneIsNamed:
    """"No sound detected" is the same message for "you spoke quietly" and
    "Windows has your mic muted", and it sends you to the gain knob either way.
    That cost three hours on 2026-09-12. Sample data cannot tell those apart;
    the endpoint's mute flag can."""

    def test_mute_state_returns_a_pair_and_never_raises(self):
        muted, level = winapi.capture_device_mute_state()
        assert muted in (True, False, None)
        assert level is None or 0.0 <= level <= 1.0

    def test_an_unreadable_state_is_unknown_not_false(self, monkeypatch):
        """An unknown must never be reported to the user as "not muted"."""
        # No interpreter to run the probe with.
        monkeypatch.setattr(winapi, "_probe_interpreter", lambda: "")
        assert winapi.capture_device_mute_state() == (None, None)

    def test_a_probe_that_says_nothing_is_unknown(self, monkeypatch):
        """A probe that crashes or prints junk must not be read as a state."""
        import subprocess

        class _Result:
            stdout = ""
            stderr = "Traceback: the audio service is not running"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        assert winapi.capture_device_mute_state() == (None, None)

    def test_the_probe_runs_out_of_process(self):
        """COM is apartment-threaded; running this in-process segfaulted the
        suite right after the UI Automation tests. Keep it isolated."""
        import inspect

        src = inspect.getsource(winapi.capture_device_mute_state)
        assert "subprocess" in src, (
            "the mute probe is back in-process -- that segfaulted the suite")
        assert "timeout" in src, "an isolated probe must still be bounded"

    def test_a_muted_mic_is_named_on_the_pill_and_in_the_status(self, mw, monkeypatch):
        import numpy as np

        from voiceassistant import metrics

        w, win_mod = mw
        monkeypatch.setattr(win_mod.winapi, "capture_device_mute_state",
                            lambda: (True, 0.6))
        w._pending_target_hwnd = 4242
        silence = np.zeros(16000, dtype=np.float32)
        w._on_recording_stopped(silence)
        assert "MUTED" in w.indicator._label.text()
        assert "muted" in w.status_bar.currentMessage().lower()
        row = metrics.load()[-1]
        assert row["outcome"] == metrics.OUTCOME_DROPPED_QUIET
        assert row["mic_muted"] is True

    def test_a_zeroed_input_level_is_named_too(self, mw, monkeypatch):
        import numpy as np

        w, win_mod = mw
        monkeypatch.setattr(win_mod.winapi, "capture_device_mute_state",
                            lambda: (False, 0.0))
        w._pending_target_hwnd = 4242
        w._on_recording_stopped(np.zeros(16000, dtype=np.float32))
        assert "zero" in w.indicator._label.text().lower()
        assert "level" in w.status_bar.currentMessage().lower()

    def test_an_unmuted_quiet_mic_keeps_the_old_message(self, mw, monkeypatch):
        """Don't cry mute when the user simply spoke too softly."""
        import numpy as np

        w, win_mod = mw
        monkeypatch.setattr(win_mod.winapi, "capture_device_mute_state",
                            lambda: (False, 0.75))
        w._pending_target_hwnd = 4242
        w._on_recording_stopped(np.zeros(16000, dtype=np.float32))
        assert "no sound" in w.indicator._label.text().lower()
        assert "muted" not in w.status_bar.currentMessage().lower().replace("unmuted", "")

    def test_an_unknown_state_keeps_the_old_message(self, mw, monkeypatch):
        import numpy as np

        w, win_mod = mw
        monkeypatch.setattr(win_mod.winapi, "capture_device_mute_state",
                            lambda: (None, None))
        w._pending_target_hwnd = 4242
        w._on_recording_stopped(np.zeros(16000, dtype=np.float32))
        assert "no sound" in w.indicator._label.text().lower()


# ===========================================================================
# Guarantee 4 - the stuck key fixes itself
# ===========================================================================
class _RescueRig:
    """Put the window in the one state a user cannot escape from: Caps Lock
    bound, actually swallowed, and currently on."""

    @staticmethod
    def arm(w, win_mod, monkeypatch, caps_on=True, suppressed=True,
            binding="caps lock"):
        w.config.set("hotkey_record", binding)
        w._record_suppression_active = suppressed
        w._ptt_active = False
        w._caps_rescue_at = 0.0
        monkeypatch.setattr(winapi, "lock_key_is_on",
                            lambda vk=winapi.VK_CAPITAL: caps_on)
        sent = []
        monkeypatch.setattr(win_mod.kb, "send", lambda key: sent.append(key))
        return sent


class TestAutomaticRescue:
    def test_it_clears_a_stranded_caps_lock(self, mw, monkeypatch):
        w, win_mod = mw
        sent = _RescueRig.arm(w, win_mod, monkeypatch)
        assert w._rescue_stuck_caps("test") is True
        assert sent == ["caps lock"]

    def test_it_uses_keyboard_send_not_raw_injection(self, mw, monkeypatch):
        """Same reason as the menu hatch: a raw winapi tap is eaten by our own
        suppressing hook, so the automatic fix would silently do nothing."""
        w, win_mod = mw
        _RescueRig.arm(w, win_mod, monkeypatch)
        raw = []
        monkeypatch.setattr(winapi, "clear_caps_lock", lambda: raw.append(1))
        w._rescue_stuck_caps("test")
        assert raw == [], "used raw injection, which our own hook would swallow"

    def test_it_does_nothing_when_caps_is_already_off(self, mw, monkeypatch):
        w, win_mod = mw
        sent = _RescueRig.arm(w, win_mod, monkeypatch, caps_on=False)
        assert w._rescue_stuck_caps("test") is False
        assert sent == [], "turned caps ON for a user who did not have it on"

    def test_it_never_fights_a_key_the_user_can_fix_themselves(self, mw, monkeypatch):
        """Suppression can be refused at registration. A Caps Lock we do NOT
        swallow is one the user can toggle deliberately - turning it off under
        them every few seconds would be far worse than the bug."""
        w, win_mod = mw
        sent = _RescueRig.arm(w, win_mod, monkeypatch, suppressed=False)
        assert w._rescue_stuck_caps("test") is False
        assert sent == []

    def test_it_never_interrupts_a_live_hold(self, mw, monkeypatch):
        w, win_mod = mw
        sent = _RescueRig.arm(w, win_mod, monkeypatch)
        w._ptt_active = True
        assert w._rescue_stuck_caps("test") is False
        assert sent == []

    def test_it_leaves_a_non_caps_binding_alone(self, mw, monkeypatch):
        """Scroll Lock has no business toggling anyone's caps."""
        w, win_mod = mw
        sent = _RescueRig.arm(w, win_mod, monkeypatch, binding="scroll lock")
        assert w._rescue_stuck_caps("test") is False
        assert sent == []

    def test_it_backs_off_instead_of_hammering_the_key(self, mw, monkeypatch):
        """The background check runs twice a second. If a clear ever failed to
        take, an ungated rescue would inject Caps Lock forever."""
        w, win_mod = mw
        sent = _RescueRig.arm(w, win_mod, monkeypatch)
        assert w._rescue_stuck_caps("first") is True
        assert w._rescue_stuck_caps("immediately after") is False
        assert sent == ["caps lock"]

    def test_it_survives_a_failure(self, mw, monkeypatch):
        w, win_mod = mw
        _RescueRig.arm(w, win_mod, monkeypatch)

        def boom(key):
            raise OSError("nope")

        monkeypatch.setattr(win_mod.kb, "send", boom)
        assert w._rescue_stuck_caps("test") is False   # must not raise


class TestRescueIsActuallyWired:
    def test_a_lost_keyup_triggers_it(self, mw, monkeypatch):
        """The watchdog already detected the exact event that strands caps -
        our hook missing the release - and used to only stop the recording."""
        w, win_mod = mw
        calls = []
        monkeypatch.setattr(w, "_rescue_stuck_caps", lambda reason: calls.append(reason))
        w.config.set("hotkey_record", "caps lock")
        w._ptt_active = True
        monkeypatch.setattr(win_mod.kb, "is_pressed", lambda part: False)
        w._on_ptt_watchdog()
        assert calls, "a lost keyup no longer rescues a stranded caps lock"

    def test_the_background_check_triggers_it(self, mw, monkeypatch):
        """Caps also strands in the unhooked gap during hotkey
        re-registration, which no keyup event covers."""
        w, win_mod = mw
        calls = []
        monkeypatch.setattr(w, "_rescue_stuck_caps", lambda reason: calls.append(reason))
        monkeypatch.setattr(winapi, "show_requested", lambda: False)
        w._poll_show_request()
        assert calls, "nothing checks for a stranded caps lock while running"


# ===========================================================================
# Guarantee 5 - closing the window admits the app is still running
# ===========================================================================
class _BalloonTray:
    def __init__(self):
        self.messages = []

    def isVisible(self):
        return True

    def showMessage(self, title, body, icon, msecs):
        self.messages.append((title, body))


class TestCloseToTrayNotice:
    def test_the_first_close_says_it_is_still_running(self, mw):
        w, _ = mw
        w.tray = _BalloonTray()
        w.config.set("close_to_tray_notified", False)
        w._notify_still_running()
        assert len(w.tray.messages) == 1
        title, body = w.tray.messages[0]
        assert "still running" in title.lower()
        assert "quit" in body.lower(), "never says where Quit actually is"
        assert "caps lock" in body.lower(), (
            "never says the key is still bound - which is the thing that "
            "strands the user after they think they closed the app")

    def test_it_only_says_it_once(self, mw):
        w, _ = mw
        w.tray = _BalloonTray()
        w.config.set("close_to_tray_notified", False)
        w._notify_still_running()
        w._notify_still_running()
        assert len(w.tray.messages) == 1

    def test_a_failed_balloon_is_not_recorded_as_shown(self, mw):
        """Otherwise the one notice that matters is silently consumed."""
        w, _ = mw

        class _Broken:
            def showMessage(self, *a):
                raise OSError("no tray")

        w.tray = _Broken()
        w.config.set("close_to_tray_notified", False)
        w._notify_still_running()                      # must not raise
        assert w.config.get("close_to_tray_notified") is False

    def test_closing_the_window_actually_shows_it(self, mw, monkeypatch):
        w, _ = mw
        w.tray = _BalloonTray()
        w._force_quit = False
        w.config.set("close_to_tray_notified", False)
        monkeypatch.setattr(w, "hide", lambda: None)

        class _Ev:
            def ignore(self):
                pass

            def accept(self):
                pass

        w.closeEvent(_Ev())        # close-to-tray returns before any teardown
        assert w.tray.messages, "closing to tray told the user nothing visible"


class TestTrayIconIsIdentifiable:
    def test_it_is_painted_not_a_stock_glyph(self, qapp):
        """SP_ComputerIcon is a generic grey monitor - indistinguishable in the
        Windows 11 overflow flyout, which is the ONLY route to Quit."""
        from voiceassistant.window import _make_tray_icon

        icon = _make_tray_icon()
        assert not icon.isNull()
        assert icon.availableSizes(), "tray icon carries no pixmap"
