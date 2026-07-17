# Voice Assistant — Project Context

> **Canonical project doc.** `CLAUDE.md` and `AGENTS.md` are kept byte-identical so
> every tool reads the same truth — edit both, or copy one over the other.
> User-facing setup/usage lives in `README.md`; this file is the developer/agent map.

## What this is

A **local, private, Windows desktop dictation app** — and the dictation engine is the
point. Dictation (Whisper) and OCR (EasyOCR) run entirely on the user's own machine
(GPU with CPU fallback) — voice and screen contents never leave it. **Exception:**
read-aloud's *neural* voices use Microsoft's online `edge-tts` service (selected text
is sent to Microsoft for synthesis); the `pyttsx3` SAPI fallback is fully offline.
Never describe the whole app as "nothing sent to the cloud" — scope the claim.
The goal is to be a faster, cleaner alternative to Wispr Flow.

Three features, in priority order:

1. **Dictation (primary)** — hold a hotkey, speak, release; the transcription is pasted
   into whatever window had focus.
2. **Read-aloud (secondary)** — select text anywhere, press a hotkey, hear it in a neural voice.
3. **OCR screen-reader (secondary)** — capture on-screen text (images, PDFs, dialogs) and read it aloud.

## Stack

| Layer | Tech |
|---|---|
| UI | PySide6 (Qt), dark theme |
| Voice-to-text | `faster-whisper` (CTranslate2 Whisper) on CUDA, CPU fallback |
| Text-to-speech | `edge-tts` streaming neural voices → VLC playback (live speed); `pyttsx3` SAPI offline fallback |
| Screen capture | `mss` |
| OCR | EasyOCR (GPU, CPU fallback) |
| Global hotkeys | `keyboard` |
| Paste/copy | `pyperclip` + raw Win32 `keybd_event` (via `ctypes`) |

## File map

| File | Responsibility |
|---|---|
| `main.py` | Entry point, PySide6 UI, all orchestration: hotkey registration, dictation flow, Win32 paste, the floating `RecordingIndicator` pill, system tray, settings dialog, and the single `HotkeyCaptureWidget`. |
| `voice_engine.py` | `VoiceRecorder` (sounddevice mic capture) + `Transcriber` (faster-whisper). |
| `screen_reader.py` | `ScreenCapture` (mss) + `OCREngine` (EasyOCR) + `RegionSelector` (drag-to-select overlay). |
| `tts_engine.py` | `TTSEngine` — edge-tts → VLC playback with live speed; pyttsx3 fallback. |
| `config.py` | `Config` + `DEFAULTS` + hotkey `normalize_hotkey` / `validate_hotkey`. |
| `setup.bat` / `run.bat` / `create_shortcut.bat` | Env setup, silent launch (pythonw), desktop shortcut. |

## How it works (data flow)

**Dictation (push-to-talk):**
`hold hotkey` → capture foreground window HWND → record mic to buffer → `release` →
`Transcriber.transcribe()` → clean up text → `paste_to_window()` copies to clipboard and
sends Win32 `Ctrl+V` into the captured window. The floating pill mirrors each state
(Ready → Recording → Transcribing → Pasted).

**Read-aloud:** hotkey → refocus the prior window → Win32 `Ctrl+C` to grab the selection
via a clipboard sentinel → `TTSEngine.speak()`.

**OCR:** hotkey (cursor region) or drag-selected region → `mss` grab → `OCREngine` →
text shown and auto-spoken.

**TTS:** `edge-tts` streams MP3 to a temp file; VLC plays it and `set_rate()` changes speed
live (no regeneration). Falls back to `pyttsx3` SAPI if neural/VLC fails.

## Key design decisions (the "why", for future reviews)

- **VAD with a no-VAD retry** (`voice_engine.py` `_run_transcribe`): VAD trims silence so
  Whisper stops hallucinating repeats/junk on pauses — but if the VAD pass returns empty,
  it retries **without** VAD so quiet/short speech is never lost. This was a real
  regression once; don't remove the retry.
