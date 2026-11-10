"""The app's look: Bridge's design system, dark and light, translated to Qt.

Bridge (project-cloud-dashboard) is the reference: same palettes, type scale, radii and
component language, so the two tools read as one family. The tokens below are copied
from Bridge's `:root` / `[data-theme="dark"]` and `[data-theme="light"]` blocks
(static/dashboard.html); keep them in step with it rather than tuning colours here in
isolation. Like Bridge, the choice is Dark, Light or Auto (follow Windows).

Bridge components and their Qt equivalents in this file:
  header bar with gradient and the orange `> bridge` mark   QFrame#header, QLabel#brand
  .ov-card / .kpi-card (card surface, 1px border, 10px)       QFrame[card="true"]
  .kpi-label (tiny uppercase muted caption)                   section_label()
  FILTER / MARKET row labels (cyan uppercase)                 QLabel[role="rowlabel"]
  .tab-btn.active / .preset-btn.active (cyan gradient)        QPushButton[variant="primary"]
  .preset-btn (chip button)                                   QPushButton (default)
  "Data as of" pill with live dot                             QLabel[role="badge"][state=...]
  .tab-bar (rounded track, active pill)                       QTabWidget / QTabBar
State changes go through set_prop() so Qt re-applies the rules (a dynamic property
change is otherwise ignored until the next polish).
"""

import glob
import os
import re

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QLabel

# ---- Bridge tokens ------------------------------------------------------------------
# `control` is a chip or button surface and `field` an editable input: one colour in
# dark, but light mode separates them as Bridge does (white fields, grey chips). The
# extra dark entries are the literals the style sheet used to hard-code, byte for byte,
# so dark mode is unchanged by having a light one.
PALETTES = {
    "dark": {
        "bg": "#0a0e17", "bg_elevated": "#0d1422", "bg_inset": "#1a2332",
        "card": "#111827", "card_hover": "#1a2332", "border": "#2d3748", "border_soft": "#1e293b",
        "border_strong": "#475569", "control": "#1e293b", "control_hover": "#334155",
        "field": "#1e293b", "panel_inset": "#0f172a", "pressed": "#1a2332",
        "cyan": "#00d4ff", "cyan_deep": "#00a8cc", "cyan_hover_a": "#33ddff", "cyan_hover_b": "#00b8de",
        "green": "#10b981", "red": "#ef4444", "red_soft": "#f87171", "yellow": "#f59e0b",
        "orange": "#f97316", "purple": "#a78bfa", "indigo": "#6366f1",
        "text": "#f1f5f9", "text_dim": "#94a3b8", "text_muted": "#64748b", "hint": "#64748b",
        "disabled_text": "#3b475a", "on_accent": "#0a0e17", "input_bg": "#0a0e17",
        "handle_ring": "#0a0e17",
        "header_a": "#0a0e17", "header_b": "#111827", "header_c": "#0f1729",
        "header_line": "rgba(30, 41, 59, 0.6)", "chip_soft": "rgba(30, 41, 59, 0.6)",
    },
    # Bridge's [data-theme="light"]: white cards on a cool grey page, a deeper cyan
    # that stays readable on white, and white text on the cyan buttons.
    "light": {
        "bg": "#f0f2f5", "bg_elevated": "#e8eaed", "bg_inset": "#f8fafc",
        "card": "#ffffff", "card_hover": "#f8fafc", "border": "#d1d5db", "border_soft": "#e2e8f0",
        "border_strong": "#94a3b8", "control": "#e2e8f0", "control_hover": "#d5dbe3",
        "field": "#ffffff", "panel_inset": "#eef1f5", "pressed": "#cbd5e1",
        "cyan": "#0284c7", "cyan_deep": "#0369a1", "cyan_hover_a": "#0ea5e9", "cyan_hover_b": "#0284c7",
        "green": "#059669", "red": "#dc2626", "red_soft": "#ef4444", "yellow": "#d97706",
        "orange": "#ea580c", "purple": "#7c3aed", "indigo": "#4f46e5",
        # `hint` is the one deliberate departure from Bridge's tokens: Bridge keeps its
        # #94a3b8 muted grey for tiny captions, but this app also sets paragraphs of
        # help text in it, which on white is 2.6:1. Slate-500 is 4.8:1; captions keep
        # Bridge's grey. (In dark, `hint` IS text_muted, so dark is unchanged.)
        "text": "#1e293b", "text_dim": "#475569", "text_muted": "#94a3b8", "hint": "#64748b",
        "disabled_text": "#a8b1bd", "on_accent": "#ffffff", "input_bg": "#ffffff",
        "handle_ring": "#ffffff",
        "header_a": "#ffffff", "header_b": "#fbfcfd", "header_c": "#f8fafc",
        "header_line": "#d1d5db", "chip_soft": "rgba(209, 213, 219, 0.38)",
    },
}
# Icon files per palette: the dark set is drawn for dark surfaces, `-light` for light.
ICON_SUFFIX = {"dark": "", "light": "-light"}
MODES = ("dark", "light", "auto")
MODE = "dark"                     # the palette in force (never "auto")
C = dict(PALETTES["dark"])        # mutated in place, so `from .theme import C` stays live


def rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    return "rgba(%d, %d, %d, %s)" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def _derived(p):
    """Translucent fills and outlines, from the palette's own accents."""
    d = {"t_" + k: rgba(p[k], "0.12" if k == "cyan" else "0.14")
         for k in ("cyan", "green", "red", "yellow", "orange")}
    d.update(l_yellow=rgba(p["yellow"], "0.45"), l_cyan=rgba(p["cyan"], "0.45"),
             l_orange=rgba(p["orange"], "0.55"), cyan_line=rgba(p["cyan"], "0.5"),
             cyan_sel=rgba(p["cyan"], "0.30"), cyan_focus=rgba(p["cyan"], "0.6"))
    return d


# Tinted fills for state chips, as Bridge's delta chips and badges use them.
TINT = {k[2:]: v for k, v in _derived(C).items() if k.startswith("t_")}


def clamp_mode(choice):
    return choice if choice in MODES else "dark"


def system_mode():
    """Windows' app colour setting, as Qt reports it: "light" or "dark"."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        scheme = QGuiApplication.styleHints().colorScheme()
        return "light" if scheme == Qt.ColorScheme.Light else "dark"
    except Exception:
        return "dark"


def resolve_mode(choice):
    """The palette a saved choice means right now ("auto" asks Windows)."""
    choice = clamp_mode(choice)
    return system_mode() if choice == "auto" else choice


def set_palette(mode):
    """Make `mode` the palette that C, TINT and new style sheets use."""
    global MODE
    mode = "light" if mode == "light" else "dark"
    MODE = mode
    C.clear()
    C.update(PALETTES[mode])
    TINT.clear()
    TINT.update({k[2:]: v for k, v in _derived(C).items() if k.startswith("t_")})
# Qt style sheets need a file path for icons; forward slashes keep them valid on Windows.
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets").replace("\\", "/")
# Inter is Bridge's typeface. It ships with the app (SIL OFL 1.1, see fonts/Inter-LICENSE.txt),
# five static weights 400-800 from the official v4.1 release; Segoe UI is only the fallback.
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
UI_FONTS = ("Inter", "Segoe UI")
MONO_FONTS = ("Cascadia Code", "Consolas")
BRAND_FONTS = ("Courier New", "Consolas")  # Bridge's mark: 'Courier New','Menlo','Consolas'


# ---- Zoom -------------------------------------------------------------------------
# Every size in the style sheet is written at 100% and multiplied by SCALE when the
# sheet is built; sizes set in code go through px(). The window re-runs its sizing
# hooks after apply_theme(), so a zoom change is live, with no restart.
ZOOM_STEPS = (0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4)
SCALE = 1.0
_SECTION_LABELS = []


def clamp_scale(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(ZOOM_STEPS[-1], max(ZOOM_STEPS[0], value))


def px(value):
    """A 100%-size pixel measure at the current zoom (never below 1 for a positive size)."""
    return max(1, round(value * SCALE)) if value > 0 else 0


_STYLE_CACHE = {}


def stylesheet(mode):
    """The whole style sheet for one palette, at 100%."""
    mode = "light" if mode == "light" else "dark"
    p = PALETTES[mode]
    return _TEMPLATE.format(**p, **_derived(p), assets=ASSETS, icon=ICON_SUFFIX[mode])


def build_style(scale, mode=None):
    """The style sheet with every px size multiplied by `scale` (cached per palette and scale)."""
    key = (mode or MODE, scale)
    if key in _STYLE_CACHE:
        return _STYLE_CACHE[key]

    def sub(m):
        v = float(m.group(1))
        return "%dpx" % (max(1, round(v * scale)) if v > 0 else 0)
    _STYLE_CACHE[key] = re.sub(r"(\d+(?:\.\d+)?)px", sub, stylesheet(key[0]))
    return _STYLE_CACHE[key]


def load_bundled_fonts():
    """Register the bundled Inter weights with Qt. Returns the font families added."""
    families = set()
    for path in sorted(glob.glob(os.path.join(FONTS_DIR, "*.ttf"))):
        fid = QFontDatabase.addApplicationFont(path)
        if fid != -1:
            families.update(QFontDatabase.applicationFontFamilies(fid))
    return families


def ui_family():
    have = set(QFontDatabase.families())
    return next((f for f in UI_FONTS if f in have), UI_FONTS[-1])


def section_label(text, role="section"):
    """Bridge's .kpi-label: tiny, uppercase, letter-spaced, muted (or cyan for row labels)."""
    lbl = QLabel(text.upper())
    lbl.setProperty("role", role)
    f = lbl.font()
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
    # Bridge's letter-spacing widens spaces too (CSS spaces every character); Qt's
    # percentage only scales glyphs, so a 10 px space between capitals read as none.
    f.setWordSpacing(1.2 * SCALE)
    f.setWeight(QFont.Weight.Bold)
    lbl.setFont(f)
    _SECTION_LABELS.append(lbl)
    return lbl


