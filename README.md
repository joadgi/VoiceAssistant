# Voice Assistant

A local Windows desktop app for voice dictation, screen text reading, and OCR.

**Privacy:** dictation (Whisper) and OCR (EasyOCR) run **entirely on your own machine** — your voice and screen contents never leave it. Read-aloud's *neural* voices use Microsoft's online `edge-tts` service (the selected text is sent to Microsoft to synthesize audio); a fully offline Windows SAPI voice is available as a fallback if you prefer read-aloud to stay local too.

## Features

- **Push-to-talk dictation** — hold a hotkey, speak, release, and the transcription is pasted directly into whichever window had your cursor (Outlook, Word, Chrome, Slack, anything).
- **Floating desktop pill** — an always-visible indicator (drag it anywhere) that shows Ready / Recording / Transcribing / Pasted, and can be **clicked to start and stop** dictation without a hotkey.
- **Read highlighted text aloud** — highlight any text in any app, press a hotkey, and hear it read by a natural neural voice (online Microsoft voices; offline SAPI fallback available).
- **OCR at cursor** — capture on-screen text from images, PDFs, error dialogs, or anything else you can't select, and have it read aloud.
- **Real-time speed control** — drag the speed slider from 0.5x to 3.0x *while audio is playing* — no restart.
- **Modern Microsoft neural voices** — Andrew, Brian, Christopher, Eric, Guy, Emma, Ava, Jenny, and more.
- **Fully customizable hotkeys** — every hotkey (Dictate, Read, OCR) is yours to change: click any hotkey pill on the main window, then press a **single key (like F9), a combo, or even a modifier-only combo (like Ctrl+Alt)** for push-to-talk. Nothing is hardcoded. (The `Fn` key can't be bound — it's handled in keyboard firmware and never reaches Windows.)
- **Local Whisper transcription** — `faster-whisper` on NVIDIA GPU (CUDA) with CPU fallback.

## Stack

| Layer | Tech |
|---|---|
| UI | PySide6 (Qt) with dark theme |
| Voice-to-text | `faster-whisper` (CTranslate2-optimized Whisper) |
| Text-to-speech | `edge-tts` (streaming neural voices — **online** Microsoft service); `pyttsx3` SAPI offline fallback |
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
- Browser users: the default Read hotkey `Ctrl+Shift+T` doubles as "reopen closed tab" in Chrome/Edge — rebind it (any pill, any combo) if that bothers you. Likewise `Ctrl+T`, `Ctrl+W`, `Ctrl+R` collide with browser shortcuts.
- Modifier-only combos (e.g. `Ctrl+Alt`) work well for hold-to-talk, but note `Ctrl+Alt` doubles as **AltGr** on many international keyboard layouts — if you type with one of those, pick a different combo.
- Safe picks: function keys (`F9`, `F10`, `F11`), `Ctrl+Shift+[letter]`, `Alt+[letter]`

## License

Personal project. Use at your own risk.
