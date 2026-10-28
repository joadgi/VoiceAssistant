"""The app's look: Bridge's dark design system, translated to Qt.

Bridge (project-cloud-dashboard) is the reference: same palette, type scale, radii and
component language, so the two tools read as one family. The tokens below are copied
from Bridge's `:root` block (static/dashboard.html); keep them in step with it rather
than tuning colours here in isolation.

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

# ---- Bridge tokens (dark theme) -------------------------------------------------
C = {
    "bg": "#0a0e17", "bg_elevated": "#0d1422", "bg_inset": "#1a2332",
    "card": "#111827", "card_hover": "#1a2332", "border": "#2d3748", "border_soft": "#1e293b",
    "control": "#1e293b", "control_hover": "#334155", "panel_inset": "#0f172a",
    "cyan": "#00d4ff", "cyan_deep": "#00a8cc", "cyan_dim": "rgba(0, 212, 255, 0.20)",
    "green": "#10b981", "red": "#ef4444", "yellow": "#f59e0b", "orange": "#f97316",
    "purple": "#a78bfa", "indigo": "#6366f1",
    "text": "#f1f5f9", "text_dim": "#94a3b8", "text_muted": "#64748b",
}
# Tinted fills for state chips, as Bridge's delta chips and badges use them.
TINT = {
    "cyan": "rgba(0, 212, 255, 0.12)", "green": "rgba(16, 185, 129, 0.14)",
    "red": "rgba(239, 68, 68, 0.14)", "yellow": "rgba(245, 158, 11, 0.14)",
    "orange": "rgba(249, 115, 22, 0.14)",
}
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


def build_style(scale):
    """The style sheet with every px size multiplied by `scale` (cached per scale)."""
    if scale in _STYLE_CACHE:
        return _STYLE_CACHE[scale]

    def sub(m):
        v = float(m.group(1))
        return "%dpx" % (max(1, round(v * scale)) if v > 0 else 0)
    _STYLE_CACHE[scale] = re.sub(r"(\d+(?:\.\d+)?)px", sub, DARK_STYLE)
    return _STYLE_CACHE[scale]


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


def apply_theme(app, scale=None):
    """Font + style sheet for the whole application, at `scale` (default: current zoom)."""
    global SCALE
    if scale is not None:
        SCALE = clamp_scale(scale)
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


DARK_STYLE = """
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
        stop:0 #0a0e17, stop:0.5 #111827, stop:1 #0f1729);
    border: none; border-bottom: 1px solid rgba(30, 41, 59, 0.6);
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
QLabel[role="hint"] {{ color: {text_muted}; font-size: 11px; background: transparent; }}
QLabel[role="value"] {{ color: {cyan}; font-weight: 700; font-size: 13px; background: transparent; }}
QLabel {{ color: {text_dim}; background: transparent; }}

/* ---- Badges (Bridge "Data as of" pill + delta chips) ---- */
QLabel[role="badge"] {{
    border-radius: 11px; padding: 0 12px; font-size: 11px; font-weight: 700;
    color: {yellow}; background-color: {t_yellow}; border: 1px solid rgba(245, 158, 11, 0.45);
}}
QLabel[role="badge"][state="ok"] {{
    color: {cyan}; background-color: {t_cyan}; border: 1px solid rgba(0, 212, 255, 0.45);
}}
QLabel[role="badge"][state="degraded"] {{
    color: {orange}; background-color: {t_orange}; border: 1px solid rgba(249, 115, 22, 0.55);
}}
QLabel[role="dot"] {{ border-radius: 4px; background-color: {yellow}; }}
QLabel[role="dot"][state="ok"] {{ background-color: {green}; }}
QLabel[role="dot"][state="degraded"] {{ background-color: {orange}; }}

