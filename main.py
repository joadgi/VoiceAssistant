"""Voice Assistant — local voice-to-text + screen reader desktop app."""

import sys
import os
import ctypes
import time
import threading
import numpy as np
import pyperclip
import keyboard as kb

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QComboBox, QSlider, QGroupBox,
    QStatusBar, QSystemTrayIcon, QMenu, QDialog, QFormLayout,
    QDialogButtonBox, QCheckBox, QSpinBox, QProgressBar, QSplitter,
    QLineEdit, QFrame,
)
from PySide6.QtCore import Qt, QTimer, Slot, QSize, Signal, QEvent
from PySide6.QtGui import QIcon, QFont, QAction, QColor, QTextCharFormat
import sounddevice as sd

from config import Config
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


class HotkeyCaptureDialog(QDialog):
    """Modal dialog that captures a keyboard shortcut via Qt's native key events.

    Full control — allows:
    - Any modifier combo + key (ctrl+alt+shift+letter, etc.)
    - Function keys alone (f1-f12)
    - Windows/Meta key combos
    - Manual text entry as a backup
    """

    SPECIAL_KEYS = {
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

    def __init__(self, action_name, current_hotkey, parent=None):
        super().__init__(parent)
        self.captured_hotkey = current_hotkey
        self._action_name = action_name
        self.setWindowTitle(f"Set {action_name} Hotkey")
        self.setModal(True)
        self.setFixedSize(520, 340)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel(f"Set {action_name} Hotkey")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #cdd6f4;")
        layout.addWidget(title)

        self.instruction = QLabel("Press your hotkey combo  —  OR  —  type it below")
        self.instruction.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.instruction.setStyleSheet("font-size: 13px; color: #f38ba8; font-weight: bold;")
        layout.addWidget(self.instruction)

        self.display = QLabel(current_hotkey or "(no combo yet)")
        self.display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.display.setStyleSheet(
            "font-size: 22px; color: #89b4fa; font-weight: bold; "
            "background: #181825; border: 1px solid #45475a; "
            "border-radius: 6px; padding: 14px;"
        )
        layout.addWidget(self.display)

        # Manual entry fallback
        manual_label = QLabel("Or type the combo string directly:")
        manual_label.setStyleSheet("color: #6c7086; font-size: 11px;")
        layout.addWidget(manual_label)

        self.manual_entry = QLineEdit()
        self.manual_entry.setPlaceholderText("e.g. ctrl+shift+f9, alt+d, windows+j, f11")
        self.manual_entry.setText(current_hotkey or "")
        self.manual_entry.setStyleSheet(
            "QLineEdit { background: #181825; color: #cdd6f4; "
            "border: 1px solid #45475a; border-radius: 4px; padding: 6px 10px; "
            "font-family: Consolas, monospace; }"
        )
        layout.addWidget(self.manual_entry)

        hint = QLabel(
            "ANY combo works — one key, two keys, three keys, any combination.\n"
            "Press Escape alone to cancel. Click Save when you're happy with what's shown."
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #6c7086; font-size: 10px;")
        layout.addWidget(hint)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.save_btn = QPushButton("Save Typed Hotkey")
        self.save_btn.clicked.connect(self._save_manual)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus()

    def _save_manual(self):
        """Use whatever's in the text field."""
        text = self.manual_entry.text().strip().lower()
        if text:
            self.captured_hotkey = text
            self.accept()

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        # If the user is typing in the manual entry field, don't intercept
        if self.manual_entry.hasFocus():
            return super().keyPressEvent(event)

        # Escape with no modifiers cancels
        if key == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:
            self.captured_hotkey = None
            self.reject()
            return

        # Ignore bare modifier keys — wait for the real key
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return

        parts = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("ctrl")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("shift")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("alt")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("windows")

        # Resolve key name
        key_name = ""
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            key_name = chr(key).lower()
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            key_name = chr(key)
        else:
            key_name = self.SPECIAL_KEYS.get(key, "")

        if key_name:
            combo = "+".join(parts + [key_name])
            self.captured_hotkey = combo
            self.display.setText(combo)
            self.manual_entry.setText(combo)
            self.instruction.setText("✓ Captured — saving...")
            self.instruction.setStyleSheet(
                "font-size: 13px; color: #a6e3a1; font-weight: bold;"
            )
            # Auto-accept after a short confirmation delay
            QTimer.singleShot(500, self.accept)


class HotkeyButton(QPushButton):
    """A button that captures keyboard shortcuts using the keyboard library directly."""

    _hotkey_captured = Signal(str)

    def __init__(self, current_hotkey="", parent=None):
        super().__init__(parent)
        self._hotkey = current_hotkey
        self._capturing = False
        self._set_idle()
        self.clicked.connect(self._start_capture)
        self._hotkey_captured.connect(self._on_captured)

    def hotkey(self):
        return self._hotkey

    def set_hotkey(self, combo):
        self._hotkey = combo
        self._set_idle()

    def _set_idle(self):
        label = self._hotkey if self._hotkey else "Click to set..."
        self.setText(f"  {label}")
        self.setStyleSheet(
            "QPushButton { background: #181825; border: 1px solid #45475a; "
            "border-radius: 4px; padding: 8px 14px; color: #89b4fa; "
            "font-weight: bold; font-size: 13px; text-align: left; }"
            "QPushButton:hover { border-color: #89b4fa; }"
        )

    def _start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self.setText("  Press your shortcut combo now...")
        self.setStyleSheet(
            "QPushButton { background: #181825; border: 2px solid #f38ba8; "
            "border-radius: 4px; padding: 8px 14px; color: #f38ba8; "
            "font-weight: bold; font-size: 13px; text-align: left; }"
        )
        # Use the keyboard library to read the next hotkey — runs in a thread
        import threading
        thread = threading.Thread(target=self._capture_thread, daemon=True)
        thread.start()

    def _capture_thread(self):
        """Blocking call to keyboard.read_hotkey() in a background thread."""
        try:
            combo = kb.read_hotkey(suppress=False)
            # Normalize: keyboard lib returns things like "ctrl+shift+r"
            self._hotkey_captured.emit(combo)
        except Exception:
            self._hotkey_captured.emit("")

    @Slot(str)
    def _on_captured(self, combo):
        self._capturing = False
        if combo and combo != "escape":
            combo_lower = combo.lower()
            # Validate: must have at least one non-modifier key
            parts = [p.strip() for p in combo_lower.split("+")]
            modifiers = {"ctrl", "shift", "alt", "windows", "cmd", "meta"}
            non_modifier_parts = [p for p in parts if p not in modifiers]
            if non_modifier_parts:
                self._hotkey = combo_lower
            # else: ignore — modifier-only combos aren't valid hotkeys
        self._set_idle()


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
            # Hotkeys are no longer edited here — keep current values
            "hotkey_record": self.config["hotkey_record"],
            "hotkey_read_aloud": self.config["hotkey_read_aloud"],
            "hotkey_screen_read": self.config["hotkey_screen_read"],
        }


# ---------------------------------------------------------------------------
# Windows API helpers for dictation paste
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32


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
_last_paste_end_time = [0.0]  # using list for mutability across function calls
_debug_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")


def _dbg(msg):
    try:
        with open(_debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


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
    """Focus a window and paste text. Guarded against re-entry AND rapid consecutive calls."""
    title = _window_title(hwnd)
    fg = user32.GetForegroundWindow()
    fg_title = _window_title(fg)
    _dbg(f"paste_to_window ENTER  target_hwnd={hwnd} title={title!r}  current_fg={fg} title={fg_title!r}")

    since_last = time.time() - _last_paste_end_time[0]
    if since_last < 1.0:
        _dbg(f"paste_to_window REJECTED — only {since_last:.2f}s since last paste")
        return False

    if not _paste_lock.acquire(blocking=False):
        _dbg("paste_to_window REJECTED — lock held")
        return False
    try:
        pyperclip.copy(text)
        _dbg(f"  clipboard set to {text!r}")

        # Wait until user has released modifier keys first
        for i in range(100):
            if not (kb.is_pressed("ctrl") or kb.is_pressed("shift") or kb.is_pressed("alt")
                    or kb.is_pressed("windows")):
                _dbg(f"  mods released after {i*20}ms")
                break
            time.sleep(0.02)

        # ONLY press Escape if the current foreground window is NOT the target
        # (meaning something else — probably Start menu — stole focus)
        current_fg = user32.GetForegroundWindow()
        if current_fg != hwnd:
            _dbg(f"  focus is on {current_fg} (title={_window_title(current_fg)!r}), pressing Esc to close intruder")
            VK_ESCAPE = 0x1B
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_ESCAPE, 0, 0, 0)
            user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
            if not set_foreground_window(hwnd):
                _dbg("  set_foreground_window FAILED")
                return False
            time.sleep(0.12)
        else:
            _dbg(f"  focus already on target window, NOT pressing Esc")
            # Still need a small delay so any keystate settles
            time.sleep(0.05)

        _win32_paste()
        _last_paste_end_time[0] = time.time()
        _dbg("paste_to_window DONE")
        return True
    finally:
        time.sleep(0.3)
        _paste_lock.release()


# ---------------------------------------------------------------------------
# Floating recording indicator — small always-on-top pill
# ---------------------------------------------------------------------------
class RecordingIndicator(QWidget):
    """Tiny floating widget that shows recording / transcribing state."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(220, 40)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)

        self._dot = QLabel()
        self._dot.setFixedSize(14, 14)
        lay.addWidget(self._dot)

        self._label = QLabel("Ready")
        self._label.setStyleSheet("color: #cdd6f4; font-weight: bold; font-size: 13px;")
        lay.addWidget(self._label)
        lay.addStretch()

        self._set_idle()

    def _position_bottom_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 20, screen.bottom() - self.height() - 60)

    def show_recording(self):
        self.setStyleSheet("background-color: #1e1e2e; border: 2px solid #f38ba8; border-radius: 8px;")
        self._dot.setStyleSheet(
            "background-color: #f38ba8; border-radius: 7px; border: none;"
        )
        self._label.setText("Recording...")
        self._label.setStyleSheet("color: #f38ba8; font-weight: bold; font-size: 13px;")
        self._position_bottom_right()
        self.show()
        self.raise_()

    def show_transcribing(self):
        self.setStyleSheet("background-color: #1e1e2e; border: 2px solid #f9e2af; border-radius: 8px;")
        self._dot.setStyleSheet(
            "background-color: #f9e2af; border-radius: 7px; border: none;"
        )
        self._label.setText("Transcribing...")
        self._label.setStyleSheet("color: #f9e2af; font-weight: bold; font-size: 13px;")
        self.show()
        self.raise_()

    def show_done(self):
        self.setStyleSheet("background-color: #1e1e2e; border: 2px solid #a6e3a1; border-radius: 8px;")
        self._dot.setStyleSheet(
            "background-color: #a6e3a1; border-radius: 7px; border: none;"
        )
        self._label.setText("Pasted")
        self._label.setStyleSheet("color: #a6e3a1; font-weight: bold; font-size: 13px;")
        self.show()
        QTimer.singleShot(1500, self.hide)

    def _set_idle(self):
        self.setStyleSheet("background-color: #1e1e2e; border: 1px solid #45475a; border-radius: 8px;")
        self._dot.setStyleSheet(
            "background-color: #45475a; border-radius: 7px; border: none;"
        )
        self._label.setText("Ready")
        self._label.setStyleSheet("color: #6c7086; font-weight: bold; font-size: 13px;")


class MainWindow(QMainWindow):
    # Thread-safe signals for global hotkey callbacks
    _sig_hotkey_press = Signal()
    _sig_hotkey_release = Signal()
    _sig_hotkey_screen = Signal()
    _sig_hotkey_read = Signal()
    _sig_inline_hk = Signal(str, str, str, int)  # config_key, label, combo, button_id

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
        self._sig_inline_hk.connect(self._on_inline_hk_captured)

        self._setup_hotkeys()
        self._apply_window_flags()

        # Load models in background
        self.transcriber.load_model()
        self.ocr.load_model()

        self._update_status("Starting up...")

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

    # -----------------------------------------------------------------------
    # Hotkeys (global)
    # -----------------------------------------------------------------------
    def _setup_hotkeys(self):
        try:
            kb.unhook_all()
        except Exception:
            pass

        # Normalize hotkeys to lowercase — keyboard lib is case-sensitive
        hk_record = self.config["hotkey_record"].lower()
        hk_screen = self.config["hotkey_screen_read"].lower()
        self.config.set("hotkey_record", hk_record)
        self.config.set("hotkey_screen_read", hk_screen)
        errors = []

        # Push-to-talk: press combo to start, release trigger key to stop
        # DON'T suppress — we need the release event for push-to-talk
        try:
            kb.add_hotkey(hk_record, lambda: self._sig_hotkey_press.emit())
            # Extract the trigger key (last part of combo)
            trigger_key = hk_record.split("+")[-1].strip()
            try:
                kb.on_release_key(trigger_key, lambda e: self._sig_hotkey_release.emit())
            except Exception:
                pass
        except Exception as e:
            errors.append(f"Record hotkey ({hk_record}): {e}")

        try:
            kb.add_hotkey(hk_screen, lambda: self._sig_hotkey_screen.emit())
        except Exception as e:
            errors.append(f"Screen hotkey ({hk_screen}): {e}")

        hk_read = self.config["hotkey_read_aloud"].lower()
        self.config.set("hotkey_read_aloud", hk_read)
        # Suppress the keys if the hotkey contains the Windows key so it
        # doesn't open the Start menu when released.
        needs_suppress_read = "windows" in hk_read
        try:
            kb.add_hotkey(hk_read, lambda: self._sig_hotkey_read.emit(),
                          suppress=needs_suppress_read)
        except Exception as e:
            # Try without suppress as fallback
            try:
                kb.add_hotkey(hk_read, lambda: self._sig_hotkey_read.emit())
            except Exception as e2:
                errors.append(f"Read aloud hotkey ({hk_read}): {e2}")

        if errors:
            self._update_status("Hotkey errors: " + "; ".join(errors))
        else:
            self._update_status(f"Hotkeys: {hk_record}=dictate  {hk_read}=read selection  {hk_screen}=OCR")

    @Slot()
    def _hotkey_press_handler(self):
        """Hotkey pressed — start recording."""
        _dbg(f"_hotkey_press_handler: ptt={self._ptt_active}  recording={self.recorder.is_recording}")
        if not self.recorder.is_recording and not self._ptt_active:
            self._ptt_active = True
            self._on_record_from_hotkey()

    @Slot()
    def _hotkey_release_handler(self):
        """Trigger key released — stop recording if push-to-talk is active."""
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

        if len(audio) > 0:
            self._update_status(f"Transcribing {duration:.1f}s (peak {max_amp:.3f})...")
            self.indicator.show_transcribing()
            self.transcriber.transcribe(audio)
        else:
            self._update_status("No audio captured")
            self.indicator.hide()

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
        _dbg(f"_on_transcription_ready FIRED  text={text!r}")
        # --- 1. Clean up Whisper duplicates (same phrase repeated back-to-back) ---
        text = self._dedupe_repeated(text)

        # --- 2. Skip if we just emitted identical text within the last 3 seconds ---
        now = time.time()
        if text == self._last_transcription and (now - self._last_transcription_time) < 3.0:
            _dbg(f"  → duplicate, ignoring")
            self.indicator.hide()
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

        if (self._dictation_active and self._target_hwnd and not is_own_window
                and text.strip() and "[No speech" not in text):
            success = paste_to_window(self._target_hwnd, text)
            if success:
                self.indicator.show_done()
                self._update_status("Transcribed and pasted")
                self._target_hwnd = None
                return
            else:
                self.indicator.hide()
                self._update_status("Transcribed (target window unavailable, text in panel)")
        else:
            self.indicator.hide()
            self._update_status("Transcription complete")

        self._append_output(text, prefix="[Voice]")
        self._target_hwnd = None

    def _dedupe_repeated(self, text):
        """Collapse immediate duplicates like 'Hello world. Hello world.' → 'Hello world.'"""
        if not text or len(text) < 10:
            return text
        # Check if the text is exactly "X X" where X is the same content
        stripped = text.strip()
        length = len(stripped)
        # Try splitting at the midpoint (with small tolerance for punctuation differences)
        for split in (length // 2 - 1, length // 2, length // 2 + 1):
            if split <= 0 or split >= length:
                continue
            left = stripped[:split].strip().rstrip(".,!?;:").lower()
            right = stripped[split:].strip().rstrip(".,!?;:").lower()
            if left and left == right:
                return stripped[:split].strip()
        return text

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

    @Slot(str, str, str, int)
    def _on_inline_hk_captured(self, config_key, label, combo, button_id):
        """Called in main thread after inline hotkey capture completes."""
        btn_map = {
            id(self.hk_btn_record): self.hk_btn_record,
            id(self.hk_btn_read): self.hk_btn_read,
            id(self.hk_btn_screen): self.hk_btn_screen,
        }
        button = btn_map.get(button_id)
        if not button:
            # Still need to re-register global hotkeys
            self._setup_hotkeys()
            return

        # Validate: must have modifier + key, not match another hotkey
        combo_lower = (combo or "").lower()
        parts = [p.strip() for p in combo_lower.split("+")]
        modifiers = {"ctrl", "shift", "alt", "windows", "cmd", "meta"}
        non_mod = [p for p in parts if p and p not in modifiers]

        is_valid = (
            combo_lower
            and combo_lower != "escape"
            and non_mod
            and len(parts) >= 2
        )

        # Check for conflicts with other hotkeys
        other_keys = [k for k in ("hotkey_record", "hotkey_read_aloud", "hotkey_screen_read")
                      if k != config_key]
        conflict = is_valid and any(self.config[k] == combo_lower for k in other_keys)

        if is_valid and not conflict:
            self.config.set(config_key, combo_lower)
            button.setText(f"  {label}: {combo_lower}  ")
            self._update_status(f"{label} hotkey set to {combo_lower}")
        else:
            current = self.config[config_key]
            button.setText(f"  {label}: {current}  ")
            if conflict:
                self._update_status(f"{combo_lower} is already used by another hotkey")
            elif combo_lower and not non_mod:
                self._update_status("Need modifier + key (e.g., Ctrl+Shift+F9)")
            else:
                self._update_status("Hotkey change cancelled — kept existing")

        self._reset_hotkey_button_style(button)
        # ALWAYS re-register all hotkeys — capture temporarily unhooked them
        self._setup_hotkeys()
        self._update_dictation_hint()

    def _save_hotkey(self, config_key, label, combo):
        """Called when a HotkeyCaptureWidget captures a new combo."""
        combo = (combo or "").lower().strip()
        if not combo:
            return
        self.config.set(config_key, combo)
        # Unhook old hotkeys and re-register all
        try:
            kb.unhook_all()
        except Exception:
            pass
        self._ptt_active = False
        self._read_in_flight = False
        self._setup_hotkeys()
        self._update_dictation_hint()
        self._update_status(f"{label} hotkey set to {combo}")

    def _change_hotkey_inline(self, config_key, label, button):
        """Inline edit: replace the pill with a text field. Type anything. Enter saves, Esc cancels."""
        try:
            kb.unhook_all()
        except Exception:
            pass
        self._ptt_active = False
        self._read_in_flight = False

        current = self.config[config_key]

        # Find the position of the button in its parent layout
        parent_layout = button.parent().layout()
        index = None
        for i in range(parent_layout.count()):
            item = parent_layout.itemAt(i)
            if item and item.widget() is button:
                index = i
                break
        if index is None:
            self._setup_hotkeys()
            return

        # Replace the button with a QLineEdit
        edit = QLineEdit(current)
        edit.setToolTip("Type any combo (e.g. a, f9, ctrl+shift+q, alt+b). Enter=save  Esc=cancel")
        edit.setStyleSheet(
            "QLineEdit { background: #181825; color: #f9e2af; "
            "border: 2px solid #f38ba8; border-radius: 6px; padding: 6px 12px; "
            "font-weight: bold; font-size: 11px; font-family: Consolas, monospace; }"
        )
        edit.setFixedWidth(button.sizeHint().width() + 40)
        edit.selectAll()

        parent_layout.removeWidget(button)
        button.hide()
        parent_layout.insertWidget(index, edit)

        self._update_status(f"Editing {label} hotkey — type it, Enter saves, Esc cancels")

        def finish(save):
            new_combo = edit.text().strip().lower() if save else ""
            # Remove edit widget, restore button
            idx = None
            for i in range(parent_layout.count()):
                if parent_layout.itemAt(i).widget() is edit:
                    idx = i
                    break
            if idx is not None:
                parent_layout.removeWidget(edit)
            edit.deleteLater()

            if save and new_combo:
                self.config.set(config_key, new_combo)
                button.setText(f"  {label}: {new_combo}  ")
                self._update_status(f"{label} hotkey set to: {new_combo}")
            else:
                button.setText(f"  {label}: {current}  ")
                self._update_status(f"{label} hotkey unchanged")

            self._reset_hotkey_button_style(button)
            if idx is not None:
                parent_layout.insertWidget(idx, button)
            button.show()
            self._setup_hotkeys()
            self._update_dictation_hint()

        edit.returnPressed.connect(lambda: finish(True))

        # Esc cancels — use a key event filter on the edit
        original_keypress = edit.keyPressEvent
        def edit_keypress(event):
            if event.key() == Qt.Key.Key_Escape:
                finish(False)
                return
            original_keypress(event)
        edit.keyPressEvent = edit_keypress

        edit.setFocus()

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
        self.indicator.hide()

    def _apply_window_flags(self):
        flags = self.windowFlags()
        if self.config["always_on_top"]:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def closeEvent(self, event):
        # Clean up hotkeys
        try:
            kb.unhook_all()
        except Exception:
            pass
        self.config.save()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Voice Assistant")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
