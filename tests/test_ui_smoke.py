"""Headless UI construction smoke tests.

Qt can't run in a normal CI display, so we use the offscreen platform and
neutralize MainWindow's heavy/side-effecting startup (model loads, global
hotkey registration, tray, the start-with-Windows registry write). What this
DOES exercise is the part that breaks on a UI refactor: `_build_ui` widget
creation, `_connect_signals` wiring (every referenced widget/slot must exist),
SettingsDialog structure, and the pill's state methods.

Skips cleanly if PySide6 can't start offscreen.
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def main_window(qapp, monkeypatch):
    """Construct a real MainWindow with side effects neutralized."""
    import voiceassistant.config as cfg
    import voiceassistant.transcriber as tr
    import voiceassistant.ocr as ocr
    import voiceassistant.tts as tts
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    monkeypatch.setattr(cfg, "CONFIG_FILE",
                        os.path.join(tempfile.mkdtemp(), "settings.json"))
    monkeypatch.setattr(tr.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(tts.TTSEngine, "_load_kokoro", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)

    w = MainWindow(entry_script="main.py")
    yield w
    # Tear down owned workers/timers so threads don't linger across tests.
    try:
        w._show_request_timer.stop()
        w.tts.shutdown()
        w.paster.shutdown()
    except Exception:
        pass


def test_mainwindow_constructs(main_window):
    w = main_window
    assert w.centralWidget() is not None
    # Core surfaces exist.
    for attr in ("indicator", "text_output", "btn_record", "btn_settings",
                 "level_bar", "label_model_status", "voice_combo", "speed_slider"):
        assert hasattr(w, attr), f"missing widget: {attr}"


def test_speed_range_matches_selected_voice_backend(main_window):
    """Local neural exposes only rates it actually produces; online keeps 3x."""
    w = main_window
    assert w.voice_combo.currentData().startswith("kokoro:")
    assert w.speed_slider.maximum() == 260

    online_index = w.voice_combo.findData("en-US-AndrewNeural")
    assert online_index >= 0
    w.voice_combo.setCurrentIndex(online_index)
    assert w.speed_slider.maximum() == 300

    local_index = w.voice_combo.findData("kokoro:am_michael")
    w.speed_slider.setValue(300)
    w.voice_combo.setCurrentIndex(local_index)
    assert w.speed_slider.maximum() == 260
    assert w.speed_slider.value() == 260


def test_local_playback_controls_show_and_enforce_fixed_read_speed(main_window):
    """The selected speed is real, visible, and cannot change mid-read."""
    w = main_window
    local_index = w.voice_combo.findData("kokoro:am_michael")
    w.voice_combo.setCurrentIndex(local_index)
    w.speed_slider.setValue(198)

    assert w.tts._speed == pytest.approx(1.98)
    assert w.speed_label.text() == "1.98x"
    assert w.speed_apply_label.text() == "applies to next read"

    w._on_tts_started()
    assert not w.voice_combo.isEnabled()
    assert not w.speed_slider.isEnabled()
    assert w.speed_apply_label.text() == "locked at 1.98x for this read"

    w._on_tts_finished()
    assert w.voice_combo.isEnabled()
    assert w.speed_slider.isEnabled()
    assert w.speed_apply_label.text() == "applies to next read"


def test_pill_states_do_not_crash(main_window):
    ind = main_window.indicator
    for method in ("show_recording", "show_transcribing", "show_pasting",
                   "show_done", "show_error", "show_idle"):
        getattr(ind, method)()


def test_settings_dialog_constructs_and_reports(qapp, main_window):
    from PySide6.QtWidgets import QTabWidget
    from voiceassistant.settings_dialog import SettingsDialog

    dlg = SettingsDialog(main_window.config, main_window.tts)
    vals = dlg.get_values()
    # get_values must return every key the window's _on_settings applies.
    for key in ("audio_device", "whisper_model", "whisper_language",
                "font_size", "always_on_top", "start_with_windows",
                "start_minimized", "light_cleanup", "debug_logging"):
        assert key in vals, f"settings get_values missing: {key}"
    # Tabbed layout: at least the two organizing tabs exist.
    tabs = dlg.findChild(QTabWidget)
    assert tabs is not None and tabs.count() >= 2


def test_pill_menu_has_expected_actions(main_window):
    menu = main_window._build_pill_menu()
    labels = [a.text() for a in menu.actions() if a.text()]
    # Tray-first: the pill menu must expose the core controls.
    assert any("Dictate" in x or "Stop recording" in x for x in labels)
    assert any("Read selection" in x for x in labels)
    assert any("Settings" in x for x in labels)
    assert any("Quit" in x for x in labels)


def test_quit_action_closes_window_and_stops_qt(monkeypatch):
    """Tray mode disables quit-on-last-window-closed, so the explicit Quit
    action must do both halves: close for teardown, then stop QApplication."""
    import voiceassistant.window as window_module

    calls = []

    class FakeApp:
        def quit(self):
            calls.append("app.quit")

    class FakeApplication:
        @staticmethod
        def instance():
            return FakeApp()

    class FakeWindow:
        _force_quit = False

        def close(self):
            calls.append("window.close")

    monkeypatch.setattr(window_module, "QApplication", FakeApplication)
    fake_window = FakeWindow()
    window_module.MainWindow.quit_app(fake_window)

    assert fake_window._force_quit is True
    assert calls == ["window.close", "app.quit"]


def test_stopped_selection_capture_cannot_start_speaking_later(
        main_window, monkeypatch):
    """A selection grab finishes asynchronously. If Stop lands first, its late
    callback must not resurrect the canceled read or cross into a newer one."""
    from voiceassistant import winapi

    callbacks = []
    spoken = []
    monkeypatch.setattr(winapi, "get_foreground_window", lambda: 1234)
    monkeypatch.setattr(
        main_window._selection_reader,
        "capture",
        lambda combo, hwnd, done_cb: callbacks.append(done_cb),
    )
    monkeypatch.setattr(main_window.tts, "speak", spoken.append)

    main_window._on_read_aloud_toggle()
    assert main_window._read_in_flight is True
    assert len(callbacks) == 1

    main_window._on_read_aloud_toggle()  # Stop while selection capture is pending.
    callbacks[0]("stale selection", "uia")
    assert spoken == [], "stopped capture started speaking after Stop"

    main_window._on_read_aloud_toggle()
    callbacks[1]("current selection", "uia")
    assert spoken == ["current selection"]


def test_empty_selection_never_reads_the_screen_aloud(main_window, monkeypatch):
    """The 2026-09-16 bug. An empty capture used to OCR a box around the MOUSE
    POINTER and speak it: 13 of the user's last 18 read presses ended that way,
    the last reciting 511 characters of screen furniture for 24.5 seconds.

    A failed read must now stay SILENT and say what happened. Reading the
    screen is still available — deliberately, on the OCR hotkey — and the
    message has to name that key, because it is the only way out the user has.
    """
    from voiceassistant import winapi

    spoken, captured = [], []
    monkeypatch.setattr(winapi, "get_foreground_window", lambda: 1234)
    monkeypatch.setattr(winapi, "get_window_app", lambda hwnd: "acrobat.exe")
    monkeypatch.setattr(main_window.tts, "speak", spoken.append)
    monkeypatch.setattr(main_window.ocr, "read_image",
                        lambda img: captured.append(img))
    # OCR must look READY, or the old fallback would bail on `is_loaded` and
    # this test would pass against the very code it exists to reject.
    monkeypatch.setattr(type(main_window.ocr), "is_loaded", property(lambda self: True))
    monkeypatch.setattr(
        main_window.screen_capture, "capture_around_cursor",
        lambda **kw: pytest.fail("read-aloud grabbed the screen on an empty "
                                 "selection"),
    )
    main_window.config.set("hotkey_screen_read", "ctrl+shift+s")

    for source in ("empty", "console_blocked", "refocus_failed", "input_busy"):
        main_window._read_gen = 5
        main_window._read_in_flight = True
        main_window._on_read_text_ready(5, "", source)

        assert spoken == [], f"{source} spoke something"
        assert captured == [], f"{source} ran OCR"
        assert main_window._read_in_flight is False, f"{source} wedged in-flight"

    # ...and the way out is named, for every cause the user can act on.
    for source in ("empty", "console_blocked"):
        assert "ctrl+shift+s" in main_window._read_failure_message(source), source


class _BalloonTray:
    def __init__(self, broken=False):
        self.messages = []
        self.broken = broken

    def showMessage(self, title, body, icon, msecs):
        if self.broken:
            raise OSError("no tray")
        self.messages.append((title, body))


def test_tts_falling_behind_reaches_the_tray_once_per_episode(main_window):
    """2026-10-20: a starved CPU was heard as stutter with no cause given."""
    w = main_window
    w.tray = _BalloonTray()
    msg = "Read-aloud paused 2.6s waiting for speech to be generated. The CPU..."

    w.tts.falling_behind.emit(msg)
    w.tts.falling_behind.emit(msg)        # the next read under the same load

    assert len(w.tray.messages) == 1, "one CPU episode raised a balloon per read"
    assert "keep up" in w.tray.messages[0][0]
    assert w.tray.messages[0][1] == msg
    assert w.status_bar.currentMessage() == msg

    # A later episode is news again.
    w._tts_slow_notified_at -= w.TTS_SLOW_NOTIFY_INTERVAL_S + 1
    w.tts.falling_behind.emit(msg)
    assert len(w.tray.messages) == 2


def test_tts_falling_behind_does_not_mark_whisper_degraded(main_window):
    """Not _on_degraded: that label means the MODEL is degraded until restart."""
    w = main_window
    w.tray = _BalloonTray()
    before = w.label_model_status.text()
    w.tts.falling_behind.emit("Read-aloud started late: ...")
    assert w.label_model_status.text() == before
    assert "DEGRADED" not in w.label_model_status.text()


def test_a_failed_slow_tts_balloon_does_not_start_the_quiet_period(main_window):
    w = main_window
    w.tray = _BalloonTray(broken=True)
    w.tts.falling_behind.emit("Read-aloud paused 1.0s ...")   # must not raise
    w.tray = _BalloonTray()
    w.tts.falling_behind.emit("Read-aloud paused 1.0s ...")
    assert len(w.tray.messages) == 1, "the one balloon that matters was eaten"


# ---------------------------------------------------------------------------
# The pill key-CAPTURE flow: pressing keys on a HotkeyCaptureWidget must
# translate Qt key events into the right combo string (the literal
# "change my hotkey" interaction). This is what the registration battery did
# NOT cover — it tested downstream of capture.
# ---------------------------------------------------------------------------
class TestHotkeyCapture:
    def _widget(self, qapp):
        from voiceassistant.widgets import HotkeyCaptureWidget
        w = HotkeyCaptureWidget("Dictate", "f9")
        captured = []
        w.hotkey_changed.connect(captured.append)
        w._capturing = True  # what mousePressEvent does when you click the pill
        return w, captured

    def _press(self, w, key, mods):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        w.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    def _release(self, w, mods):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        w.keyReleaseEvent(QKeyEvent(QEvent.Type.KeyRelease, 0, mods))

    def test_capture_combo_with_modifiers(self, qapp):
        from PySide6.QtCore import Qt
        w, captured = self._widget(qapp)
        self._press(w, Qt.Key.Key_F9,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert captured == ["ctrl+shift+f9"], captured

    def test_capture_single_key(self, qapp):
        from PySide6.QtCore import Qt
        w, captured = self._widget(qapp)
        self._press(w, Qt.Key.Key_F9, Qt.KeyboardModifier.NoModifier)
        assert captured == ["f9"], captured

    def test_capture_letter_combo(self, qapp):
        from PySide6.QtCore import Qt
        w, captured = self._widget(qapp)
        self._press(w, Qt.Key.Key_R,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert captured == ["ctrl+shift+r"], captured

    def test_capture_modifier_only_combo(self, qapp):
        # Josh's ctrl+alt: press both modifiers, release all -> saved on release.
        from PySide6.QtCore import Qt
        w, captured = self._widget(qapp)
        self._press(w, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
        self._press(w, Qt.Key.Key_Alt,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        assert captured == []  # nothing saved while still held
        self._release(w, Qt.KeyboardModifier.NoModifier)
        assert captured == ["ctrl+alt"], captured

    def test_single_modifier_alone_not_saved(self, qapp):
        # One bare modifier is not a usable hotkey — must NOT be captured.
        from PySide6.QtCore import Qt
        w, captured = self._widget(qapp)
        self._press(w, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
        self._release(w, Qt.KeyboardModifier.NoModifier)
        assert captured == [], captured

    def test_escape_cancels_capture(self, qapp):
        from PySide6.QtCore import Qt
        w, captured = self._widget(qapp)
        self._press(w, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        assert captured == []
        assert w._capturing is False  # capture ended, nothing emitted


def test_theme_builds_and_its_icons_exist():
    """The Bridge theme is one formatted string plus three SVGs; a stray brace or a
    missing asset would silently drop the whole style sheet or an icon."""
    from voiceassistant import theme

    assert "{cyan}" not in theme.DARK_STYLE and "{assets}" not in theme.DARK_STYLE
    for name in ("check.svg", "chevron-down.svg", "chevron-up.svg"):
        assert os.path.exists(os.path.join(theme.ASSETS, name)), name
        assert theme.ASSETS + "/" + name in theme.DARK_STYLE


def test_model_badge_follows_model_state(main_window):
    """The header pill's colour comes from its `state` property, not inline colours."""
    w = main_window
    w._on_model_loading("Loading large-v3...")
    assert w.label_model_status.property("state") == "loading"
    w._on_model_ready()
    assert w.label_model_status.property("state") == "ok"
    assert w._model_dot.property("state") == "ok"
    w._on_degraded("CUDA unavailable; running on CPU")
    assert w.label_model_status.property("state") == "degraded"
    assert "DEGRADED" in w.label_model_status.text()