def set_prop(widget, name, value):
    """Set a dynamic property and make the style sheet notice it."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def apply_theme(app, scale=None, mode=None):
    """Font + style sheet for the whole application, at `scale` (default: current zoom)
    in palette `mode` ("dark" or "light"; default: the palette in force)."""
    global SCALE
    if scale is not None:
        SCALE = clamp_scale(scale)
    if mode is not None:
        set_palette(mode)
    load_bundled_fonts()
    f = QFont(ui_family())
    f.setPointSizeF(9.75 * SCALE)   # 13 px at 100%: Bridge's --text-base on its 14 px root
    # Render Inter as a browser does. Full hinting snaps its advances to whole pixels,
    # which shrank the space at 10-11 px bold from 2.55 to 2.0 px - "MIC TEST" and
    # "DICTATION ON" lost their gaps (measured with QFontMetricsF). Vertical-only
    # hinting keeps fractional widths and crisp baselines.
    f.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    app.setFont(f)
    app.setStyleSheet(build_style(SCALE))
    for lbl in list(_SECTION_LABELS):
        try:
            lf = lbl.font()
            lf.setWordSpacing(1.2 * SCALE)
            lbl.setFont(lf)
        except RuntimeError:            # the label's window was destroyed
            _SECTION_LABELS.remove(lbl)


_TEMPLATE = """
QMainWindow, QDialog {{ background-color: {bg}; color: {text}; }}
QWidget {{ background-color: transparent; color: {text}; }}
QMainWindow > QWidget, QDialog {{ background-color: {bg}; }}
QToolTip {{
    background-color: {bg_elevated}; color: {text}; border: 1px solid {border};
    border-radius: 6px; padding: 5px 9px;
}}

