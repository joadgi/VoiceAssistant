"""Voice Assistant — local voice-to-text + screen reader desktop app."""

import sys
import os
import ctypes
import re
import time
import threading
import numpy as np
import pyperclip
import keyboard as kb

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QComboBox, QSlider, QGroupBox,
    QStatusBar, QSystemTrayIcon, QMenu, QDialog, QFormLayout,
    QDialogButtonBox, QCheckBox, QSpinBox, QProgressBar,
    QFrame, QStyle,
)
from PySide6.QtCore import Qt, QTimer, Slot, QSize, Signal, QEvent
from PySide6.QtGui import QIcon, QFont, QAction, QColor, QTextCharFormat
import sounddevice as sd

from config import Config, DEFAULTS, normalize_hotkey, validate_hotkey
from voice_engine import VoiceRecorder, Transcriber
from screen_reader import ScreenCapture, OCREngine, RegionSelector
from tts_engine import TTSEngine

# ---------------------------------------------------------------------------
# Dark theme stylesheet
# ---------------------------------------------------------------------------
DARK_STYLE = """
QMainWindow, QDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Segoe UI", sans-serif;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 14px;
    font-weight: bold;
    color: #cdd6f4;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 600;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #45475a;
}
QPushButton:pressed {
    background-color: #585b70;
}
QPushButton:disabled {
    background-color: #181825;
    color: #6c7086;
    border-color: #313244;
}
QPushButton#btn_record {
    background-color: #a6231a;
    color: #ffffff;
    font-size: 15px;
    padding: 14px 28px;
    border: none;
}
QPushButton#btn_record:hover {
    background-color: #c62828;
}
QPushButton#btn_record[recording="true"] {
    background-color: #d32f2f;
    border: 2px solid #ff5252;
}
QPushButton#btn_stop {
    background-color: #585b70;
    color: #ffffff;
    font-size: 15px;
    padding: 14px 28px;
    border: none;
}
QPushButton#btn_stop:hover {
    background-color: #6c7086;
}
QPushButton#btn_screen_read {
    background-color: #1565c0;
    color: #ffffff;
    font-size: 14px;
    padding: 14px 24px;
    border: none;
}
QPushButton#btn_screen_read:hover {
    background-color: #1976d2;
}
QPushButton#btn_cursor_read {
    background-color: #0d47a1;
    color: #ffffff;
    font-size: 14px;
    padding: 14px 24px;
    border: none;
}
QPushButton#btn_cursor_read:hover {
    background-color: #1565c0;
}
QPushButton#btn_speak {
    background-color: #2e7d32;
    color: #ffffff;
    border: none;
}
QPushButton#btn_speak:hover {
    background-color: #388e3c;
}
QPushButton#btn_copy {
    background-color: #4527a0;
    color: #ffffff;
    border: none;
}
QPushButton#btn_copy:hover {
    background-color: #5e35b1;
}
QTextEdit {
    background-color: #11111b;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 10px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 13px;
    selection-background-color: #45475a;
}
QComboBox {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 6px 10px;
    min-width: 120px;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background-color: #313244;
    color: #cdd6f4;
    selection-background-color: #45475a;
}
QLabel {
    color: #bac2de;
}
QStatusBar {
    background-color: #181825;
    color: #a6adc8;
    font-size: 12px;
}
QProgressBar {
    background-color: #313244;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 3px;
}
QSlider::groove:horizontal {
    background: #45475a;
    height: 6px;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #89b4fa;
    width: 16px;
    height: 16px;
    margin: -5px 0;
    border-radius: 8px;
}
QCheckBox {
    color: #cdd6f4;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 3px;
    border: 1px solid #45475a;
    background: #313244;
}
QCheckBox::indicator:checked {
    background: #89b4fa;
    border-color: #89b4fa;
}
"""


class HotkeyCaptureWidget(QFrame):
    """A pill-shaped widget that captures any key combo when clicked.

    Click it → border turns red, shows "press keys..."
    Press any combo (1, 2, 3+ keys) → captured and saved
    Click elsewhere or press Escape → cancel
    """

    hotkey_changed = Signal(str)  # emits the new hotkey string

    SPECIAL_KEYS = {
        # Standalone-friendly keys (good single-key push-to-talk choices)
        Qt.Key.Key_CapsLock: "caps lock", Qt.Key.Key_ScrollLock: "scroll lock",
        Qt.Key.Key_Pause: "pause", Qt.Key.Key_Print: "print screen",
        Qt.Key.Key_Menu: "menu",
        Qt.Key.Key_F1: "f1", Qt.Key.Key_F2: "f2", Qt.Key.Key_F3: "f3",
        Qt.Key.Key_F4: "f4", Qt.Key.Key_F5: "f5", Qt.Key.Key_F6: "f6",
        Qt.Key.Key_F7: "f7", Qt.Key.Key_F8: "f8", Qt.Key.Key_F9: "f9",
        Qt.Key.Key_F10: "f10", Qt.Key.Key_F11: "f11", Qt.Key.Key_F12: "f12",
        Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "enter",
        Qt.Key.Key_Enter: "enter",
        Qt.Key.Key_Tab: "tab", Qt.Key.Key_Delete: "delete",
        Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Insert: "insert",
        Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
        Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown",
        Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
        Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
        Qt.Key.Key_Minus: "-", Qt.Key.Key_Equal: "=",
        Qt.Key.Key_BracketLeft: "[", Qt.Key.Key_BracketRight: "]",
        Qt.Key.Key_Semicolon: ";", Qt.Key.Key_Apostrophe: "'",
        Qt.Key.Key_Comma: ",", Qt.Key.Key_Period: ".",
        Qt.Key.Key_Slash: "/", Qt.Key.Key_Backslash: "\\",
        Qt.Key.Key_QuoteLeft: "`",
    }

    def __init__(self, label, initial_hotkey, parent=None):
        super().__init__(parent)
        self._label = label
        self._hotkey = initial_hotkey
        self._capturing = False
        self._held_mods = []  # track order of modifier presses for combo naming

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(160)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        self._text = QLabel()
        self._text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._text)

        self._update_display()
        self._set_idle_style()

    def hotkey(self):
        return self._hotkey

    def set_hotkey(self, combo):
        self._hotkey = combo
        self._update_display()

    def _update_display(self):
        if self._capturing:
            self._text.setText(f"{self._label}: press any keys…")
        else:
            self._text.setText(f"{self._label}: {self._hotkey}")

    def _set_idle_style(self):
        self.setStyleSheet(
            "HotkeyCaptureWidget { background: #313244; border: 1px solid #585b70; "
            "border-radius: 6px; }"
            "HotkeyCaptureWidget:hover { background: #45475a; border-color: #89b4fa; }"
            "QLabel { color: #89b4fa; font-weight: bold; font-size: 11px; background: transparent; border: none; }"
        )

    def _set_capture_style(self):
        self.setStyleSheet(
            "HotkeyCaptureWidget { background: #181825; border: 2px solid #f38ba8; "
            "border-radius: 6px; }"
            "QLabel { color: #f38ba8; font-weight: bold; font-size: 11px; background: transparent; border: none; }"
        )

    def mousePressEvent(self, event):
        if not self._capturing:
            self._capturing = True
            self._held_mods = []
            self._set_capture_style()
            self._update_display()
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def _current_mods(self, modifiers):
        """Return ordered list of modifier names currently held."""
        mods = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            mods.append("ctrl")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            mods.append("shift")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            mods.append("alt")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            mods.append("windows")
        return mods

    def _show_in_progress(self, mods):
        """Update display while user is building a combo."""
        if mods:
            self._text.setText(f"{self._label}: {'+'.join(mods)}+… (release to save)")
        else:
            self._text.setText(f"{self._label}: press any keys…")

    def _finish_capture(self, combo):
        self._hotkey = combo
        self._capturing = False
        self._held_mods = []
        self._set_idle_style()
        self._update_display()
        self.clearFocus()
        self.hotkey_changed.emit(combo)

    def _cancel_capture(self):
        self._capturing = False
        self._held_mods = []
        self._set_idle_style()
        self._update_display()
        self.clearFocus()

    def keyPressEvent(self, event):
        if not self._capturing:
            return super().keyPressEvent(event)

        key = event.key()
        modifiers = event.modifiers()

        # Escape with no modifiers cancels
        if key == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:
            self._cancel_capture()
            return

        mods = self._current_mods(modifiers)
        self._held_mods = mods  # remember current set for release handling

        # If this is a modifier key itself, update the display and wait for more
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            self._show_in_progress(mods)
            return

        # Otherwise, a non-modifier key was pressed — finish with this combo
        key_name = ""
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            key_name = chr(key).lower()
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            key_name = chr(key)
        else:
            key_name = self.SPECIAL_KEYS.get(key, "")

        if key_name:
            combo = "+".join(mods + [key_name])
            self._finish_capture(combo)

    def keyReleaseEvent(self, event):
        """When user releases all keys, save modifier-only combos if ≥ 2 mods were held."""
        if not self._capturing:
            return super().keyReleaseEvent(event)

        modifiers = event.modifiers()
        still_held = self._current_mods(modifiers)

        # All modifiers released?
        if not still_held and self._held_mods:
            if len(self._held_mods) >= 2:
                # Save the modifier-only combo (e.g. ctrl+windows)
                combo = "+".join(self._held_mods)
                self._finish_capture(combo)
            else:
                # Only 1 modifier was held — not a useful hotkey alone
                self._held_mods = []
                self._show_in_progress([])

    def focusOutEvent(self, event):
        if self._capturing:
            self._cancel_capture()
        super().focusOutEvent(event)