def test_bundled_inter_font_loads(qapp):
    """Bridge's typeface ships with the app; if the files go missing the UI silently
    falls back to Segoe UI, which is exactly the drift this guards against."""
    from voiceassistant import theme

    assert "Inter" in theme.load_bundled_fonts()
    assert theme.ui_family() == "Inter"
    assert os.path.exists(os.path.join(theme.FONTS_DIR, "Inter-LICENSE.txt"))


def test_zoom_is_live_and_remembered(main_window):
    """Josh: the UI felt too big. Zoom rescales the theme and every coded size live,
    clamps to the supported steps, and is saved for the next launch."""
    from voiceassistant import theme

    w = main_window
    w._apply_zoom(0.8)
    assert theme.SCALE == 0.8
    assert w.label_zoom.text() == "80%"
    assert w.config.get("ui_scale") == 0.8
    assert w.label_model_status.height() == theme.px(24) == 19
    assert "font-size: 10px" not in w.styleSheet()   # window has no local sheet
    assert "font-size: 8px" in theme.build_style(0.8)  # 10px section caption at 80%
    w._zoom_step(+1)
    assert w.label_zoom.text() == "90%"
    w._apply_zoom(5)                     # clamped to the largest step
    assert theme.SCALE == theme.ZOOM_STEPS[-1]
    assert not w.btn_zoom_in.isEnabled()
    w._apply_zoom(1.0)
    assert theme.SCALE == 1.0


