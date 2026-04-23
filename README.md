# Voice Assistant

A local Windows desktop app for voice dictation, screen text reading, and OCR — all running on your own GPU with no cloud lock-in.

## Features

- **Push-to-talk dictation** — hold a hotkey, speak, release, and the transcription is pasted directly into whichever window had your cursor (Outlook, Word, Chrome, Slack, anything).
- **Read highlighted text aloud** — highlight any text in any app, press a hotkey, and hear it read by a natural neural voice.
- **OCR at cursor** — capture on-screen text from images, PDFs, error dialogs, or anything else you can't select, and have it read aloud.
- **Real-time speed control** — drag the speed slider from 0.5x to 3.0x *while audio is playing* — no restart.
- **Modern Microsoft neural voices** — Andrew, Brian, Christopher, Eric, Guy, Emma, Ava, Jenny, and more.
- **Fully customizable hotkeys** — click any hotkey pill on the main window, press your preferred combo.
- **Local Whisper transcription** — `faster-whisper` on NVIDIA GPU (CUDA) with CPU fallback.

## Stack

| Layer | Tech |
|---|---|
| UI | PySide6 (Qt) with dark theme |
| Voice-to-text | `faster-whisper` (CTranslate2-optimized Whisper) |
| Text-to-speech | `edge-tts` (streaming neural voices) |
| Audio playback | VLC (via `python-vlc`) — for real-time speed control |
| Screen capture | `mss` |
| OCR | EasyOCR (GPU-accelerated) |
| Global hotkeys | `keyboard` |
| Clipboard | `pyperclip` + raw Win32 `keybd_event` |

## Installation (Windows)

**Prerequisites:**
- Windows 10 or 11
- Python 3.10+ (add to PATH during install)
- NVIDIA GPU recommended (CPU also works, slower)
- VLC Media Player — install via `winget install VideoLAN.VLC` or from [videolan.org](https://videolan.org)

**Setup:**
```powershell
git clone https://github.com/joadgi/VoiceAssistant.git
cd VoiceAssistant
setup.bat
```

This creates a Python virtual environment, installs PyTorch with CUDA support, and pulls all dependencies.

**Run:**
```powershell
run.bat
```

Or double-click `run.bat` in File Explorer. First launch downloads Whisper + EasyOCR models (~1 GB, one-time).

## Project Structure

```
VoiceAssistant/
├── main.py              PySide6 UI, hotkey wiring, all orchestration
├── voice_engine.py      Mic recording + Whisper transcription
├── tts_engine.py        edge-tts streaming + VLC playback
├── screen_reader.py     Screen capture + EasyOCR + region selector
├── config.py            JSON settings load/save
├── requirements.txt     Python dependencies
├── setup.bat            First-time environment setup
├── run.bat              Silent launcher (uses pythonw.exe)
└── create_shortcut.bat  Creates desktop shortcut
```

## Default Hotkeys

| Action | Hotkey | Behavior |
|---|---|---|
| Dictate | `Ctrl+Shift+R` | Hold to record, release to transcribe + paste |
| Read Selection | `Ctrl+Shift+T` | Press once to read highlighted text, press again to stop |
| OCR at Cursor | `Ctrl+Shift+S` | Capture 600x300 region around cursor, OCR, read aloud |

All hotkeys are customizable inline on the main window — click the pill, press your combo.

## Notes

- Avoid hotkeys with the **Windows key** — the OS intercepts it and opens the Start menu
- Avoid common browser shortcuts: `Ctrl+T`, `Ctrl+W`, `Ctrl+R`, `Ctrl+Shift+T`
- Safe picks: function keys (`F9`, `F10`, `F11`), `Ctrl+Shift+[letter]`, `Alt+[letter]`

## License

Personal project. Use at your own risk.
