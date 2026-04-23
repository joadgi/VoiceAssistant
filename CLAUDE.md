# Voice Assistant

Local desktop voice-to-text and screen reader application.

## Architecture
- **PySide6** — Qt-based desktop UI with dark theme
- **faster-whisper** — Whisper transcription on NVIDIA GPU via CTranslate2
- **EasyOCR** — Screen text recognition (GPU accelerated)
- **pyttsx3** — Text-to-speech via Windows SAPI
- **mss** — Fast screen capture
- **keyboard** — Global hotkeys

## File Map
| File | Purpose |
|---|---|
| `main.py` | Application entry point, PySide6 UI, all controls |
| `voice_engine.py` | Audio recording (sounddevice) + Whisper transcription |
| `screen_reader.py` | Screen capture (mss) + OCR (easyocr) + region selector overlay |
| `tts_engine.py` | Text-to-speech wrapper |
| `config.py` | Settings load/save (JSON) |
| `setup.bat` | One-time environment setup (venv + deps) |
| `run.bat` | Launch the app |

## Global Hotkeys
- `Ctrl+Shift+R` — Toggle recording
- `Ctrl+Shift+S` — Read text at cursor position

## Setup
1. Run `setup.bat` (creates venv, installs PyTorch + deps)
2. Run `run.bat` to launch
3. First launch downloads Whisper + OCR models (~1GB one-time)

## Notes
- GPU falls back to CPU automatically if CUDA isn't available
- Settings persist in `settings.json`
- Screen reader auto-speaks OCR results
- Region selector: click and drag to select any screen area for OCR