/* ---- Header (Bridge .header: gradient bar, hairline bottom border) ---- */
QFrame#header {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {header_a}, stop:0.5 {header_b}, stop:1 {header_c});
    border: none; border-bottom: 1px solid {header_line};
}}
QLabel#brand {{ color: {orange}; font-size: 20px; font-weight: 800; }}
QFrame#brandCursor {{ background-color: {orange}; border: none; }}
QLabel#brandSub {{
    color: {text_dim}; font-size: 9px; font-weight: 600;
    border: none; border-left: 1px solid {border}; padding-left: 12px;
}}

/* ---- Cards (Bridge .ov-card / .kpi-card) ---- */
QFrame[card="true"] {{
    background-color: {card}; border: 1px solid {border}; border-radius: 10px;
}}
QFrame[card="true"][accent="cyan"] {{ border-left: 3px solid {cyan}; }}
QLabel[role="section"] {{ color: {text_muted}; font-size: 10px; background: transparent; }}
QLabel[role="rowlabel"] {{ color: {cyan}; font-size: 10px; background: transparent; }}
QLabel[role="hint"] {{ color: {hint}; font-size: 11px; background: transparent; }}
QLabel[role="value"] {{ color: {cyan}; font-weight: 700; font-size: 13px; background: transparent; }}
QLabel {{ color: {text_dim}; background: transparent; }}

/* ---- Badges (Bridge "Data as of" pill + delta chips) ---- */
QLabel[role="badge"] {{
    border-radius: 11px; padding: 0 12px; font-size: 11px; font-weight: 700;
    color: {yellow}; background-color: {t_yellow}; border: 1px solid {l_yellow};
}}
QLabel[role="badge"][state="ok"] {{
    color: {cyan}; background-color: {t_cyan}; border: 1px solid {l_cyan};
}}
QLabel[role="badge"][state="degraded"] {{
    color: {orange}; background-color: {t_orange}; border: 1px solid {l_orange};
}}
QLabel[role="dot"] {{ border-radius: 4px; background-color: {yellow}; }}
QLabel[role="dot"][state="ok"] {{ background-color: {green}; }}
QLabel[role="dot"][state="degraded"] {{ background-color: {orange}; }}

/* ---- Buttons (Bridge .preset-btn chip; primary = the cyan gradient) ---- */
QPushButton {{
    background-color: {control}; color: {text_dim}; border: 1px solid {border};
    border-radius: 6px; padding: 7px 16px; font-size: 12px; font-weight: 700; min-height: 18px;
}}
QPushButton:hover {{ background-color: {control_hover}; color: {text}; border-color: {border_strong}; }}
QPushButton:pressed {{ background-color: {pressed}; }}
QPushButton:disabled {{ background-color: {bg_elevated}; color: {disabled_text}; border-color: {border_soft}; }}
QPushButton:focus {{ outline: none; }}
QPushButton[variant="primary"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan}, stop:1 {cyan_deep});
    color: {on_accent}; border: none; padding: 9px 22px; font-size: 13px; font-weight: 800;
}}
QPushButton[variant="primary"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan_hover_a}, stop:1 {cyan_hover_b});
}}
QPushButton[variant="primary"]:disabled {{ background: {bg_inset}; color: {text_muted}; }}
QPushButton[variant="danger"], QPushButton[variant="primary"][active="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {red_soft}, stop:1 {red});
    color: #ffffff; border: none;
}}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid {border}; color: {text_dim}; }}
QPushButton[variant="ghost"]:hover {{ border-color: {cyan}; color: {text}; }}
QPushButton[variant="outline"] {{ background: transparent; border: 1px solid {cyan_line}; color: {cyan}; }}
QPushButton[variant="outline"]:hover {{ background-color: {t_cyan}; border-color: {cyan}; }}
QPushButton[variant="outline"]:disabled {{ background: transparent; border-color: {border_soft}; color: {disabled_text}; }}
QPushButton[size="sm"] {{ padding: 4px 12px; font-size: 11px; min-height: 14px; }}
QPushButton[variant="icon"] {{
    background: transparent; border: 1px solid {border}; color: {text_dim};
    padding: 0; min-height: 0; font-size: 15px; font-weight: 700;
}}
QPushButton[variant="icon"]:hover {{ border-color: {cyan}; color: {text}; }}
QPushButton[variant="icon"]:disabled {{ color: {disabled_text}; border-color: {border_soft}; }}
QPushButton#btn_record[recording="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {red_soft}, stop:1 {red});
    color: #ffffff; border: none;
}}
/* Dictation switch: the header pill (live = cyan gradient with dark text, like the active tab) */
QPushButton#btn_dictation {{
    border-radius: 13px; padding: 0 16px; font-size: 11px; font-weight: 800;
    background: transparent; color: {text_muted}; border: 1px solid {border};
}}
QPushButton#btn_dictation:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan}, stop:1 {cyan_deep});
    color: {on_accent}; border: none;
}}

