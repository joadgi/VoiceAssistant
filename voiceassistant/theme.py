"""Dark theme stylesheet (Catppuccin-ish palette)."""

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