- **Post-process repeat collapse** (`collapse_repeated_phrases` in `main.py`): catches
  word/phrase/sentence repeats as a safety net. Prefer this over aggressive transcribe-time
  filters.
- **Paste hygiene** (`paste_to_window` / `sanitize_for_paste`): strip control chars and
  flatten newlines before paste, and **never inject `Escape`** into the target — both were
  sources of an audible Windows "ding" on single-line/chat inputs.
- **One hotkey-capture path** (`HotkeyCaptureWidget`): two earlier duplicate implementations
  and a dead inline-edit path were removed. Keep it to one.
- **Hotkeys are fully user-configurable — never hardcode them** (`validate_hotkey`):
  every action's hotkey (Dictate / Read / OCR) is a per-user setting edited via the
  capture pills and stored in `settings.json`; `DEFAULTS` are factory defaults only.
  Valid: single safe keys (F-keys, Caps Lock…), normal combos, and **modifier-only
  combos of 2+ modifiers** (e.g. `ctrl+alt` — a deliberate product decision for
  hold-to-talk ergonomics; docs warn about AltGr on international layouts). Blocked
  only where a binding would break basic function: bare typing keys (letters/digits/
  space/punctuation — they'd fire mid-sentence), a single bare modifier, and `escape`.
- **Push-to-talk** = `on_press_key(trigger)` that only fires when *all* combo keys are held,
  plus `on_release_key(trigger)` to stop. The release handler no-ops (and stays silent)
  unless PTT is active.
- **Floating pill doesn't steal focus** (`WA_ShowWithoutActivating` +
  `WindowDoesNotAcceptFocus`), so clicking it to start/stop dictation leaves the target
  window focused for paste.
- **Single instance** via a named Win32 mutex. Note: `venv\Scripts\pythonw.exe` is a
  launcher stub that spawns the real interpreter as a child — so "two pythonw processes"
  (stub + child) is **one** logical instance, not a duplicate.

## Constraints & gotchas

- **The `Fn` key cannot be bound** — it's handled in keyboard firmware and never reaches
  Windows, so no software can capture it. Recommend an F-key (F9) instead.
- Avoid **Windows-key** hotkeys (OS intercepts them) and common browser combos (`Ctrl+T/W/R`).
- **VLC must be installed** for neural TTS playback (`winget install VideoLAN.VLC`).
- `settings.json` and `debug.log` are **gitignored** (local runtime state). Internal tuning
  knobs (`min_record_seconds`, `min_record_peak`) live in both `DEFAULTS` and `settings.json`.
- Whisper + EasyOCR models download on first run (~1 GB) and are cached outside the repo.

## Setup & run

1. `setup.bat` — creates the venv, installs PyTorch (CUDA) + deps.
2. `run.bat` — launches silently via `pythonw.exe`.
3. First launch downloads models (~1 GB, one-time).

## Default hotkeys

| Action | Default | Behavior |
|---|---|---|
| Dictate | `Ctrl+Shift+R` | Hold to record, release to transcribe + paste |
| Read selection | `Ctrl+Shift+T` | Press to read highlighted text; press again to stop |
| OCR at cursor | `Ctrl+Shift+S` | Capture region around cursor, OCR, read aloud |

All are editable inline — click a hotkey pill and press your combo (single key or combo).

## Reviewing / extending this codebase

- **Start in** `main.py` `MainWindow` for orchestration and signal wiring.
- **Dictation accuracy/latency** → `voice_engine.py` (`Transcriber`, VAD params, model size).
- **Paste reliability / focus / the beep** → `paste_to_window` and the Win32 helpers in `main.py`.
- **Playback / voices / speed** → `tts_engine.py`.
- **Watchpoints:** the anti-duplicate guards (`_last_audio_id`, `_paste_lock`, the ~1.2s
  identical-text guard), the silent-drop thresholds, and the VAD retry — these interact, so
  change them deliberately and test dictation end-to-end (it can't be verified by import alone).