/* ---- Text surfaces ---- */
QTextEdit, QPlainTextEdit {{
    background-color: {input_bg}; color: {text}; border: 1px solid {border}; border-radius: 8px;
    padding: 10px; selection-background-color: {cyan_sel}; selection-color: {text};
}}
QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {cyan_focus}; }}
QLineEdit, QSpinBox {{
    background-color: {field}; color: {text}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 9px; selection-background-color: {cyan_sel};
}}
QLineEdit:focus, QSpinBox:focus {{ border-color: {cyan}; }}
QComboBox {{
    background-color: {field}; color: {text}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 10px;
}}
QComboBox:hover {{ border-color: {border_strong}; }}
QComboBox:focus, QComboBox:on {{ border-color: {cyan}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({assets}/chevron-down{icon}.svg); width: 12px; height: 12px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; background: transparent; width: 18px; }}
QSpinBox::up-arrow {{ image: url({assets}/chevron-up{icon}.svg); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url({assets}/chevron-down{icon}.svg); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{
    background-color: {card}; color: {text}; border: 1px solid {border};
    selection-background-color: {t_cyan}; selection-color: {cyan}; outline: none; padding: 4px;
}}

/* ---- Meter, slider, checkbox ---- */
QProgressBar {{ background-color: {control}; border: none; border-radius: 3px; }}
QProgressBar::chunk {{
    border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {cyan}, stop:1 {green});
}}
QSlider::groove:horizontal {{ background: {control}; height: 5px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {cyan_deep}, stop:1 {cyan});
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {cyan}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    border: 2px solid {handle_ring};
}}
QSlider::handle:horizontal:disabled {{ background: {text_muted}; }}
QSlider::sub-page:horizontal:disabled {{ background: {border}; }}
QCheckBox {{ color: {text}; spacing: 9px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px; border: 1px solid {border_strong}; background: {field};
}}
QCheckBox::indicator:hover {{ border-color: {cyan}; }}
QCheckBox::indicator:checked {{ background: {cyan}; border-color: {cyan}; image: url({assets}/check{icon}.svg); }}

/* ---- Tabs (Bridge .tab-bar: a rounded track, the active tab a cyan pill) ---- */
QTabWidget::pane {{
    background-color: {card}; border: 1px solid {border}; border-radius: 10px; top: 8px;
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {text_dim}; padding: 7px 18px; margin-right: 4px;
    border-radius: 8px; font-weight: 700; font-size: 12px;
}}
QTabBar::tab:hover {{ color: {text}; background: {chip_soft}; }}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan}, stop:1 {cyan_deep});
    color: {on_accent};
}}
QDialogButtonBox QPushButton {{ min-width: 84px; }}

/* ---- Scroll bars, status bar ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {border_strong}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{
    background-color: {bg_elevated}; color: {hint}; font-size: 11px;
    border-top: 1px solid {border_soft};
}}
QStatusBar::item {{ border: none; }}
QGroupBox {{
    background-color: {card}; border: 1px solid {border}; border-radius: 10px;
    margin-top: 14px; padding-top: 12px; color: {text_muted}; font-weight: 700;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {text_muted}; }}
QMenu {{
    background-color: {card}; color: {text}; border: 1px solid {border}; border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background-color: {t_cyan}; color: {cyan}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
"""
