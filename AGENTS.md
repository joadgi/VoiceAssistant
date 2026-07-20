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
| OCR | **Windows-native `Windows.Media.Ocr`** via `winsdk` (default — ~10ms, zero heavy deps; DPI-correct physical-pixel capture); EasyOCR optional fallback (`pip install easyocr`, pulls the multi-GB torch stack) |
| Global hotkeys | `keyboard` |
| Paste/copy | `pyperclip` + raw Win32 `keybd_event` (via `ctypes`) |

## File map

The app lives in the `voiceassistant/` package (flat layout, dependencies
point downward only); root `main.py` is a 10-line entry shim for
run.bat/shortcuts/startup-registry compatibility.

| Module | Responsibility |
|---|---|
| `voiceassistant/app.py` | Bootstrap: crash handlers FIRST, single-instance mutex, QApplication. |
| `voiceassistant/window.py` | `MainWindow` — all orchestration and signal wiring. Renders state + dispatches jobs; never blocks. |
| `voiceassistant/widgets.py` | `RecordingIndicator` pill + the single `HotkeyCaptureWidget`. |
| `voiceassistant/settings_dialog.py` / `theme.py` | Settings UI, dark stylesheet. |
| `voiceassistant/recorder.py` | `VoiceRecorder` (sounddevice mic capture, guarded teardown). |
| `voiceassistant/transcriber.py` | `Transcriber` + `TranscriptionResult` (faster-whisper, both-pass guards, job-bound context). |
| `voiceassistant/tts.py` | `TTSEngine` — edge-tts → VLC (live speed), per-utterance generations, pyttsx3 fallback, bounded network waits. |
| `voiceassistant/ocr.py` | `ScreenCapture` (mss) + `OCREngine` (Windows-native OCR default, EasyOCR fallback) + `RegionSelector`. |
| `voiceassistant/paste.py` | `Paster` — the paste worker: clipboard snapshot/restore + Win32 Ctrl+V, off the GUI thread. |
| `voiceassistant/selection.py` | `SelectionReader` — read-aloud's selection grab (Ctrl+C sentinel + refocus), off the GUI thread (mirrors `Paster`). |
| `voiceassistant/winapi.py` | ALL Win32/ctypes calls (foreground window, keystrokes, single-instance, startup registry). |
| `voiceassistant/text.py` | Pure text logic: repeat collapse, cleanup chain, hallucination denylist, paste sanitizing. 100% unit-tested. |
| `voiceassistant/config.py` | `Config` (ATOMIC saves, corrupt-file backup) + `DEFAULTS` + hotkey validation. |
| `voiceassistant/workers.py` | `SerialWorker` — **the threading law**: every subsystem owns exactly one worker+queue; no ad-hoc `threading.Thread` anywhere. |
| `voiceassistant/applog.py` | Privacy-safe rotating log (never logs payloads), opt-in debug, excepthooks + faulthandler. |
| `voiceassistant/selfcheck.py` | `python main.py --check` — no-GUI health probe (mic, hotkeys, CUDA, OCR, VLC, TTS). |
| `tests/` | Characterization + fault-injection suites (fast) and the golden-audio corpus gate (local, `RUN_CORPUS=1`). |
| `setup.bat` / `run.bat` / `create_shortcut.bat` | Env setup, silent launch (pythonw), desktop shortcut. |

## How it works (data flow)

**Dictation (push-to-talk):**
`hold hotkey` → capture foreground window HWND → record mic to buffer → `release` →
`Transcriber.transcribe()` → clean up text → the `Paster` worker copies to the clipboard
and sends Win32 `Ctrl+V` into the captured window (off the GUI thread; the prior clipboard
is restored afterward). The floating pill mirrors each state (Ready → Recording →
Transcribing → Pasted).

**Read-aloud:** hotkey → refocus the prior window → Win32 `Ctrl+C` to grab the selection
via a clipboard sentinel → `TTSEngine.speak()`.

**OCR:** hotkey (cursor region) or drag-selected region → `mss` grab → `OCREngine` →
text shown and auto-spoken.

**TTS:** `edge-tts` streams MP3 to a temp file; VLC plays it and `set_rate()` changes speed
live (no regeneration). Falls back to `pyttsx3` SAPI if neural/VLC fails.

## Key design decisions (the "why", for future reviews)

- **VAD with a no-VAD retry** (`transcriber.py` `_run_transcribe`): VAD trims silence so
  Whisper stops hallucinating repeats/junk on pauses — but if the VAD pass returns empty,
  it retries **without** VAD so quiet/short speech is never lost. This was a real
  regression once; don't remove the retry.
- **Post-process repeat collapse** (`collapse_repeated_phrases` in `text.py`): catches
  word/phrase/sentence repeats as a safety net. Prefer this over aggressive transcribe-time
  filters.
- **Paste hygiene** (`paste.py` + `sanitize_for_paste` in `text.py`): strip control chars and
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
- `settings.json`, `debug.log`, and `crash.log` are **gitignored** (local runtime state).
  Internal tuning knobs (`min_record_seconds`, `min_record_peak`) live in both `DEFAULTS`
  and `settings.json`. Debug logging is opt-in (`debug_logging`, default off).
- The Whisper model downloads on first run (one-time, cached outside the repo). The
  default OCR backend is Windows-native — no model download, no PyTorch. **PyTorch is
  no longer a dependency** (it only ever served EasyOCR); Whisper-GPU gets its CUDA
  runtime from the `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` wheels (see
  `Transcriber._add_nvidia_dll_dirs`).
- **TTS dependency decision (2026-07-17):** Piper (fully local neural TTS) was evaluated
  as an edge-tts replacement and DEFERRED — edge-tts voice quality is materially better,
  the offline-private option already exists (SAPI fallback), and live speed control
  depends on VLC either way. Revisit if/when packaging for distribution.

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

- **Start in** `voiceassistant/window.py` `MainWindow` for orchestration and signal wiring.
- **Dictation accuracy/latency** → `voiceassistant/transcriber.py` (VAD params, both-pass
  segment guards, model size) and `voiceassistant/text.py` (cleanup chain, denylist).
- **Paste reliability / focus / the beep** → `voiceassistant/paste.py` + `winapi.py`.
- **Playback / voices / speed** → `voiceassistant/tts.py`.
- **Before ANY behavior change:** run `pytest tests -q` (fast suites) and, for anything
  touching the dictation pipeline, `RUN_CORPUS=1 pytest tests/test_corpus_gate.py`
  (the golden-audio gate — the objective definition of "dictation still works").
- **Watchpoints:** the job-bound target HWND (never reintroduce a shared field), the
  monotonic job-id guard, the silent-drop thresholds, the VAD retry (its segment guards
  must stay on BOTH passes), and the threading law (`workers.SerialWorker` — no ad-hoc
  threads). These interact; change them deliberately and finish with a live end-to-end
  voice test (models can't hear a human in CI).
