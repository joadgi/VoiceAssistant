# Voice Assistant

A local Windows desktop app for voice dictation, screen text reading, and OCR.

**Privacy:** dictation (Whisper) and OCR (Windows' built-in engine) run **entirely on your own machine** — your voice and screen contents never leave it. Read-aloud's *neural* voices use Microsoft's online `edge-tts` service (the selected text is sent to Microsoft to synthesize audio); a fully offline Windows SAPI voice is available as a fallback if you prefer read-aloud to stay local too.

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
| OCR | **Windows-native OCR** (the same engine PowerToys Text Extractor uses — instant, no downloads); EasyOCR available as an optional fallback |
| Global hotkeys | `keyboard` |
| Clipboard | `pyperclip` + raw Win32 `keybd_event` |

## How It Works

Three independent pipelines, each triggered by its own hotkey. All of them run
off the UI thread, so the window never freezes and never steals focus from
whatever you're typing into.

**Dictation (hold-to-talk)**

The microphone stream is opened **once, at launch**, and continuously fills a
small rolling buffer. When you press the hotkey the app doesn't open a mic — it
just marks a position in that buffer a fraction of a second *before* your press.
That's the whole trick: opening a stream on demand costs 120-140 ms before the
first audio sample arrives, which is exactly long enough to clip the start of
your first word, every single time. Pre-rolling means your first word survives
even if you start talking slightly before you press.

    press hotkey  ->  remember which window had focus
                  ->  mark buffer position ~300 ms in the past
    release       ->  drain a short tail so the last syllable isn't cut
                  ->  slice the buffer, hand it to Whisper
                  ->  clean up the text (collapse repeats, drop hallucinations)
                  ->  copy to clipboard, send Ctrl+V to the remembered window
                  ->  restore whatever was on the clipboard before

The floating pill mirrors every stage (Ready / Recording / Transcribing /
Pasted) and, if a clip is discarded for being too short or too quiet, it says
so rather than failing silently.

**Read-aloud** tries three ways to find your selected text, cheapest first:

1. **UI Automation** — reads the highlight directly out of the other app. No
   clipboard, no keystrokes, no focus change, and it works even when that
   window isn't focused. Covers most apps (~70 ms).
2. **Clipboard copy** — for apps that don't expose their text to UI Automation.
   Briefly takes focus, copies, restores your clipboard. Skipped entirely when a
   terminal is focused, because there Ctrl+C means *interrupt* and would kill
   whatever command is running.
3. **OCR** — for text that isn't really text: scanned PDFs, images,
   copy-protected pages.

**OCR** grabs a region of the screen, runs Windows' built-in OCR engine over it
(about 10 ms, no model download), then shows and speaks the result.

**A note on model loading:** Whisper is loaded from your local cache first.
Left to its default behavior the library re-checks Hugging Face over the network
on *every* launch, which added ~3 minutes of startup on an already-downloaded
model. The network is now touched exactly once per model — the first time you
select it.

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

This creates a Python virtual environment and pulls all dependencies (~600 MB —
no PyTorch needed; OCR uses the Windows-native engine and Whisper-GPU uses the
slim NVIDIA runtime wheels).

**Run:**
```powershell
run.bat
```

Or double-click `run.bat` in File Explorer. First launch downloads the Whisper
speech model (one-time). OCR needs no download — it's built into Windows.

**Verify your install** (optional — run in a terminal):
```powershell
venv\Scripts\python.exe main.py --check
```
Reports PASS/WARN/FAIL for every component (mic, hotkeys, CUDA runtime, OCR,
VLC, offline/neural TTS). Add `--deep` to also load the Whisper model and prove
the transcription path. A non-zero exit means a **required** component is missing.

## Project Structure

```
VoiceAssistant/
├── main.py                  Entry shim (the app lives in the package below)
├── voiceassistant/          The application package
│   ├── app.py               Bootstrap: crash handlers, single instance, Qt
│   ├── window.py            Main window — orchestration + signal wiring
│   ├── widgets.py           Floating pill + hotkey-capture widget
│   ├── settings_dialog.py   Settings UI
│   ├── theme.py             Dark stylesheet
│   ├── recorder.py          Mic capture (always-open stream + pre-roll buffer)
│   ├── transcriber.py       Whisper transcription
│   ├── tts.py               edge-tts neural voices + VLC playback
│   ├── ocr.py               Screen capture + OCR (Windows-native, EasyOCR fallback)
│   ├── selection.py         Read-aloud's 3-tier selection grab
│   ├── uia.py               UI Automation selection reader (no clipboard needed)
│   ├── paste.py             Paste worker (clipboard-safe)
│   ├── winapi.py            All Win32 calls
│   ├── text.py              Pure text-cleanup logic
│   ├── config.py            Atomic settings read/write
│   ├── workers.py           Threading model (one worker per subsystem)
│   ├── metrics.py           Local reliability metrics (never stores text)
│   ├── selfcheck.py         The --check health probe
│   └── applog.py            Privacy-safe rotating log + crash visibility
├── tests/                   Unit + fault-injection suites, golden-audio corpus
├── requirements.txt         Python dependencies
├── setup.bat                First-time environment setup
├── run.bat                  Silent launcher (uses pythonw.exe)
├── create_shortcut.bat      Creates desktop shortcut
└── uninstall.bat            Removes startup entry, shortcut, venv, models
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

## Troubleshooting

**Start here.** This one command checks every moving part and prints PASS / WARN
/ FAIL per component (microphone, hotkey registration, CUDA runtime, OCR, VLC,
neural and offline voices):

```powershell
venv\Scripts\python.exe main.py --check
```

Add `--deep` to also load the Whisper model and prove the transcription path end
to end. A non-zero exit code means something **required** is missing.

The app also records its own reliability stats for each dictation — hold time,
audio length, volume peak, decode latency, and outcome. If dictation starts
feeling unreliable, read this before changing any settings:

```powershell
venv\Scripts\python.exe main.py --report
```

(`--last=50` limits it to the most recent 50.) It stores **counts only, never
your transcribed text.**

| Symptom | Cause and fix |
|---|---|
| First launch says "model still loading" | Normal — the speech model downloads once (~1.5 GB for `medium`). Later launches load from cache in a few seconds. |
| Dictation suddenly 10-20x slower, tray says DEGRADED | The GPU path failed and it fell back to CPU. Usually a CUDA/cuDNN problem — run `--check`. Restart the app after fixing. |
| Nothing pastes, or the wrong window gets the text | Click into the target window *before* holding the hotkey. Dictation pastes into whichever window had focus when you pressed. |
| Short words ("yes", "no") get dropped | They fell below the minimum-length or minimum-volume gate. The pill names the reason. Tune `min_record_seconds` / `min_record_peak` in `settings.json`. |
| Hotkey does nothing in *one* specific app | That app is probably running as administrator. Windows blocks keyboard hooks from a normal-privilege app into an elevated one — run this app as admin too, or use a different app. |
| Caps Lock toggles caps while dictating | Shouldn't happen — the app swallows the key when Caps Lock is bound. If it does, report it; it means the key suppression broke. |
| Read-aloud is silent | No neural voice usually means VLC is missing: `winget install VideoLAN.VLC`. If it speaks with the robotic Windows voice instead, the neural service is unreachable and it fell back to offline SAPI. |
| Read-aloud reads the wrong text (e.g. a browser address bar) | That app doesn't expose its selection properly. Re-select the text and try again; it will fall back to copy or OCR. |
| The `Fn` key won't bind | It can't. `Fn` is handled inside your keyboard's firmware and never reaches Windows, so no software can see it. Use `F9` or similar. |
| A hotkey fires in the browser too | Some defaults collide (`Ctrl+Shift+T` reopens a closed tab). Click the hotkey pill and rebind it. |

**Logs.** `debug.log` and `crash.log` are written next to `run.bat`. Neither ever
contains your dictated text or screen contents. Verbose logging is off by
default — set `"debug_logging": true` in `settings.json` to enable it.

You may see `Windows fatal exception: code 0x8001010d` in `crash.log`. That is a
harmless first-chance COM exception that Windows handles internally; the app
keeps running.

## Uninstalling

```powershell
uninstall.bat
```

Or double-click it. It walks through four steps and tells you what it's doing:

1. Removes the "start with Windows" entry from the registry
2. Removes the desktop shortcut
3. Removes the virtual environment, caches and logs (it asks whether to keep
   your `settings.json`)
4. Lists the downloaded Whisper models and **asks before deleting them** —
   several GB, and that cache is shared with any other AI tool on your machine,
   so only this app's models are ever touched

Then delete the folder itself, which the script can't do while running from
inside it. It tells you this at the end.

**It deliberately leaves Python and VLC alone** — this app didn't install them
and you may want them for other things. Remove those from Settings > Apps if
you're sure.

Nothing else to clean up: this app never writes to Program Files and changes no
system settings. Apart from that one startup entry and the model cache,
everything it creates lives inside its own folder.

## License

[MIT](LICENSE) — use it, change it, redistribute it. No warranty of any kind.