class SettingsDialog(QDialog):
    """Settings dialog for configuring the assistant."""

    def __init__(self, config, tts_engine, parent=None):
        super().__init__(parent)
        self.config = config
        self.tts = tts_engine
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._build_ui()

    def _build_ui(self):
        layout = QFormLayout(self)
        layout.setSpacing(12)

        # --- Audio Input section ---
        section_label0 = QLabel("AUDIO INPUT")
        section_label0.setStyleSheet("color: #89b4fa; font-weight: bold; font-size: 11px; padding-top: 4px;")
        layout.addRow(section_label0)

        self.mic_combo = QComboBox()
        self.mic_combo.addItem("System Default", -1)
        try:
            devices = sd.query_devices()
            seen = set()
            for i, d in enumerate(devices):
                if d["max_input_channels"] > 0:
                    name = d["name"]
                    if name not in seen:
                        seen.add(name)
                        self.mic_combo.addItem(name, i)
        except Exception:
            pass
        saved_mic = self.config.get("audio_device", -1)
        idx = self.mic_combo.findData(saved_mic)
        if idx >= 0:
            self.mic_combo.setCurrentIndex(idx)
        layout.addRow("Microphone:", self.mic_combo)

        # --- Transcription section ---
        section_label = QLabel("TRANSCRIPTION")
        section_label.setStyleSheet("color: #89b4fa; font-weight: bold; font-size: 11px; padding-top: 8px;")
        layout.addRow(section_label)

        self.model_combo = QComboBox()
        for m in ["tiny", "base", "small", "medium", "large-v3"]:
            self.model_combo.addItem(m)
        self.model_combo.setCurrentText(self.config["whisper_model"])
        layout.addRow("Whisper Model:", self.model_combo)

        model_hint = QLabel("tiny=fastest  base=fast+accurate  medium=best accuracy")
        model_hint.setStyleSheet("color: #6c7086; font-size: 10px;")
        layout.addRow(model_hint)

        self.lang_combo = QComboBox()
        for code, name in [("en", "English"), ("es", "Spanish"), ("fr", "French"),
                           ("de", "German"), ("ja", "Japanese"), ("zh", "Chinese"),
                           ("pt", "Portuguese"), ("it", "Italian"), ("ko", "Korean")]:
            self.lang_combo.addItem(name, code)
        idx = self.lang_combo.findData(self.config["whisper_language"])
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        layout.addRow("Language:", self.lang_combo)

        # --- Hotkeys moved to main window ---
        hotkey_note = QLabel(
            "Hotkeys are set on the main window — click any hotkey pill\n"
            "under Dictation Mode to edit it inline."
        )
        hotkey_note.setStyleSheet(
            "color: #89b4fa; font-size: 11px; font-style: italic; padding: 8px 0;"
        )
        layout.addRow(hotkey_note)

        # --- Display section ---
        section_label3 = QLabel("DISPLAY")
        section_label3.setStyleSheet("color: #89b4fa; font-weight: bold; font-size: 11px; padding-top: 8px;")
        layout.addRow(section_label3)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(9, 24)
        self.font_spin.setValue(self.config["font_size"])
        layout.addRow("Font Size:", self.font_spin)

        self.aot_check = QCheckBox("Always on top")
        self.aot_check.setChecked(self.config["always_on_top"])
        layout.addRow(self.aot_check)

        self.startup_check = QCheckBox("Start with Windows")
        self.startup_check.setChecked(self.config.get("start_with_windows", True))
        layout.addRow(self.startup_check)

        self.start_minimized_check = QCheckBox("Start minimized to tray")
        self.start_minimized_check.setChecked(self.config.get("start_minimized", True))
        layout.addRow(self.start_minimized_check)

        self.cleanup_check = QCheckBox("Light cleanup for dictation")
        self.cleanup_check.setChecked(self.config.get("light_cleanup", True))
        layout.addRow(self.cleanup_check)

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_values(self):
        return {
            "audio_device": self.mic_combo.currentData(),
            "whisper_model": self.model_combo.currentText(),
            "whisper_language": self.lang_combo.currentData(),
            "font_size": self.font_spin.value(),
            "always_on_top": self.aot_check.isChecked(),
            "start_with_windows": self.startup_check.isChecked(),
            "start_minimized": self.start_minimized_check.isChecked(),
            "light_cleanup": self.cleanup_check.isChecked(),
            # Hotkeys are no longer edited here — keep current values
            "hotkey_record": self.config["hotkey_record"],
            "hotkey_read_aloud": self.config["hotkey_read_aloud"],
            "hotkey_screen_read": self.config["hotkey_screen_read"],
        }


# ---------------------------------------------------------------------------
# Windows API helpers for dictation paste
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32


