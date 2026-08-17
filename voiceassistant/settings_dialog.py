"""SettingsDialog — preferences UI.

Hotkeys are edited on the main window's capture pills (their single home);
this dialog covers audio, transcription, and display preferences.
"""

import sounddevice as sd
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)


class SettingsDialog(QDialog):
    """Settings dialog for configuring the assistant.

    Organized into tabs (Transcription / General) so the growing set of
    preferences stays scannable. Hotkeys have their own home — the capture
    pills on the main window — so they are not duplicated here.
    """

    def __init__(self, config, tts_engine, parent=None):
        super().__init__(parent)
        self.config = config
        self.tts = tts_engine
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self._build_ui()

    @staticmethod
    def _hint(text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #6c7086; font-size: 10px;")
        lbl.setWordWrap(True)
        return lbl

    def _build_ui(self):
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        # ---- Tab 1: Transcription (mic + Whisper) ----
        trans = QWidget()
        tlay = QFormLayout(trans)
        tlay.setSpacing(12)

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
        tlay.addRow("Microphone:", self.mic_combo)

        self.model_combo = QComboBox()
        # NOTE: the distil-* models are deliberately NOT offered. distil-large-v3
        # fails this app's hallucination gate — on pure room noise it invents
        # "Thank you." with no_speech_prob ~0.09-0.16, i.e. it is CONFIDENT the
        # noise is speech, so no threshold can filter it and the text gets pasted.
        # (`medium` reports 0.88-0.96 on the same audio and is correctly dropped.)
        # Verified: RUN_CORPUS=1 CORPUS_MODEL=distil-large-v3 -> 3 failures.
        for m in ["tiny", "base", "small", "medium", "large-v3"]:
            self.model_combo.addItem(m)
        self.model_combo.setCurrentText(self.config["whisper_model"])
        tlay.addRow("Whisper Model:", self.model_combo)
        tlay.addRow(self._hint(
            "tiny/base=fastest  medium=good balance  large-v3=most accurate"
        ))
        tlay.addRow(self._hint("Changing model downloads it once (~1-3 GB)."))

        self.lang_combo = QComboBox()
        for code, name in [("en", "English"), ("es", "Spanish"), ("fr", "French"),
                           ("de", "German"), ("ja", "Japanese"), ("zh", "Chinese"),
                           ("pt", "Portuguese"), ("it", "Italian"), ("ko", "Korean")]:
            self.lang_combo.addItem(name, code)
        idx = self.lang_combo.findData(self.config["whisper_language"])
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        tlay.addRow("Language:", self.lang_combo)

        self.cleanup_check = QCheckBox("Light cleanup (fillers, casing, spacing)")
        self.cleanup_check.setChecked(self.config.get("light_cleanup", True))
        tlay.addRow(self.cleanup_check)
        tabs.addTab(trans, "Transcription")

        # ---- Tab 2: General (display + startup + diagnostics) ----
        gen = QWidget()
        glay = QFormLayout(gen)
        glay.setSpacing(12)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(9, 24)
        self.font_spin.setValue(self.config["font_size"])
        glay.addRow("Font Size:", self.font_spin)

        self.aot_check = QCheckBox("Always on top")
        self.aot_check.setChecked(self.config["always_on_top"])
        glay.addRow(self.aot_check)

        self.startup_check = QCheckBox("Start with Windows")
        self.startup_check.setChecked(self.config.get("start_with_windows"))
        glay.addRow(self.startup_check)

        self.start_minimized_check = QCheckBox("Start minimized to tray")
        self.start_minimized_check.setChecked(self.config.get("start_minimized", True))
        glay.addRow(self.start_minimized_check)

        self.debug_check = QCheckBox("Debug logging (troubleshooting only)")
        self.debug_check.setChecked(self.config.get("debug_logging", False))
        glay.addRow(self.debug_check)

        glay.addRow(self._hint(
            "Hotkeys (Dictate / Read / OCR) are set on the main window — click "
            "any hotkey pill to change it. Read-aloud voice & speed are on the "
            "main window's Playback bar."
        ))
        tabs.addTab(gen, "General")

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

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
        }
