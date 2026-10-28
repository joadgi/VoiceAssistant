"""Reusable Qt widgets: the hotkey-capture pill and the floating indicator."""

import html

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from . import theme
from .theme import C


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

    def _render(self, value, value_color):
        """Bridge chip text: the action as a muted caption, the binding in colour."""
        self._text.setText(
            f'<span style="color:{C["text_muted"]};font-weight:700;">{html.escape(self._label.upper())}</span>'
            f'&nbsp;&nbsp;<span style="color:{value_color};font-weight:700;">{html.escape(value)}</span>'
        )

    def _update_display(self):
        if self._capturing:
            self._render("press any keys…", C["orange"])
        else:
            self._render(self._hotkey, C["cyan"])

    def _set_idle_style(self):
        self.setStyleSheet(
            f"HotkeyCaptureWidget {{ background: {C['control']}; border: 1px solid {C['border']}; "
            f"border-radius: {theme.px(6)}px; }}"
            f"HotkeyCaptureWidget:hover {{ background: {C['control_hover']}; border-color: {C['cyan']}; }}"
            f"QLabel {{ font-size: {theme.px(11)}px; background: transparent; border: none; }}"
        )

    def _set_capture_style(self):
        self.setStyleSheet(
            f"HotkeyCaptureWidget {{ background: {C['bg']}; border: 2px solid {C['orange']}; "
            f"border-radius: {theme.px(6)}px; }}"
            f"QLabel {{ font-size: {theme.px(11)}px; background: transparent; border: none; }}"
        )

    def rescale(self):
        """Re-apply the current style at the current zoom (sizes come from theme.px)."""
        if self._capturing:
            self._set_capture_style()
        else:
            self._set_idle_style()

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
            self._render(f"{'+'.join(mods)}+…  release to save", C["orange"])
        else:
            self._render("press any keys…", C["orange"])

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
        # event.modifiers() does NOT reliably include the modifier key being
        # pressed right now (Qt often reports the state BEFORE this key), so a
        # modifier-only combo like ctrl+alt could otherwise register as just
        # "ctrl" and be refused. Fold in the pressed modifier key explicitly.
        _MODKEY = {
            Qt.Key.Key_Control: "ctrl", Qt.Key.Key_Shift: "shift",
            Qt.Key.Key_Alt: "alt", Qt.Key.Key_Meta: "windows",
        }
        if key in _MODKEY and _MODKEY[key] not in mods:
            mods = mods + [_MODKEY[key]]
        self._held_mods = mods  # remember current set for release handling

        # If this is a modifier key itself, update the display and wait for more
        if key in _MODKEY:
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
        # A plain QWidget only paints a style-sheet background/border with this set;
        # without it the card's border never drew (and leaked onto the label instead).
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Don't steal focus from the window the user is dictating into.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.setFixedSize(self.COMPACT_W, self.COMPACT_H)
        self.setToolTip("Click to start/stop dictation  •  right-click for menu  •  drag to move")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Right-click menu makes the pill a self-sufficient primary surface —
        # the window builds the menu (see MainWindow._on_pill_menu).
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        outer = QVBoxLayout(self)

        # Live-preview caption: the rolling draft of what is being said. Hidden
        # (and the pill compact) until the first draft arrives. Words the last
        # two decodes agreed on are bright; the still-moving tail is dimmed.
        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setTextFormat(Qt.TextFormat.RichText)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self._preview.hide()
        outer.addWidget(self._preview)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self._dot = QLabel()
        row.addWidget(self._dot)

        self._label = QLabel("Ready")
        row.addWidget(self._label)
        row.addStretch()
        outer.addLayout(row)

        self._positioned = False  # auto-place once, then respect user drags
        self._drag_offset = None
        self._preview_text = ""   # plain text of the current draft ("" = none)
        self._state = None        # (border, dot, text, text_color); None = idle
        self.rescale()
        self._set_idle()

    # Compact pill vs. expanded caption card. The card keeps the pill's
    # bottom-right corner fixed and grows up/left, so a pill parked in the
    # corner never walks off the screen. Sizes are written at 100% and follow
    # the app zoom (theme.px), like every other size in the UI.
    @property
    def COMPACT_W(self):
        return theme.px(200)

    @property
    def COMPACT_H(self):
        return theme.px(40)

    @property
    def CARD_W(self):
        return theme.px(460)

    # Roughly three lines at 14px across CARD_W; older words scroll off the
    # front so the newest words — the ones still moving — stay visible. The
    # font and the card scale together, so the count holds at every zoom.
    PREVIEW_CHARS = 170

    def rescale(self):
        """Follow the app zoom: re-apply sizes and styles, keep the current state."""
        px = theme.px
        self.layout().setContentsMargins(px(12), px(6), px(12), px(6))
        self.layout().setSpacing(px(4))
        # `border: none` is load-bearing: the pill sets its border on the
        # widget itself, and Qt style sheets propagate that to children — so
        # without this the caption gets its own box drawn around it.
        self._preview.setStyleSheet(
            f"color: {C['text']}; font-size: {px(14)}px; border: none;"
            f"background-color: {C['bg']}; border-radius: {px(8)}px; padding: {px(7)}px {px(8)}px;"
        )
        self._preview.setFixedWidth(self.CARD_W - px(24))
        self._paint()
        if self._preview_text:
            self._fit_card()
        else:
            self._resize_anchored(self.COMPACT_W, self.COMPACT_H)

    def preview_text(self):
        """Plain text of the draft currently shown ("" when collapsed)."""
        return self._preview_text

    def show_preview(self, stable, tail):
        """Render the live draft: `stable` bright, `tail` dimmed."""
        stable = (stable or "").strip()
        tail = (tail or "").strip()
        full = " ".join(p for p in (stable, tail) if p)
        if not full:
            self.clear_preview()
            return
        self._preview_text = full
        # Trim from the FRONT so the newest words stay on screen.
        overflow = len(full) - self.PREVIEW_CHARS
        if overflow > 0:
            cut = overflow + 1
            if len(stable) >= cut:
                stable = "…" + stable[cut:].lstrip()
            else:
                cut -= len(stable) + 1
                stable = ""
                tail = "…" + tail[max(0, cut):].lstrip()
        parts = []
        if stable:
            parts.append(html.escape(stable))
        if tail:
            parts.append(f'<span style="color:{C["text_muted"]};">{html.escape(tail)}</span>')
        self._preview.setText(" ".join(parts))
        if self._preview.isHidden():
            self._preview.show()
        self._fit_card()

    def _fit_card(self):
        # heightForWidth on a styled QLabel does not always include the style
        # sheet's padding, so take the larger of the two measurements — a card
        # one pixel short clips the last line of the draft.
        height = max(self._preview.heightForWidth(self._preview.width()),
                     self._preview.sizeHint().height())
        self._resize_anchored(self.CARD_W, self.COMPACT_H + height + theme.px(4))

    def clear_preview(self):
        if not self._preview_text and self._preview.isHidden():
            return
        self._preview_text = ""
        self._preview.clear()
        self._preview.hide()
        self._resize_anchored(self.COMPACT_W, self.COMPACT_H)

    def _resize_anchored(self, w, h):
        """Resize keeping the bottom-right corner where it is, clamped on-screen."""
        if self.width() == w and self.height() == h:
            return
        geo = self.frameGeometry()
        right, bottom = geo.right(), geo.bottom()
        self.setFixedSize(w, h)
        x, y = right - w + 1, bottom - h + 1
        screen = QApplication.screenAt(geo.center()) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - w + 1))
            y = max(avail.top(), min(y, avail.bottom() - h + 1))
        self.move(x, y)

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
        self._state = (border, dot, text, text_color)
        self._paint()
        self._position_bottom_right()
        self.show()
        self.raise_()

    def show_recording(self):
        self._apply(C["red"], C["red"], "Recording", C["red"])   # the dot is the bullet

    def show_transcribing(self):
        self._apply(C["yellow"], C["yellow"], "Transcribing…", C["yellow"])

    def show_pasting(self):
        self._apply(C["cyan"], C["cyan"], "Pasting…", C["cyan"])

    def show_done(self):
        self._apply(C["green"], C["green"], "Pasted ✓", C["green"])
        QTimer.singleShot(1500, self.show_idle)

    def show_error(self, text="Paste failed — text in panel"):
        self.clear_preview()
        self._apply(C["orange"], C["orange"], text, C["orange"])
        QTimer.singleShot(2500, self.show_idle)

    def show_idle(self):
        self._set_idle()
        self._position_bottom_right()
        self.show()
        self.raise_()

    def _set_idle(self):
        self.clear_preview()
        self._state = None
        self._paint()

    def _paint(self):
        """Style the pill for its current state at the current zoom."""
        px = theme.px
        if self._state is None:
            border, dot, text, color = C["border"], C["text_muted"], "Ready — click to dictate", C["text_dim"]
            width, size = 1, 12
        else:
            border, dot, text, color = self._state
            width, size = 2, 13
        # Scoped to the pill itself: an unscoped rule also boxed every child label.
        self.setStyleSheet(f"RecordingIndicator {{ background-color: {C['card']}; "
                           f"border: {px(width)}px solid {border}; border-radius: {px(10)}px; }}")
        # Qt drops the rounding entirely when the radius exceeds half the size,
        # so the dot's radius is derived from its scaled size, not scaled itself.
        d = px(14)
        self._dot.setFixedSize(d, d)
        self._dot.setStyleSheet(f"background-color: {dot}; border-radius: {d // 2}px; border: none;")
        self._label.setText(text)
        self._label.setStyleSheet(f"color: {color}; font-weight: bold; font-size: {px(size)}px; "
                                  "border: none; background: transparent;")
