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
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    monkeypatch.setattr(cfg, "CONFIG_FILE",
                        os.path.join(tempfile.mkdtemp(), "settings.json"))
    monkeypatch.setattr(tr.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
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
