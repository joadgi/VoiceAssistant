"""Reusable Qt widgets: the hotkey-capture pill and the floating indicator."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QWidget


class HotkeyCaptureWidget(QFrame):
    """A pill-shaped widget that captures any key combo when clicked.

    Click it → border turns red, shows "press keys..."
    Press any combo (1, 2, 3+ keys) → captured and saved
    Click elsewhere or press Escape → cancel

    This is the ONE hotkey-capture path (two earlier duplicate implementations
    were removed — keep it that way). Hotkeys are fully user-configurable;
    validation happens in config.validate_hotkey, not here.
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


class RecordingIndicator(QWidget):
    """Always-visible floating pill that shows dictation state and can be
    clicked to start/stop recording. Drag it anywhere on the desktop.

    Never steals focus (WA_ShowWithoutActivating + WindowDoesNotAcceptFocus)
    so clicking it leaves the target window focused for the paste."""

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
        self.setToolTip("Click to start/stop dictation  •  right-click for menu  •  drag to move")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Right-click menu makes the pill a self-sufficient primary surface —
        # the window builds the menu (see MainWindow._on_pill_menu).
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

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

    def show_pasting(self):
        self._apply("#89b4fa", "#89b4fa", "Pasting…", "#89b4fa")

    def show_done(self):
        self._apply("#a6e3a1", "#a6e3a1", "Pasted ✓", "#a6e3a1")
        QTimer.singleShot(1500, self.show_idle)

    def show_error(self, text="Paste failed — text in panel"):
        self._apply("#fab387", "#fab387", text, "#fab387")
        QTimer.singleShot(2500, self.show_idle)

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
