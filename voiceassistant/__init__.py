"""Voice Assistant — local Windows dictation, read-aloud, and OCR.

Package layout (flat; dependencies point downward only):

    app.py         bootstrap: crash handlers, single instance, QApplication
    window.py      MainWindow — orchestration + signal wiring (Qt)
    widgets.py     RecordingIndicator pill, HotkeyCaptureWidget (Qt)
    settings_dialog.py  SettingsDialog (Qt)
    theme.py       dark stylesheet
    hotkeys.py     global-hotkey registration/manager (keyboard lib)
    recorder.py    VoiceRecorder (sounddevice)
    transcriber.py Transcriber + TranscriptionResult (faster-whisper)
    tts.py         TTSEngine (edge-tts + VLC; pyttsx3 fallback)
    ocr.py         ScreenCapture + OCREngine + RegionSelector
    paste.py       Paster — the paste worker (clipboard + Ctrl+V)
    winapi.py      ALL Win32/ctypes calls live here
    text.py        pure text logic (cleanup, collapse, denylist) — no Qt/IO
    config.py      settings persistence (atomic), hotkey validation
    workers.py     SerialWorker — the app-wide threading law
    applog.py      privacy-safe rotating log + crash handlers

THREADING LAW: every subsystem owns exactly ONE SerialWorker (or an
OS-owned callback thread, e.g. sounddevice); no ad-hoc threading.Thread
anywhere. Cross-thread communication is Qt signals only.
"""

__version__ = "0.9.0"