def test_pill_follows_zoom(main_window):
    """The floating pill is its own top-level window, so the app style sheet alone
    does not shrink it: it must rescale with the main window, in every state, and a
    caption card that is open when the zoom changes must be re-fitted, not clipped."""
    from voiceassistant import theme

    w, ind = main_window, main_window.indicator
    try:
        w._apply_zoom(0.8)
        assert ind.size().width() == ind.COMPACT_W == 160
        assert ind.size().height() == ind.COMPACT_H == 32
        assert "font-size: 10px" in ind._label.styleSheet()      # idle 12px at 80%
        ind.show_recording()
        assert "font-size: 10px" in ind._label.styleSheet()      # 13px at 80%
        assert ind._dot.width() == theme.px(14) == 11
        assert "border-radius: 5px" in ind._dot.styleSheet()     # never over half
        w._apply_zoom(1.0)
        assert ind.size().width() == 200
        assert "font-size: 13px" in ind._label.styleSheet()      # state kept
        assert ind._label.text() == "Recording"

        ind.show_preview("the quick brown fox jumps over", "the lazy")
        card_h = ind.height()
        w._apply_zoom(0.7)
        assert ind.width() == ind.CARD_W == 322
        assert ind._preview.width() == ind.CARD_W - theme.px(24)
        assert ind.height() < card_h                            # re-fitted, not left at 100%
        assert ind.preview_text() == "the quick brown fox jumps over the lazy"
        ind.clear_preview()
        assert ind.size().width() == ind.COMPACT_W
    finally:
        w._apply_zoom(1.0)
        ind._set_idle()
