"""SettingsDialog — preferences UI.

Hotkeys are edited on the main window's capture pills (their single home);
this dialog covers audio, transcription, and display preferences.
"""

import sounddevice as sd
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QSpinBox,
)


class SettingsDialog(QDialog):
    """Settings dialog for configuring the assistant."""

    def __init__(self, config, tts_engine, parent=None):
        super().__init__(parent)
        self.config = config
        self.tts = tts_engine
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
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

        # --- Hotkeys live on the main window ---
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
        self.startup_check.setChecked(self.config.get("start_with_windows"))
        layout.addRow(self.startup_check)

        self.start_minimized_check = QCheckBox("Start minimized to tray")
        self.start_minimized_check.setChecked(self.config.get("start_minimized", True))
        layout.addRow(self.start_minimized_check)

        self.cleanup_check = QCheckBox("Light cleanup for dictation")
        self.cleanup_check.setChecked(self.config.get("light_cleanup", True))
        layout.addRow(self.cleanup_check)

        self.debug_check = QCheckBox("Debug logging (troubleshooting only)")
        self.debug_check.setChecked(self.config.get("debug_logging", False))
        layout.addRow(self.debug_check)

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
            "debug_logging": self.debug_check.isChecked(),
            # Hotkeys are not edited here — keep current values
            "hotkey_record": self.config["hotkey_record"],
            "hotkey_read_aloud": self.config["hotkey_read_aloud"],
            "hotkey_screen_read": self.config["hotkey_screen_read"],
        }