def collapse_repeated_phrases(text):
    """Collapse consecutive repeated phrases (1–8 words) — Whisper silence artifacts.

    'send the file send the file' → 'send the file';  'the the the' → 'the'.
    Comparison is case-insensitive; the first occurrence's casing is kept.
    """
    words = text.split()
    out = []
    i = 0
    n = len(words)
    while i < n:
        matched = False
        max_plen = min(8, (n - i) // 2)
        for plen in range(max_plen, 0, -1):
            phrase = [w.lower() for w in words[i:i + plen]]
            reps = 1
            j = i + plen
            while [w.lower() for w in words[j:j + plen]] == phrase:
                reps += 1
                j += plen
            if reps >= 2:
                out.extend(words[i:i + plen])  # keep one copy, original casing
                i = j
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return " ".join(out)


def sanitize_for_paste(text):
    """Make text safe to paste: drop control chars and flatten newlines/tabs.

    Stray newlines/control chars are what make single-line and chat inputs emit
    the Windows 'ding' on paste.
    """
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_foreground_window():
    """Return the HWND of the currently focused window."""
    return user32.GetForegroundWindow()


def set_foreground_window(hwnd):
    """Bring a window to front. Uses AttachThreadInput trick for reliability."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    if current_thread != target_thread:
        user32.AttachThreadInput(current_thread, target_thread, True)
    user32.SetForegroundWindow(hwnd)
    if current_thread != target_thread:
        user32.AttachThreadInput(current_thread, target_thread, False)
    return True


_paste_lock = threading.Lock()
_debug_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
_single_instance_mutex = None
_show_window_event = None


def _dbg(msg):
    try:
        with open(_debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def request_existing_instance_show():
    """Ask the already-running app to show its main window."""
    EVENT_MODIFY_STATE = 0x0002
    kernel32 = ctypes.windll.kernel32
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, r"Local\VoiceAssistant.ShowWindow")
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)
        return True
    return False


def create_show_window_event():
    global _show_window_event
    kernel32 = ctypes.windll.kernel32
    _show_window_event = kernel32.CreateEventW(None, False, False, r"Local\VoiceAssistant.ShowWindow")
    return _show_window_event


def acquire_single_instance_lock():
    """Prevent two app copies from registering the same global hotkeys."""
    global _single_instance_mutex
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, r"Local\VoiceAssistant.MainInstance")
    if not mutex:
        return True
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        request_existing_instance_show()
        _dbg("second instance detected; requested existing window show")
        kernel32.CloseHandle(mutex)
        return False
    _single_instance_mutex = mutex
    create_show_window_event()
    return True


def set_start_with_windows(enabled):
    """Register or remove the tray-first startup command for this user."""
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                pythonw = sys.executable
                if pythonw.lower().endswith("python.exe"):
                    pythonw = pythonw[:-10] + "pythonw.exe"
                command = f'"{pythonw}" "{os.path.abspath(__file__)}" --minimized'
                winreg.SetValueEx(key, "VoiceAssistant", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "VoiceAssistant")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        _dbg(f"startup registration failed: {e}")
        return False


def _win32_paste():
    """Send Ctrl+V via raw Win32 API — no keyboard-library involvement."""
    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x0002
    _dbg("  _win32_paste: ctrl down")
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.015)
    _dbg("  _win32_paste: v down")
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.03)
    _dbg("  _win32_paste: v up")
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.015)
    _dbg("  _win32_paste: ctrl up")
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _window_title(hwnd):
    """Get the title of a window by hwnd (for debug)."""
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return "?"


def paste_to_window(hwnd, text):
    """Focus a window and paste text. Guarded against re-entry only.

    The non-blocking lock stops a genuine re-entrant double-paste; we no longer
    impose a 1s cooldown, which used to swallow a legitimate fast second dictation.
    """
    # Privacy: log only handles/lengths here — never window titles or the
    # actual dictated/clipboard text (debug.log persists on disk).
    fg = user32.GetForegroundWindow()
    _dbg(f"paste_to_window ENTER  target_hwnd={hwnd}  current_fg={fg}")

    if not _paste_lock.acquire(blocking=False):
        _dbg("paste_to_window REJECTED — lock held")
        return False
    try:
        text = sanitize_for_paste(text)
        if not text:
            _dbg("  nothing left after sanitize, skipping paste")
            return False
        pyperclip.copy(text)
        _dbg(f"  clipboard set ({len(text)} chars)")

        # Wait until user has released modifier keys first
        for i in range(100):
            if not (kb.is_pressed("ctrl") or kb.is_pressed("shift") or kb.is_pressed("alt")
                    or kb.is_pressed("windows")):
                _dbg(f"  mods released after {i*20}ms")
                break
            time.sleep(0.02)

        # If focus drifted off the target, just refocus it. We deliberately do
        # NOT inject Escape — sending Esc into the target app is what produced
        # the audible Windows beep on many controls.
        current_fg = user32.GetForegroundWindow()
        if current_fg != hwnd:
            _dbg(f"  focus is on {current_fg}, refocusing target (no Esc)")
            if not set_foreground_window(hwnd):
                _dbg("  set_foreground_window FAILED")
                return False
            time.sleep(0.12)
        else:
            _dbg(f"  focus already on target window")
            # Still need a small delay so any keystate settles
            time.sleep(0.05)

        _win32_paste()
        _dbg("paste_to_window DONE")
        return True
    finally:
        time.sleep(0.3)
        _paste_lock.release()


# ---------------------------------------------------------------------------
# Floating recording indicator — small always-on-top pill
# ---------------------------------------------------------------------------
class RecordingIndicator(QWidget):
    """Always-visible floating pill that shows dictation state and can be
    clicked to start/stop recording. Drag it anywhere on the desktop."""

    clicked = Signal()  # emitted on a click (not a drag)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        # Don't steal focus from the window the user is dictating into.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setFixedSize(200, 40)
        self.setToolTip("Click to start/stop dictation  •  drag to move")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)

        self._dot = QLabel()
        self._dot.setFixedSize(14, 14)
        lay.addWidget(self._dot)

        self._label = QLabel("Ready")
        self._label.setStyleSheet("color: #cdd6f4; font-weight: bold; font-size: 13px;")
        lay.addWidget(self._label)
        lay.addStretch()

        self._positioned = False  # auto-place once, then respect user drags
        self._drag_offset = None
        self._set_idle()

    def _position_bottom_right(self):
        if self._positioned:
            return
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 20, screen.bottom() - self.height() - 60)
        self._positioned = True

    # --- drag-to-move + click-to-record ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._positioned = True
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            self._drag_offset = None
            if moved < 6:  # treat as a click, not a drag
                self.clicked.emit()
            event.accept()

    def _apply(self, border, dot, text, text_color):
        self.setStyleSheet(f"background-color: #1e1e2e; border: 2px solid {border}; border-radius: 8px;")
        self._dot.setStyleSheet(f"background-color: {dot}; border-radius: 7px; border: none;")
        self._label.setText(text)
        self._label.setStyleSheet(f"color: {text_color}; font-weight: bold; font-size: 13px;")
        self._position_bottom_right()
        self.show()
        self.raise_()

    def show_recording(self):
        self._apply("#f38ba8", "#f38ba8", "● Recording", "#f38ba8")

    def show_transcribing(self):
        self._apply("#f9e2af", "#f9e2af", "Transcribing…", "#f9e2af")

    def show_done(self):
        self._apply("#a6e3a1", "#a6e3a1", "Pasted ✓", "#a6e3a1")
        QTimer.singleShot(1500, self.show_idle)

    def show_idle(self):
        self._set_idle()
        self._position_bottom_right()
        self.show()
        self.raise_()

    def _set_idle(self):
        self.setStyleSheet("background-color: #1e1e2e; border: 1px solid #45475a; border-radius: 8px;")
        self._dot.setStyleSheet("background-color: #585b70; border-radius: 7px; border: none;")
        self._label.setText("Ready — click to dictate")
        self._label.setStyleSheet("color: #9399b2; font-weight: bold; font-size: 12px;")


class MainWindow(QMainWindow):
    # Thread-safe signals for global hotkey callbacks
    _sig_hotkey_press = Signal()
    _sig_hotkey_release = Signal()
    _sig_hotkey_screen = Signal()
    _sig_hotkey_read = Signal()

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.setWindowTitle("Voice Assistant")
        self.setMinimumSize(720, 520)
        self.resize(820, 600)

        # --- Engines ---
        self.recorder = VoiceRecorder(sample_rate=self.config["sample_rate"])
        self.transcriber = Transcriber(
            model_size=self.config["whisper_model"],
            device=self.config["whisper_device"],
            compute_type=self.config["whisper_compute_type"],
            language=self.config["whisper_language"],
        )
        self.screen_capture = ScreenCapture()
        self.ocr = OCREngine(
            languages=self.config["ocr_languages"],
            gpu=self.config["ocr_gpu"],
        )
        self.tts = TTSEngine(
            rate=self.config["tts_rate"],
            volume=self.config["tts_volume"],
        )
        self.tts.set_speed(self.config.get("tts_speed", 1.0))
        saved_voice = self.config.get("tts_voice", "en-US-AndrewNeural")
        self.tts.set_voice(saved_voice)
        self.region_selector = RegionSelector()
        self.indicator = RecordingIndicator()

        # Dictation state
        self._target_hwnd = None  # window to paste into after transcription
        self._read_target_hwnd = None  # window to refocus for read-selection copy
        self._dictation_active = self.config.get("dictation_mode", True)

        self._build_ui()
        self._connect_signals()

        # Push-to-talk state
        self._ptt_active = False
        self._last_hotkey_press_time = 0.0
        self._force_quit = False
        # Debounce flag for read-aloud hotkey
        self._read_in_flight = False
        # Track last transcription to prevent double-paste
        self._last_transcription = ""
        self._last_transcription_time = 0.0

        # Wire hotkey signals BEFORE registering hotkeys
        self._sig_hotkey_press.connect(self._hotkey_press_handler)
        self._sig_hotkey_release.connect(self._hotkey_release_handler)
        self._sig_hotkey_screen.connect(self._on_cursor_read)
        self._sig_hotkey_read.connect(self._on_read_aloud_toggle)
        self._sig_read_text_ready.connect(self._on_read_text_ready)

        self._setup_hotkeys()
        self._setup_tray()
        self._setup_show_request_timer()
        set_start_with_windows(self.config.get("start_with_windows", True))
        self._apply_window_flags()

        # Load models in background
        self.transcriber.load_model()
        self.ocr.load_model()

        self._update_status("Starting up...")
        self.indicator.show_idle()  # always-visible desktop pill

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 12, 16, 8)
        root_layout.setSpacing(10)

        # ---- Top control bar ----
        voice_group = QGroupBox("Voice")
        voice_lay = QHBoxLayout(voice_group)

        self.btn_record = QPushButton("  Record")
        self.btn_record.setObjectName("btn_record")
        self.btn_record.setToolTip(f"Start recording ({self.config['hotkey_record']})")

        self.btn_stop = QPushButton("  Stop")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setEnabled(False)

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setValue(0)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(8)

        voice_lay.addWidget(self.btn_record)
        voice_lay.addWidget(self.btn_stop)
        voice_lay.addWidget(self.level_bar, 1)

        screen_group = QGroupBox("Screen Reader")
        screen_lay = QHBoxLayout(screen_group)

        self.btn_screen_read = QPushButton("  Select Region")
        self.btn_screen_read.setObjectName("btn_screen_read")
        self.btn_screen_read.setToolTip("Draw a rectangle to read text from screen")

        self.btn_cursor_read = QPushButton("  Read at Cursor")
        self.btn_cursor_read.setObjectName("btn_cursor_read")
        self.btn_cursor_read.setToolTip(
            f"Read text near cursor ({self.config['hotkey_screen_read']})"
        )

        screen_lay.addWidget(self.btn_screen_read)
        screen_lay.addWidget(self.btn_cursor_read)

        top_row = QHBoxLayout()
        top_row.addWidget(voice_group, 2)
        top_row.addWidget(screen_group, 1)
        root_layout.addLayout(top_row)

        # ---- Model selector ----
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Whisper Model:"))
        self.model_combo = QComboBox()
        for m in ["tiny", "base", "small", "medium", "large-v3"]:
            self.model_combo.addItem(m)
        self.model_combo.setCurrentText(self.config["whisper_model"])
        model_row.addWidget(self.model_combo)
        model_row.addStretch()

        self.label_model_status = QLabel("Loading...")
        self.label_model_status.setStyleSheet("color: #f9e2af; font-weight: bold;")
        model_row.addWidget(self.label_model_status)
        root_layout.addLayout(model_row)

        # ---- Dictation mode toggle ----
        dict_row = QHBoxLayout()
        self.btn_dictation = QPushButton("  DICTATION MODE: ON")
        self.btn_dictation.setCheckable(True)
        self.btn_dictation.setChecked(self._dictation_active)
        self._update_dictation_button()
        self.btn_dictation.setStyleSheet("""
            QPushButton {
                font-size: 13px; font-weight: bold; padding: 8px 16px;
                border-radius: 6px; border: none;
            }
            QPushButton:checked {
                background-color: #2e7d32; color: #ffffff;
            }
            QPushButton:!checked {
                background-color: #313244; color: #6c7086;
            }
        """)
        dict_row.addWidget(self.btn_dictation)

        self.dictation_hint = QLabel("")
        self.dictation_hint.setStyleSheet("color: #6c7086; font-size: 11px;")
        self._update_dictation_hint()
        dict_row.addWidget(self.dictation_hint)
        dict_row.addStretch()
        root_layout.addLayout(dict_row)

        # ---- Hotkey capture widgets ----
        hk_row = QHBoxLayout()
        hk_label = QLabel("Hotkeys:")
        hk_label.setStyleSheet("font-weight: bold;")
        hk_row.addWidget(hk_label)

        self.hk_dictate = HotkeyCaptureWidget("Dictate", self.config["hotkey_record"])
        self.hk_read = HotkeyCaptureWidget("Read", self.config["hotkey_read_aloud"])
        self.hk_ocr = HotkeyCaptureWidget("OCR", self.config["hotkey_screen_read"])

        self.hk_dictate.hotkey_changed.connect(
            lambda c: self._save_hotkey("hotkey_record", "Dictate", c)
        )
        self.hk_read.hotkey_changed.connect(
            lambda c: self._save_hotkey("hotkey_read_aloud", "Read", c)
        )
        self.hk_ocr.hotkey_changed.connect(
            lambda c: self._save_hotkey("hotkey_screen_read", "OCR", c)
        )

        hk_row.addWidget(self.hk_dictate)
        hk_row.addWidget(self.hk_read)
        hk_row.addWidget(self.hk_ocr)
        hk_row.addStretch()

        hk_reset = QPushButton("  Reset  ")
        hk_reset.setStyleSheet(
            "QPushButton { background: #45475a; color: #f9e2af; border: 1px solid #585b70; "
            "border-radius: 4px; padding: 4px 10px; font-size: 11px; }"
            "QPushButton:hover { background: #585b70; }"
        )
        hk_reset.clicked.connect(self._reset_hotkeys_to_defaults)
        hk_row.addWidget(hk_reset)

        hk_hint = QLabel("  (click, then press your keys)")
        hk_hint.setStyleSheet("color: #6c7086; font-size: 10px;")
        hk_row.addWidget(hk_hint)
        root_layout.addLayout(hk_row)

        # ---- Text output ----
        self.text_output = QTextEdit()
        self.text_output.setPlaceholderText(
            "Transcription and screen reader output will appear here..."
        )
        self.text_output.setFont(
            QFont("Cascadia Code", self.config["font_size"])
        )
        root_layout.addWidget(self.text_output, 1)

        # ---- Voice & Speed controls (always visible) ----
        playback_group = QGroupBox("Playback")
        playback_lay = QHBoxLayout(playback_group)
        playback_lay.setSpacing(12)

        playback_lay.addWidget(QLabel("Voice:"))
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(260)
        voices = self.tts.get_voices()
        for vid, vname in voices:
            self.voice_combo.addItem(vname, vid)
        # Select saved voice
        saved_voice = self.config.get("tts_voice", "en-US-AndrewNeural")
        idx = self.voice_combo.findData(saved_voice)
        if idx >= 0:
            self.voice_combo.setCurrentIndex(idx)
        playback_lay.addWidget(self.voice_combo)

        playback_lay.addSpacing(16)
        playback_lay.addWidget(QLabel("Speed:"))

        # Speed as multiplier: 50 = 0.5x, 100 = 1.0x, 300 = 3.0x
        initial_speed = self.config.get("tts_speed", 1.0)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(50, 300)
        self.speed_slider.setValue(int(initial_speed * 100))
        self.speed_slider.setFixedWidth(200)
        self.speed_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.speed_slider.setTickInterval(25)
        playback_lay.addWidget(self.speed_slider)

        self.speed_label = QLabel(f"{initial_speed:.2f}x")
        self.speed_label.setFixedWidth(60)
        self.speed_label.setStyleSheet("color: #89b4fa; font-weight: bold;")
        playback_lay.addWidget(self.speed_label)

        playback_lay.addStretch()
        root_layout.addWidget(playback_group)

        # ---- Bottom action bar ----
        action_row = QHBoxLayout()

        self.btn_copy = QPushButton("  Copy")
        self.btn_copy.setObjectName("btn_copy")

        self.btn_clear = QPushButton("  Clear")

        self.btn_speak_toggle = QPushButton("  Speak")
        self.btn_speak_toggle.setObjectName("btn_speak")
        self.btn_speak_toggle.setCheckable(True)

        self.btn_settings = QPushButton("  Settings")

        action_row.addWidget(self.btn_copy)
        action_row.addWidget(self.btn_clear)
        action_row.addStretch()
        action_row.addWidget(self.btn_speak_toggle)
        action_row.addStretch()
        action_row.addWidget(self.btn_settings)
        root_layout.addLayout(action_row)

        # ---- Status bar ----
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    # -----------------------------------------------------------------------
    # Signal connections
    # -----------------------------------------------------------------------
    def _connect_signals(self):
        # Buttons
        self.btn_record.clicked.connect(self._on_record)
        self.btn_stop.clicked.connect(self._on_stop_record)
        self.btn_screen_read.clicked.connect(self._on_screen_select)
        self.btn_cursor_read.clicked.connect(self._on_cursor_read)
        self.btn_copy.clicked.connect(self._on_copy)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_speak_toggle.clicked.connect(self._on_speak_toggle)
        self.btn_settings.clicked.connect(self._on_settings)
        self.model_combo.currentTextChanged.connect(self._on_model_change)

        # Playback controls (always visible)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_change)
        self.speed_slider.valueChanged.connect(self._on_speed_change)

        # Dictation toggle
        self.btn_dictation.toggled.connect(self._on_dictation_toggle)

        # Voice engine
        self.recorder.recording_started.connect(self._on_recording_started)
        self.recorder.recording_stopped.connect(self._on_recording_stopped)
        self.recorder.level_update.connect(self._on_level_update)
        self.recorder.error.connect(self._on_error)

        # Transcriber
        self.transcriber.model_loading.connect(self._on_model_loading)
        self.transcriber.model_ready.connect(self._on_model_ready)
        self.transcriber.transcription_ready.connect(self._on_transcription_ready)
        self.transcriber.transcription_progress.connect(self._update_status)
        self.transcriber.error.connect(self._on_error)

        # OCR
        self.ocr.model_loading.connect(self._on_model_loading)
        self.ocr.model_ready.connect(self._on_ocr_ready)
        self.ocr.text_ready.connect(self._on_ocr_text_ready)
        self.ocr.error.connect(self._on_error)

        # TTS
        self.tts.speaking_started.connect(self._on_tts_started)
        self.tts.speaking_finished.connect(self._on_tts_finished)
        self.tts.status.connect(self._update_status)
        self.tts.error.connect(self._on_error)

        # Region selector
        self.region_selector.region_selected.connect(self._on_region_selected)
        self.region_selector.cancelled.connect(lambda: self._update_status("Selection cancelled"))

        # Floating desktop indicator — click to toggle dictation
        self.indicator.clicked.connect(self._on_indicator_clicked)

    # -----------------------------------------------------------------------
    # Hotkeys (global)
    # -----------------------------------------------------------------------
    def _setup_hotkeys(self):
        try:
            kb.unhook_all()
        except Exception:
            pass

        hk_record = self._clean_hotkey("hotkey_record")
        hk_screen = self._clean_hotkey("hotkey_screen_read")
        hk_read = self._clean_hotkey("hotkey_read_aloud")
        errors = []

        try:
            # Watch the trigger key for press (start) and release (stop). The press
            # callback only fires recording when the FULL combo is held, so typing
            # the trigger letter alone never starts dictation. Works for single-key
            # hotkeys (e.g. f9) too. The release handler is a no-op unless PTT is on.
            trigger_key = self._hotkey_trigger_key(hk_record)
            kb.on_press_key(
                trigger_key,
                lambda e, combo=hk_record: self._emit_record_press_if_active(combo),
            )
            kb.on_release_key(trigger_key, lambda e: self._sig_hotkey_release.emit())
        except Exception as e:
            errors.append(f"Record hotkey ({hk_record}): {e}")

        try:
            kb.add_hotkey(hk_screen, lambda: self._sig_hotkey_screen.emit())
        except Exception as e:
            errors.append(f"Screen hotkey ({hk_screen}): {e}")

        needs_suppress_read = "windows" in hk_read
        try:
            kb.add_hotkey(hk_read, lambda: self._sig_hotkey_read.emit(),
                          suppress=needs_suppress_read)
        except Exception:
            try:
                kb.add_hotkey(hk_read, lambda: self._sig_hotkey_read.emit())
            except Exception as e2:
                errors.append(f"Read aloud hotkey ({hk_read}): {e2}")

        if errors:
            self._update_status("Hotkey errors: " + "; ".join(errors))
        else:
            self._update_status(f"Hotkeys: {hk_record}=dictate  {hk_read}=read selection  {hk_screen}=OCR")

    def _clean_hotkey(self, config_key):
        combo = normalize_hotkey(self.config.get(config_key, DEFAULTS[config_key]))
        if not validate_hotkey(combo):
            combo = DEFAULTS[config_key]
        self.config.set(config_key, combo)
        return combo

    def _hotkey_parts(self, combo):
        return [part for part in normalize_hotkey(combo).split("+") if part]

    def _hotkey_trigger_key(self, combo):
        non_modifiers = [part for part in self._hotkey_parts(combo) if part not in {"ctrl", "shift", "alt", "windows", "cmd", "meta"}]
        return non_modifiers[-1] if non_modifiers else self._hotkey_parts(combo)[-1]

    def _emit_record_press_if_active(self, combo):
        """Fire the record signal only when every key in the combo is held."""
        try:
            if all(kb.is_pressed(part) for part in self._hotkey_parts(combo)):
                self._sig_hotkey_press.emit()
        except Exception as e:
            _dbg(f"record hotkey state check failed: {e}")

    def _set_hotkey_if_valid(self, config_key, label, combo):
        combo = normalize_hotkey(combo)
        if not validate_hotkey(combo):
            self._update_status(
                f"{label}: use a single key like F9 or Caps Lock, or a combo like Ctrl+Shift+F9"
            )
            return False
        other_keys = [k for k in ("hotkey_record", "hotkey_read_aloud", "hotkey_screen_read")
                      if k != config_key]
        if any(normalize_hotkey(self.config[k]) == combo for k in other_keys):
            self._update_status(f"{combo} is already used by another hotkey")
            return False
        self.config.set(config_key, combo)
        return True

    @Slot()
    def _hotkey_press_handler(self):
        """Hotkey pressed — start recording.

        NOTE: OS key-autorepeat re-fires this at ~30Hz for the whole time the
        hotkey is held. Keep the debounce path silent and log only when we
        actually act — the old per-repeat logging wrote to disk ~60x/sec on the
        GUI thread and bloated debug.log by megabytes per session.
        """
        now = time.monotonic()
        if now - self._last_hotkey_press_time < 0.25:
            return  # autorepeat / double-fire — ignore silently
        self._last_hotkey_press_time = now
        if not self.recorder.is_recording and not self._ptt_active:
            _dbg(f"_hotkey_press_handler: starting PTT")
            self._ptt_active = True
            self._on_record_from_hotkey()

    @Slot()
    def _hotkey_release_handler(self):
        """Trigger key released — stop recording if push-to-talk is active."""
        # No-op (and no logging) unless PTT is active — this fires on every
        # trigger-letter release during normal typing.
        if not self._ptt_active:
            return
        _dbg(f"_hotkey_release_handler: ptt={self._ptt_active}  recording={self.recorder.is_recording}")
        if self._ptt_active and self.recorder.is_recording:
            self._ptt_active = False
            self._on_stop_record()
        elif self._ptt_active:
            self._ptt_active = False

    # -----------------------------------------------------------------------
    # Recording handlers
    # -----------------------------------------------------------------------
    @Slot()
    def _on_record_from_hotkey(self):
        """Start recording via hotkey — capture the currently focused window first."""
        _dbg(f"_on_record_from_hotkey: transcriber_loaded={self.transcriber.is_loaded}")
        if not self.transcriber.is_loaded:
            self._update_status("Whisper model still loading, please wait...")
            return
        if self._dictation_active:
            self._target_hwnd = get_foreground_window()
            _dbg(f"  target_hwnd captured: {self._target_hwnd}")
        self.recorder.start()
        _dbg("  recorder.start() called")

    @Slot()
    def _on_indicator_clicked(self):
        """Click the floating pill to toggle dictation (capture → record → paste)."""
        if self.recorder.is_recording:
            self._ptt_active = False
            self._on_stop_record()
            return
        if not self.transcriber.is_loaded:
            self._update_status("Whisper model still loading, please wait...")
            return
        # Pill doesn't take focus, so the foreground window is still the target.
        if self._dictation_active:
            self._target_hwnd = get_foreground_window()
        self.recorder.start()

    @Slot()
    def _on_record(self):
        """Start recording via button click."""
        if not self.transcriber.is_loaded:
            self._update_status("Whisper model still loading, please wait...")
            return
        self._target_hwnd = None  # clicked in our own window, don't paste elsewhere
        self.recorder.start()

    @Slot()
    def _on_stop_record(self):
        self.recorder.stop()

    @Slot()
    def _on_recording_started(self):
        _dbg("_on_recording_started: showing red pill")
        self.btn_record.setText("  RECORDING")
        self.btn_record.setProperty("recording", "true")
        self.btn_record.style().unpolish(self.btn_record)
        self.btn_record.style().polish(self.btn_record)
        self.btn_record.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._update_status("Recording... speak now")
        self.indicator.show_recording()

    @Slot(np.ndarray)
    def _on_recording_stopped(self, audio):
        duration = len(audio) / 16000.0 if len(audio) else 0
        max_amp = float(np.max(np.abs(audio))) if len(audio) else 0
        _dbg(f"_on_recording_stopped: samples={len(audio)}  duration={duration:.2f}s  peak={max_amp:.4f}")

        self.btn_record.setText("  Record")
        self.btn_record.setProperty("recording", "false")
        self.btn_record.style().unpolish(self.btn_record)
        self.btn_record.style().polish(self.btn_record)
        self.btn_record.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.level_bar.setValue(0)

        if getattr(self, "_last_audio_id", None) == id(audio):
            _dbg("  ignored — same audio buffer")
            return
        self._last_audio_id = id(audio)

        min_seconds = float(self.config.get("min_record_seconds", 0.2))
        min_peak = float(self.config.get("min_record_peak", 0.008))
        if duration < min_seconds or max_amp < min_peak:
            _dbg("  ignored - too short or too quiet for reliable dictation")
            self._update_status("Recording ignored - too short or too quiet")
            self.indicator.show_idle()
            self._target_hwnd = None
            return

        if len(audio) > 0:
            self._update_status(f"Transcribing {duration:.1f}s (peak {max_amp:.3f})...")
            self.indicator.show_transcribing()
            self.transcriber.transcribe(audio)
        else:
            self._update_status("No audio captured")
            self.indicator.show_idle()

    @Slot(float)
    def _on_level_update(self, rms):
        level = min(100, int(rms * 500))
        self.level_bar.setValue(level)

    # -----------------------------------------------------------------------
    # Transcription handlers
    # -----------------------------------------------------------------------
    @Slot(str)
    def _on_model_loading(self, msg):
        self.label_model_status.setText(msg)
        self.label_model_status.setStyleSheet("color: #f9e2af; font-weight: bold;")
        self._update_status(msg)

    @Slot()
    def _on_model_ready(self):
        device = self.transcriber.device.upper()
        self.label_model_status.setText(
            f"Whisper {self.transcriber.model_size} ready ({device})"
        )
        self.label_model_status.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        self._update_status("Ready")

    @Slot()
    def _on_ocr_ready(self):
        gpu_str = "GPU" if self.ocr.gpu else "CPU"
        self._update_status(f"Ready  |  OCR engine loaded ({gpu_str})")

    @Slot(str)
    def _on_transcription_ready(self, text):
        _dbg(f"_on_transcription_ready FIRED  ({len(text)} chars)")
        # --- 1. Clean up Whisper duplicates and light filler/stutter artifacts ---
        text = self._dedupe_repeated(text)
        if self.config.get("light_cleanup", True):
            text = self._light_cleanup(text)

        # --- 2. Skip only a near-instant repeat of identical text (double-fire),
        #        short enough that intentionally repeating a word still works ---
        now = time.time()
        if text == self._last_transcription and (now - self._last_transcription_time) < 1.2:
            _dbg(f"  → duplicate, ignoring")
            self.indicator.show_idle()
            self._target_hwnd = None
            self._update_status("Duplicate transcription ignored")
            return
        self._last_transcription = text
        self._last_transcription_time = now

        # --- 3. Check if target is our own window ---
        own_hwnd = int(self.winId())
        is_own_window = (
            self._target_hwnd is not None
            and self._target_hwnd == get_foreground_window()
            and self._target_hwnd == own_hwnd
        )

        if (self._dictation_active and self.config.get("auto_paste", True)
                and self._target_hwnd and not is_own_window
                and text.strip() and "[No speech" not in text):
            success = paste_to_window(self._target_hwnd, text)
            if success:
                self.indicator.show_done()
                self._update_status("Transcribed and pasted")
                self._target_hwnd = None
                return
            else:
                self.indicator.show_idle()
                self._update_status("Transcribed (target window unavailable, text in panel)")
        else:
            self.indicator.show_idle()
            self._update_status("Transcription complete")

        self._append_output(text, prefix="[Voice]")
        self._target_hwnd = None

    def _light_cleanup(self, text):
        """Very light dictation cleanup: fillers, simple stutters, spacing, casing."""
        if not text or text.startswith("["):
            return text
        cleaned = re.sub(r"\b(um+|uh+|erm|ah+)\b[, ]*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(\w{1,4})[- ]+\1\b", r"\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
            if cleaned[-1] not in ".!?":
                cleaned += "."
        return cleaned

    def _dedupe_repeated(self, text):
        """Collapse consecutive repeated words/phrases Whisper emits on silence.

        Handles 'the the the', 'send the file send the file send the file', and
        repeated full sentences — not just the exact 2x split the old version caught.
        """
        if not text or text.startswith("["):
            return text
        return collapse_repeated_phrases(text)

    # -----------------------------------------------------------------------
    # Screen reader handlers
    # -----------------------------------------------------------------------
    @Slot()
    def _on_screen_select(self):
        if not self.ocr.is_loaded:
            self._update_status("OCR engine still loading...")
            return
        self._update_status("Select a screen region (ESC to cancel)")
        self.region_selector.activate()

    @Slot()
    def _on_cursor_read(self):
        if not self.ocr.is_loaded:
            self._update_status("OCR engine still loading...")
            return
        self._update_status("Reading screen at cursor...")
        img = self.screen_capture.capture_around_cursor(
            width=self.config["screen_capture_width"],
            height=self.config["screen_capture_height"],
        )
        self.ocr.read_image(img)

    @Slot(int, int, int, int)
    def _on_region_selected(self, x, y, w, h):
        self._update_status("Reading selected region...")
        img = self.screen_capture.capture_region(x, y, w, h)
        self.ocr.read_image(img)

    @Slot(str)
    def _on_ocr_text_ready(self, text):
        self._append_output(text, prefix="[Screen]")
        self._update_status("Screen read complete")
        if text.strip() and "[No text" not in text:
            self.tts.speak(text)

    # -----------------------------------------------------------------------
    # Action handlers
    # -----------------------------------------------------------------------
    @Slot()
    def _on_copy(self):
        text = self.text_output.toPlainText()
        if text.strip():
            pyperclip.copy(text)
            self._update_status("Copied to clipboard")
        else:
            self._update_status("Nothing to copy")

    @Slot()
    def _on_clear(self):
        self.text_output.clear()
        self._update_status("Cleared")

    @Slot()
    def _on_speak_toggle(self):
        if self.tts.is_speaking:
            self.tts.stop()
            self.btn_speak_toggle.setChecked(False)
            self._update_status("Speech stopped")
        else:
            text = self.text_output.toPlainText()
            if text.strip():
                self.tts.speak(text)
            else:
                self.btn_speak_toggle.setChecked(False)
                self._update_status("No text to speak")

    @Slot()
    def _on_tts_started(self):
        self.btn_speak_toggle.setText("  Stop")
        self.btn_speak_toggle.setChecked(True)
        self.btn_speak_toggle.setStyleSheet(
            "QPushButton { background-color: #d32f2f; color: #fff; border: none; "
            "border-radius: 6px; padding: 10px 20px; font-size: 13px; font-weight: 600; }"
        )

    @Slot()
    def _on_tts_finished(self):
        self.btn_speak_toggle.setText("  Speak")
        self.btn_speak_toggle.setChecked(False)
        self.btn_speak_toggle.setStyleSheet("")  # reset to theme default for btn_speak

    # Signal used by the read-aloud worker thread to send captured text to UI
    _sig_read_text_ready = Signal(str)

    @Slot()
    def _on_read_aloud_toggle(self):
        """Toggle read aloud: if speaking or in-flight, stop. Otherwise start read."""
        if self.tts.is_speaking or self._read_in_flight:
            self.tts.stop()
            self._read_in_flight = False
            self._update_status("Read aloud stopped")
            return

        # Capture target window NOW before Windows key can steal focus
        self._read_target_hwnd = get_foreground_window()

        self._read_in_flight = True
        self._update_status("Capturing selection...")
        thread = threading.Thread(target=self._read_selection_worker, daemon=True)
        thread.start()

    def _read_selection_worker(self):
        """Wait for modifier release, refocus target window, copy selection, emit signal."""
        # Wait for ALL keys in the hotkey combo to be released (up to 1 second)
        hotkey_keys = self.config["hotkey_read_aloud"].lower().split("+")
        for _ in range(200):
            if not any(kb.is_pressed(k) for k in hotkey_keys if k):
                break
            time.sleep(0.005)

        # Give Windows a moment in case Start menu opened, then close it via Esc
        time.sleep(0.05)
        # If Start menu opened, Escape closes it
        VK_ESCAPE = 0x1B
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_ESCAPE, 0, 0, 0)
        user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)

        # Refocus the target window (where the user had text selected)
        target = getattr(self, "_read_target_hwnd", None)
        if target:
            set_foreground_window(target)
            time.sleep(0.1)

        # Save current clipboard and write a sentinel
        try:
            old_clipboard = pyperclip.paste()
        except Exception:
            old_clipboard = ""

        SENTINEL = "\x00__VA_CLIP_SENTINEL__\x00"
        try:
            pyperclip.copy(SENTINEL)
        except Exception:
            pass

        time.sleep(0.05)

        # Send Ctrl+C via raw Win32 API — bypasses keyboard library
        VK_CONTROL = 0x11
        VK_C = 0x43
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.015)
        user32.keybd_event(VK_C, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.015)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

        # Poll clipboard for up to 1.5 seconds — Gmail/Chrome can be slow
        text = ""
        for _ in range(150):
            time.sleep(0.01)
            try:
                current = pyperclip.paste()
                if current != SENTINEL:
                    text = current
                    break
            except Exception:
                pass

        if text.strip():
            self._sig_read_text_ready.emit(text)
        else:
            try:
                if old_clipboard:
                    pyperclip.copy(old_clipboard)
            except Exception:
                pass
            self._sig_read_text_ready.emit("")

    @Slot(str)
    def _on_read_text_ready(self, text):
        """Called in main thread when selection has been captured."""
        self._read_in_flight = False
        if text:
            self._append_output(text, prefix="[Read]")
            self.tts.speak(text)
            self._update_status(f"Reading {len(text)} chars aloud...")
        else:
            self._update_status("No text selected — highlight text first, then press the hotkey")

    def _save_hotkey(self, config_key, label, combo):
        """Called when a HotkeyCaptureWidget captures a new combo."""
        if not self._set_hotkey_if_valid(config_key, label, combo):
            return
        # Unhook old hotkeys and re-register all
        try:
            kb.unhook_all()
        except Exception:
            pass
        self._ptt_active = False
        self._read_in_flight = False
        self._setup_hotkeys()
        self._update_dictation_hint()
        self._update_status(f"{label} hotkey set to {self.config[config_key]}")

    def _reset_hotkeys_to_defaults(self):
        from config import DEFAULTS
        self.config.set("hotkey_record", DEFAULTS["hotkey_record"])
        self.config.set("hotkey_read_aloud", DEFAULTS["hotkey_read_aloud"])
        self.config.set("hotkey_screen_read", DEFAULTS["hotkey_screen_read"])
        self.hk_dictate.set_hotkey(DEFAULTS["hotkey_record"])
        self.hk_read.set_hotkey(DEFAULTS["hotkey_read_aloud"])
        self.hk_ocr.set_hotkey(DEFAULTS["hotkey_screen_read"])
        kb.unhook_all()
        self._setup_hotkeys()
        self._update_dictation_hint()
        self._update_status("Hotkeys reset to defaults")

    def _reset_hotkey_button_style(self, btn):
        btn.setStyleSheet(
            "QPushButton { background: #181825; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 4px 8px; color: #89b4fa; "
            "font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { border-color: #89b4fa; }"
        )

    @Slot(bool)
    def _on_dictation_toggle(self, checked):
        self._dictation_active = checked
        self.config.set("dictation_mode", checked)
        self._update_dictation_button()
        self._update_dictation_hint()
        state = "ON" if checked else "OFF"
        self._update_status(f"Dictation mode {state}")

    def _update_dictation_button(self):
        if self._dictation_active:
            self.btn_dictation.setText("  DICTATION MODE: ON")
        else:
            self.btn_dictation.setText("  DICTATION MODE: OFF")

    def _update_dictation_hint(self):
        hk = self.config["hotkey_record"]
        if self._dictation_active:
            self.dictation_hint.setText(
                f"Hold {hk} and speak — release to paste where your cursor is"
            )
        else:
            self.dictation_hint.setText(
                "Text goes to the panel below only"
            )

    @Slot()
    def _on_voice_change(self):
        voice_id = self.voice_combo.currentData()
        if voice_id:
            self.tts.set_voice(voice_id)
            self.config.set("tts_voice", voice_id)
            self._update_status(f"Voice: {self.voice_combo.currentText()}")

    @Slot(int)
    def _on_speed_change(self, value):
        speed = value / 100.0
        self.speed_label.setText(f"{speed:.2f}x")
        self.tts.set_speed(speed)  # live change via VLC!
        self.config.set("tts_speed", speed)

    @Slot()
    def _on_settings(self):
        dlg = SettingsDialog(self.config, self.tts, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            # Audio device
            new_dev = vals.get("audio_device", -1)
            self.config.set("audio_device", new_dev)
            self.recorder.device = new_dev if new_dev >= 0 else None

            # Apply and save settings
            if vals["whisper_model"] != self.config["whisper_model"]:
                self.config.set("whisper_model", vals["whisper_model"])
                self.model_combo.setCurrentText(vals["whisper_model"])
                self.transcriber.change_model(vals["whisper_model"])

            self.config.set("whisper_language", vals["whisper_language"])
            self.transcriber.language = vals["whisper_language"]

            self.config.set("font_size", vals["font_size"])
            self.text_output.setFont(QFont("Cascadia Code", vals["font_size"]))

            self.config.set("always_on_top", vals["always_on_top"])
            self.config.set("start_with_windows", vals["start_with_windows"])
            self.config.set("start_minimized", vals["start_minimized"])
            self.config.set("light_cleanup", vals["light_cleanup"])
            set_start_with_windows(vals["start_with_windows"])
            self._apply_window_flags()

            # Hotkey changes — re-register
            old_record = self.config["hotkey_record"]
            old_screen = self.config["hotkey_screen_read"]
            old_read = self.config["hotkey_read_aloud"]
            new_record = vals["hotkey_record"]
            new_screen = vals["hotkey_screen_read"]
            new_read = vals["hotkey_read_aloud"]

            if new_record != old_record or new_screen != old_screen or new_read != old_read:
                try:
                    kb.unhook_all()
                except Exception:
                    pass
                self.config.set("hotkey_record", new_record)
                self.config.set("hotkey_read_aloud", new_read)
                self.config.set("hotkey_screen_read", new_screen)
                self._setup_hotkeys()
                self.btn_record.setToolTip(f"Start recording ({new_record})")
                self.btn_cursor_read.setToolTip(f"Read at cursor ({new_screen})")
                self._update_status(f"Hotkeys updated: Record={new_record}, Screen={new_screen}")

            self.config.save()

    @Slot(str)
    def _on_model_change(self, model_name):
        if model_name != self.transcriber.model_size:
            self.config.set("whisper_model", model_name)
            self.transcriber.change_model(model_name)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    def _append_output(self, text, prefix=""):
        cursor = self.text_output.textCursor()
        if self.text_output.toPlainText().strip():
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText("\n\n")

        if prefix:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#89b4fa"))
            fmt.setFontWeight(QFont.Weight.Bold)
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(f"{prefix}  ", fmt)

        fmt_normal = QTextCharFormat()
        fmt_normal.setForeground(QColor("#cdd6f4"))
        cursor.insertText(text, fmt_normal)

        self.text_output.setTextCursor(cursor)
        self.text_output.ensureCursorVisible()

    def _update_status(self, msg):
        self.status_bar.showMessage(msg)

    @Slot(str)
    def _on_error(self, msg):
        self._update_status(f"Error: {msg}")
        self._append_output(msg, prefix="[Error]")
        self.indicator.show_idle()

    def _setup_show_request_timer(self):
        self._show_request_timer = QTimer(self)
        self._show_request_timer.timeout.connect(self._poll_show_request)
        self._show_request_timer.start(500)

    def _poll_show_request(self):
        if not _show_window_event:
            return
        WAIT_OBJECT_0 = 0
        result = ctypes.windll.kernel32.WaitForSingleObject(_show_window_event, 0)
        if result == WAIT_OBJECT_0:
            self.show_normal()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        menu = QMenu(self)
        show_action = QAction("Show Voice Assistant", self)
        show_action.triggered.connect(self.show_normal)
        menu.addAction(show_action)
        pause_action = QAction("Pause Dictation", self)
        pause_action.setCheckable(True)
        pause_action.setChecked(not self._dictation_active)
        pause_action.toggled.connect(lambda paused: self.btn_dictation.setChecked(not paused))
        menu.addAction(pause_action)
        stop_action = QAction("Stop Reading", self)
        stop_action.triggered.connect(self.tts.stop)
        menu.addAction(stop_action)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_normal()

    def show_normal(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._force_quit = True
        self.close()

    def _apply_window_flags(self):
        was_visible = self.isVisible()
        flags = self.windowFlags()
        if self.config["always_on_top"]:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def closeEvent(self, event):
        if not self._force_quit and self.tray.isVisible():
            event.ignore()
            self.hide()
            self._update_status("Still running in the tray")
            return
        try:
            kb.unhook_all()
        except Exception:
            pass
        if _show_window_event:
            try:
                ctypes.windll.kernel32.CloseHandle(_show_window_event)
            except Exception:
                pass
        if _single_instance_mutex:
            try:
                ctypes.windll.kernel32.CloseHandle(_single_instance_mutex)
            except Exception:
                pass
        self.config.save()
        event.accept()


def main():
    if not acquire_single_instance_lock():
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Voice Assistant")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)

    window = MainWindow()
    if "--minimized" in sys.argv or window.config.get("start_minimized", True):
        window.hide()
    else:
        window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
