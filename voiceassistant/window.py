"""MainWindow — orchestration and signal wiring.

All engine callbacks arrive as queued Qt signals; all blocking work happens on
subsystem workers (recorder callback thread, transcriber/tts/ocr/paste/read
SerialWorkers). Nothing in this class may sleep or block: the GUI thread only
renders state and dispatches jobs.
"""

import time

import keyboard as kb
import numpy as np
import pyperclip
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QProgressBar, QPushButton, QSlider, QStatusBar, QStyle, QSystemTrayIcon,
    QTextEdit, QVBoxLayout, QWidget,
)

from . import applog, winapi
from .config import Config, DEFAULTS, normalize_hotkey, validate_hotkey
from .ocr import OCREngine, RegionSelector, ScreenCapture
from .paste import Paster
from .recorder import VoiceRecorder
from .settings_dialog import SettingsDialog
from .text import clean_transcript, is_probable_hallucination
from .transcriber import Transcriber
from .tts import TTSEngine
from .widgets import HotkeyCaptureWidget, RecordingIndicator
from .workers import SerialWorker


class MainWindow(QMainWindow):
    # Thread-safe signals for global hotkey callbacks
    _sig_hotkey_press = Signal()
    _sig_hotkey_release = Signal()
    _sig_hotkey_screen = Signal()
    _sig_hotkey_read = Signal()
    # Worker → GUI marshalling
    _sig_read_text_ready = Signal(str)
    _sig_paste_done = Signal(bool, str)
    _sig_crash_notice = Signal(str)

    def __init__(self, entry_script):
        super().__init__()
        self.config = Config()
        applog.set_debug(self.config.get("debug_logging", False))
        self._entry_script = entry_script
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
        self.tts.set_voice(self.config.get("tts_voice", "en-US-AndrewNeural"))
        self.region_selector = RegionSelector()
        self.indicator = RecordingIndicator()
        self.paster = Paster()
        self._read_worker = SerialWorker("read-selection")

        # Dictation state.
        # _pending_target_hwnd is only a hand-off between record-START (where
        # the foreground window is captured) and record-STOP (where it is bound
        # into the transcription job). From then on the HWND travels WITH the
        # job (TranscriptionResult.context) — overlapping dictations can no
        # longer paste into each other's windows.
        self._pending_target_hwnd = None
        self._read_target_hwnd = None  # window to refocus for read-selection copy
        self._dictation_active = self.config.get("dictation_mode", True)
        self._last_job_id = None  # duplicate-delivery guard (monotonic job ids)

        self._build_ui()
        self._connect_signals()

        # Push-to-talk state
        self._ptt_active = False
        self._last_hotkey_press_time = 0.0
        self._force_quit = False
        # Debounce flag for read-aloud hotkey
        self._read_in_flight = False

        # Wire hotkey signals BEFORE registering hotkeys
        self._sig_hotkey_press.connect(self._hotkey_press_handler)
        self._sig_hotkey_release.connect(self._hotkey_release_handler)
        self._sig_hotkey_screen.connect(self._on_cursor_read)
        self._sig_hotkey_read.connect(self._on_read_aloud_toggle)
        self._sig_read_text_ready.connect(self._on_read_text_ready)
        self._sig_paste_done.connect(self._on_paste_done)
        self._sig_crash_notice.connect(self._on_crash_notice)
        applog.set_notifier(self._sig_crash_notice.emit)

        self._setup_hotkeys()
        self._setup_tray()
        self._setup_show_request_timer()
        winapi.set_start_with_windows(
            self.config.get("start_with_windows", True), self._entry_script
        )
        self._apply_window_flags()

        # Load models in background (each engine's own worker)
        self.transcriber.load_model()
        self.ocr.load_model()

        self._update_status("Starting up...")
        if self.config.load_error:
            self._update_status(self.config.load_error)
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

        # ---- Hotkey capture widgets (fully user-configurable, never hardcoded) ----
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

        # ---- Voice & Speed controls ----
        playback_group = QGroupBox("Playback")
        playback_lay = QHBoxLayout(playback_group)
        playback_lay.setSpacing(12)

        playback_lay.addWidget(QLabel("Voice:"))
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(260)
        voices = self.tts.get_voices()
        for vid, vname in voices:
            self.voice_combo.addItem(vname, vid)
        saved_voice = self.config.get("tts_voice", "en-US-AndrewNeural")
        idx = self.voice_combo.findData(saved_voice)
        if idx >= 0:
            self.voice_combo.setCurrentIndex(idx)
        playback_lay.addWidget(self.voice_combo)

        playback_lay.addSpacing(16)
        playback_lay.addWidget(QLabel("Speed:"))

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
        self.btn_record.clicked.connect(self._on_record)
        self.btn_stop.clicked.connect(self._on_stop_record)
        self.btn_screen_read.clicked.connect(self._on_screen_select)
        self.btn_cursor_read.clicked.connect(self._on_cursor_read)
        self.btn_copy.clicked.connect(self._on_copy)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_speak_toggle.clicked.connect(self._on_speak_toggle)
        self.btn_settings.clicked.connect(self._on_settings)
        self.model_combo.currentTextChanged.connect(self._on_model_change)

        self.voice_combo.currentIndexChanged.connect(self._on_voice_change)
        self.speed_slider.valueChanged.connect(self._on_speed_change)
        self.speed_slider.sliderReleased.connect(self.config.flush)

        self.btn_dictation.toggled.connect(self._on_dictation_toggle)

        self.recorder.recording_started.connect(self._on_recording_started)
        self.recorder.recording_stopped.connect(self._on_recording_stopped)
        self.recorder.level_update.connect(self._on_level_update)
        self.recorder.error.connect(self._on_mic_error)

        self.transcriber.model_loading.connect(self._on_model_loading)
        self.transcriber.model_ready.connect(self._on_model_ready)
        self.transcriber.transcription_ready.connect(self._on_transcription_ready)
        self.transcriber.transcription_progress.connect(self._update_status)
        self.transcriber.error.connect(self._on_error)

        self.ocr.model_loading.connect(self._on_model_loading)
        self.ocr.model_ready.connect(self._on_ocr_ready)
        self.ocr.text_ready.connect(self._on_ocr_text_ready)
        self.ocr.error.connect(self._on_error)

        self.tts.speaking_started.connect(self._on_tts_started)
        self.tts.speaking_finished.connect(self._on_tts_finished)
        self.tts.status.connect(self._update_status)
        self.tts.error.connect(self._on_error)

        self.region_selector.region_selected.connect(self._on_region_selected)
        self.region_selector.cancelled.connect(lambda: self._update_status("Selection cancelled"))

        self.indicator.clicked.connect(self._on_indicator_clicked)

    # -----------------------------------------------------------------------
    # Global hotkeys — fully user-configurable (see config.py contract)
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
            applog.dbg(f"record hotkey state check failed: {e}")

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
        actually act.
        """
        now = time.monotonic()
        if now - self._last_hotkey_press_time < 0.25:
            return  # autorepeat / double-fire — ignore silently
        self._last_hotkey_press_time = now
        if not self.recorder.is_recording and not self._ptt_active:
            applog.dbg("_hotkey_press_handler: starting PTT")
            self._ptt_active = True
            self._on_record_from_hotkey()

    @Slot()
    def _hotkey_release_handler(self):
        """Trigger key released — stop recording if push-to-talk is active."""
        # No-op (and no logging) unless PTT is active — this fires on every
        # trigger-letter release during normal typing.
        if not self._ptt_active:
            return
        applog.dbg(f"_hotkey_release_handler: recording={self.recorder.is_recording}")
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
        applog.dbg(f"_on_record_from_hotkey: transcriber_loaded={self.transcriber.is_loaded}")
        if not self.transcriber.is_loaded:
            self._update_status("Whisper model still loading, please wait...")
            return
        if self._dictation_active:
            self._pending_target_hwnd = winapi.get_foreground_window()
            applog.dbg(f"  target_hwnd captured: {self._pending_target_hwnd}")
        self.recorder.start()

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
            self._pending_target_hwnd = winapi.get_foreground_window()
        self.recorder.start()

    @Slot()
    def _on_record(self):
        """Start recording via button click."""
        if not self.transcriber.is_loaded:
            self._update_status("Whisper model still loading, please wait...")
            return
        self._pending_target_hwnd = None  # clicked in our own window, don't paste elsewhere
        self.recorder.start()

    @Slot()
    def _on_stop_record(self):
        self.recorder.stop()

    @Slot()
    def _on_recording_started(self):
        applog.dbg("_on_recording_started: showing red pill")
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
        applog.dbg(f"_on_recording_stopped: samples={len(audio)}  duration={duration:.2f}s  peak={max_amp:.4f}")

        self.btn_record.setText("  Record")
        self.btn_record.setProperty("recording", "false")
        self.btn_record.style().unpolish(self.btn_record)
        self.btn_record.style().polish(self.btn_record)
        self.btn_record.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.level_bar.setValue(0)

        # Consume the pending target exactly once — from here on it travels
        # WITH the job. (The old shared-field approach let a second dictation
        # overwrite the first one's paste target: confirmed wrong-window race.)
        target_hwnd = self._pending_target_hwnd
        self._pending_target_hwnd = None

        min_seconds = float(self.config.get("min_record_seconds", 0.2))
        min_peak = float(self.config.get("min_record_peak", 0.008))
        if duration < min_seconds or max_amp < min_peak:
            applog.dbg("  ignored - too short or too quiet for reliable dictation")
            self._update_status("Recording ignored - too short or too quiet")
            self.indicator.show_idle()
            return

        if len(audio) > 0:
            self._update_status(f"Transcribing {duration:.1f}s (peak {max_amp:.3f})...")
            self.indicator.show_transcribing()
            self.transcriber.transcribe(audio, context=target_hwnd)
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

    @Slot(object)
    def _on_transcription_ready(self, result):
        """Handle a completed transcription job (TranscriptionResult)."""
        applog.dbg(
            f"_on_transcription_ready job={result.job_id} "
            f"({len(result.text)} chars, retried={result.retried}, "
            f"dur={result.duration_s:.2f}s)"
        )
        # Duplicate-delivery guard: monotonic job ids (the old id(audio) check
        # could drop a REAL second dictation after CPython reused the address).
        if self._last_job_id == result.job_id:
            applog.dbg("  duplicate job delivery ignored")
            return
        self._last_job_id = result.job_id

        if result.no_speech:
            self.indicator.show_idle()
            self._update_status("No speech detected")
            return

        text = clean_transcript(
            result.text, light=self.config.get("light_cleanup", True)
        )

        # Backstop for silence-hallucinations that slip past the segment
        # filters: sub-1.2s clip, VAD found nothing, text is a known artifact.
        if is_probable_hallucination(result, text):
            applog.dbg("  suppressed probable hallucination (retry artifact)")
            self.indicator.show_idle()
            self._update_status("Ignored noise (no clear speech)")
            return

        target_hwnd = result.context
        own_hwnd = int(self.winId())
        is_own_window = target_hwnd is not None and target_hwnd == own_hwnd

        if (self._dictation_active and self.config.get("auto_paste", True)
                and target_hwnd and not is_own_window and text.strip()):
            # Paste runs on the paste worker — the GUI thread never blocks.
            self.indicator.show_pasting()
            self._update_status("Pasting...")
            self.paster.submit(target_hwnd, text, self._sig_paste_done.emit)
        else:
            self.indicator.show_idle()
            self._update_status("Transcription complete")
            self._append_output(text, prefix="[Voice]")

    @Slot(bool, str)
    def _on_paste_done(self, success, text):
        if success:
            self.indicator.show_done()
            self._update_status("Transcribed and pasted")
        else:
            self.indicator.show_error()
            self._update_status(
                "Paste failed — text is in the panel and on the clipboard (Ctrl+V to paste manually)"
            )
            self._append_output(text, prefix="[Voice]")

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
            # speak() interrupts any current speech — a new OCR capture is
            # never silently dropped while TTS is busy.
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
        self.btn_speak_toggle.setStyleSheet("")

    # -----------------------------------------------------------------------
    # Read-aloud (selection) flow
    # -----------------------------------------------------------------------
    @Slot()
    def _on_read_aloud_toggle(self):
        """Toggle read aloud: if speaking or in-flight, stop. Otherwise start read."""
        if self.tts.is_speaking or self._read_in_flight:
            self.tts.stop()
            self._read_in_flight = False
            self._update_status("Read aloud stopped")
            return

        # Capture target window NOW before Windows key can steal focus
        self._read_target_hwnd = winapi.get_foreground_window()

        self._read_in_flight = True
        self._update_status("Capturing selection...")
        self._read_worker.submit(self._read_selection_job)

    def _read_selection_job(self):
        """Runs on the read-selection worker: wait for modifier release,
        refocus target, copy selection via clipboard sentinel, emit result.
        Always emits (try/finally) so _read_in_flight can never wedge."""
        try:
            text = self._capture_selection()
        except Exception:
            applog.exception("read-selection capture failed")
            text = ""
        finally:
            self._sig_read_text_ready.emit(text if isinstance(text, str) else "")

    def _capture_selection(self):
        # Wait for ALL keys in the hotkey combo to be released (up to 1 second)
        hotkey_keys = self.config["hotkey_read_aloud"].lower().split("+")
        for _ in range(200):
            if not any(kb.is_pressed(k) for k in hotkey_keys if k):
                break
            time.sleep(0.005)

        # Give Windows a moment in case Start menu opened, then close it via Esc.
        # (Escape is only needed for Windows-key hotkeys; scoping it further is
        # a Phase 4 item — see M2.)
        time.sleep(0.05)
        winapi.send_escape()
        time.sleep(0.05)

        # Refocus the target window (where the user had text selected)
        target = self._read_target_hwnd
        if target:
            winapi.set_foreground_window(target)
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
        winapi.send_ctrl_c()

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

        if not text.strip():
            try:
                if old_clipboard:
                    pyperclip.copy(old_clipboard)
            except Exception:
                pass
            return ""
        return text

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

    # -----------------------------------------------------------------------
    # Hotkey editing
    # -----------------------------------------------------------------------
    def _save_hotkey(self, config_key, label, combo):
        """Called when a HotkeyCaptureWidget captures a new combo."""
        if not self._set_hotkey_if_valid(config_key, label, combo):
            return
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

    # -----------------------------------------------------------------------
    # Mode toggles / settings
    # -----------------------------------------------------------------------
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
        # defer_save: the slider fires per tick — one disk write per pixel of
        # drag was a real I/O storm. Flushed on sliderReleased + closeEvent.
        self.config.set("tts_speed", speed, defer_save=True)

    @Slot()
    def _on_settings(self):
        dlg = SettingsDialog(self.config, self.tts, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            new_dev = vals.get("audio_device", -1)
            self.config.set("audio_device", new_dev)
            self.recorder.device = new_dev if new_dev >= 0 else None

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
            self.config.set("debug_logging", vals["debug_logging"])
            applog.set_debug(vals["debug_logging"])
            winapi.set_start_with_windows(vals["start_with_windows"], self._entry_script)
            self._apply_window_flags()

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
    def _on_mic_error(self, msg):
        """Recorder failure: clear dictation hand-off state, then report."""
        self._pending_target_hwnd = None
        self._ptt_active = False
        self._on_error(msg)

    @Slot(str)
    def _on_error(self, msg):
        self._update_status(f"Error: {msg}")
        self._append_output(msg, prefix="[Error]")
        self.indicator.show_idle()

    @Slot(str)
    def _on_crash_notice(self, msg):
        """Unhandled-exception notifier (from applog's crash handlers)."""
        self._update_status(msg)
        try:
            self.tray.showMessage("Voice Assistant", msg,
                                  QSystemTrayIcon.MessageIcon.Warning, 5000)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Tray / window lifecycle
    # -----------------------------------------------------------------------
    def _setup_show_request_timer(self):
        self._show_request_timer = QTimer(self)
        self._show_request_timer.timeout.connect(self._poll_show_request)
        self._show_request_timer.start(500)

    def _poll_show_request(self):
        if winapi.show_requested():
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
        # Full teardown — hooks, timers, workers, players, temp files.
        try:
            kb.unhook_all()
        except Exception:
            pass
        try:
            self._show_request_timer.stop()
        except Exception:
            pass
        if self.recorder.is_recording:
            try:
                self.recorder.stop()
            except Exception:
                pass
        try:
            self.tts.shutdown()   # stop + drain worker + release VLC + rm temp dir
        except Exception:
            pass
        try:
            self.paster.shutdown()
        except Exception:
            pass
        winapi.release_single_instance_lock()
        self.config.flush()
        self.config.save()
        event.accept()