/* ---- Buttons (Bridge .preset-btn chip; primary = the cyan gradient) ---- */
QPushButton {{
    background-color: {control}; color: {text_dim}; border: 1px solid {border};
    border-radius: 6px; padding: 7px 16px; font-size: 12px; font-weight: 700; min-height: 18px;
}}
QPushButton:hover {{ background-color: {control_hover}; color: {text}; border-color: #475569; }}
QPushButton:pressed {{ background-color: {bg_inset}; }}
QPushButton:disabled {{ background-color: {bg_elevated}; color: #3b475a; border-color: {border_soft}; }}
QPushButton:focus {{ outline: none; }}
QPushButton[variant="primary"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan}, stop:1 {cyan_deep});
    color: {bg}; border: none; padding: 9px 22px; font-size: 13px; font-weight: 800;
}}
QPushButton[variant="primary"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #33ddff, stop:1 #00b8de);
}}
QPushButton[variant="primary"]:disabled {{ background: {bg_inset}; color: {text_muted}; }}
QPushButton[variant="danger"], QPushButton[variant="primary"][active="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f87171, stop:1 {red});
    color: #ffffff; border: none;
}}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid {border}; color: {text_dim}; }}
QPushButton[variant="ghost"]:hover {{ border-color: {cyan}; color: {text}; }}
QPushButton[variant="outline"] {{ background: transparent; border: 1px solid rgba(0, 212, 255, 0.5); color: {cyan}; }}
QPushButton[variant="outline"]:hover {{ background-color: {t_cyan}; border-color: {cyan}; }}
QPushButton[size="sm"] {{ padding: 4px 12px; font-size: 11px; min-height: 14px; }}
QPushButton[variant="icon"] {{
    background: transparent; border: 1px solid {border}; color: {text_dim};
    padding: 0; min-height: 0; font-size: 15px; font-weight: 700;
}}
QPushButton[variant="icon"]:hover {{ border-color: {cyan}; color: {text}; }}
QPushButton[variant="icon"]:disabled {{ color: #3b475a; border-color: {border_soft}; }}
QPushButton#btn_record[recording="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f87171, stop:1 {red});
    color: #ffffff; border: none;
}}
/* Dictation switch: the header pill (live = cyan gradient with dark text, like the active tab) */
QPushButton#btn_dictation {{
    border-radius: 13px; padding: 0 16px; font-size: 11px; font-weight: 800;
    background: transparent; color: {text_muted}; border: 1px solid {border};
}}
QPushButton#btn_dictation:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan}, stop:1 {cyan_deep});
    color: {bg}; border: none;
}}

/* ---- Text surfaces ---- */
QTextEdit, QPlainTextEdit {{
    background-color: {bg}; color: {text}; border: 1px solid {border}; border-radius: 8px;
    padding: 10px; selection-background-color: rgba(0, 212, 255, 0.30); selection-color: {text};
}}
QTextEdit:focus, QPlainTextEdit:focus {{ border-color: rgba(0, 212, 255, 0.6); }}
QLineEdit, QSpinBox {{
    background-color: {control}; color: {text}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 9px; selection-background-color: rgba(0, 212, 255, 0.30);
}}
QLineEdit:focus, QSpinBox:focus {{ border-color: {cyan}; }}
QComboBox {{
    background-color: {control}; color: {text}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 10px;
}}
QComboBox:hover {{ border-color: #475569; }}
QComboBox:focus, QComboBox:on {{ border-color: {cyan}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({assets}/chevron-down.svg); width: 12px; height: 12px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; background: transparent; width: 18px; }}
QSpinBox::up-arrow {{ image: url({assets}/chevron-up.svg); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url({assets}/chevron-down.svg); width: 10px; height: 10px; }}
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
    border: 2px solid {bg};
}}
QSlider::handle:horizontal:disabled {{ background: {text_muted}; }}
QSlider::sub-page:horizontal:disabled {{ background: {border}; }}
QCheckBox {{ color: {text}; spacing: 9px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px; border: 1px solid #475569; background: {control};
}}
QCheckBox::indicator:hover {{ border-color: {cyan}; }}
QCheckBox::indicator:checked {{ background: {cyan}; border-color: {cyan}; image: url({assets}/check.svg); }}

/* ---- Tabs (Bridge .tab-bar: a rounded track, the active tab a cyan pill) ---- */
QTabWidget::pane {{
    background-color: {card}; border: 1px solid {border}; border-radius: 10px; top: 8px;
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {text_dim}; padding: 7px 18px; margin-right: 4px;
    border-radius: 8px; font-weight: 700; font-size: 12px;
}}
QTabBar::tab:hover {{ color: {text}; background: rgba(30, 41, 59, 0.6); }}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {cyan}, stop:1 {cyan_deep});
    color: {bg};
}}
QDialogButtonBox QPushButton {{ min-width: 84px; }}

/* ---- Scroll bars, status bar ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #475569; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{
    background-color: {bg_elevated}; color: {text_muted}; font-size: 11px;
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
""".format(**C, **{"t_" + k: v for k, v in TINT.items()}, assets=ASSETS)
