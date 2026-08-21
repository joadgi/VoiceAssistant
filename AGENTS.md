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
| `voiceassistant/recorder.py` | `VoiceRecorder` — **always-open** mic stream + pre-roll ring buffer; GUI-thread tick owns metering/duration-cap/mic-health. |
| `voiceassistant/transcriber.py` | `Transcriber` + `TranscriptionResult` (faster-whisper, both-pass guards, job-bound context). |
| `voiceassistant/tts.py` | `TTSEngine` — edge-tts → VLC (live speed), per-utterance generations, pyttsx3 fallback, bounded network waits. |
| `voiceassistant/ocr.py` | `ScreenCapture` (mss) + `OCREngine` (Windows-native OCR default, EasyOCR fallback) + `RegionSelector`. |
| `voiceassistant/paste.py` | `Paster` — the paste worker: clipboard snapshot/restore + Win32 Ctrl+V, off the GUI thread. |
| `voiceassistant/selection.py` | `SelectionReader` — read-aloud's 3-tier selection grab (UIA → Ctrl+C sentinel → tell caller to OCR), off the GUI thread (mirrors `Paster`). |
| `voiceassistant/uia.py` | UI Automation selection reader — highlighted text with **no clipboard, no keystrokes, no focus switch**. |
| `voiceassistant/winapi.py` | ALL Win32/ctypes calls (foreground window, keystrokes, single-instance, startup registry). |
| `voiceassistant/text.py` | Pure text logic: repeat collapse, cleanup chain, hallucination denylist, paste sanitizing. 100% unit-tested. |
| `voiceassistant/config.py` | `Config` (ATOMIC saves, corrupt-file backup) + `DEFAULTS` + hotkey validation. |
| `voiceassistant/workers.py` | `SerialWorker` — **the threading law**: every subsystem owns exactly one worker+queue; no ad-hoc `threading.Thread` anywhere. |
| `voiceassistant/metrics.py` | Per-dictation metrics (JSONL, rolling, **never text**) + `--report` summary. |
| `voiceassistant/applog.py` | Privacy-safe rotating log (never logs payloads), opt-in debug, excepthooks + faulthandler. |
| `voiceassistant/selfcheck.py` | `python main.py --check` — no-GUI health probe (mic, hotkeys, CUDA, OCR, VLC, TTS). |
| `tests/` | Characterization + fault-injection suites (fast) and the golden-audio corpus gate (local, `RUN_CORPUS=1`). |
| `setup.bat` / `run.bat` / `create_shortcut.bat` | Env setup, silent launch (pythonw), desktop shortcut. |
| `uninstall.bat` | Removes the HKCU startup value, desktop shortcut, venv + local runtime files, and (after confirming) the `models--Systran--faster-whisper-*` HF cache dirs. Scoped by glob so unrelated HF models survive. Cannot delete its own folder — tells the user. |

## How it works (data flow)

**Dictation (push-to-talk):**
The mic stream is **already open** (opened once at launch) and continuously filling a ring
buffer. `hold hotkey` → capture foreground window HWND → mark the ring offset
`preroll_ms` BEFORE the press → `release` → after a short tail drain, slice the ring →
`Transcriber.transcribe()` → clean up text → the `Paster` worker copies to the clipboard
and sends Win32 `Ctrl+V` into the captured window (off the GUI thread; the prior clipboard
is restored afterward). The floating pill mirrors each state (Ready → Recording →
Transcribing → Pasted), **and names the reason when a clip is dropped**.

**Read-aloud:** hotkey → **3-tier selection grab** → `TTSEngine.speak()`.
1. **UIA** (`uia.get_selection`) reads the highlight directly — no clipboard, no
   keystrokes, no focus switch, and it works when the window ISN'T focused.
2. **Ctrl+C sentinel** (refocus → sentinel → Ctrl+C → poll → restore) for apps UIA
   doesn't expose. Skipped entirely for console windows.
3. **OCR the area around the cursor** (`window._read_ocr_fallback`) for text that
   isn't text — scanned PDFs, images, copy-protected content.
The tier is reported back so failures name the real cause and `--report` counts them.

**OCR:** hotkey (cursor region) or drag-selected region → `mss` grab → `OCREngine` →
text shown and auto-spoken.

**TTS:** `edge-tts` streams MP3 to a temp file; VLC plays it and `set_rate()` changes speed
live (no regeneration). Falls back to `pyttsx3` SAPI if neural/VLC fails.

## Key design decisions (the "why", for future reviews)

- **The mic stream is ALWAYS OPEN; recordings are ring-buffer slices**
  (`recorder.py`). Opening a stream per recording cost a **measured 117–137 ms
  before the first sample arrived** on this hardware (Yeti/MME) — the leading edge
  of the first word, lost on every single dictation, and short words ("yes") lost
  enough to fall under `min_record_seconds` and be dropped outright. That was the
  bulk of the "it doesn't hear me / I have to repeat myself" complaint. Now
  `start()` just marks an offset (**~30 µs measured**) `preroll_ms` in the PAST, so
  the first word survives even if you speak slightly before pressing, and `stop()`
  drains a short tail so the last syllable survives too. Verified live: a 1000 ms
  hold delivers ~1450 ms of audio (300 pre-roll + hold + ~150 tail).
  **Do not go back to opening a stream per recording.**
- **The audio callback does the minimum and emits NOTHING.** It downmixes, writes
  the ring, and tracks a peak. Metering, the duration cap and mic-health all run on
  a GUI-thread `QTimer`. The old per-block `level_update.emit()` from the audio
  thread contributed to the logged `audio input overflow x29`.
- **Recorder teardown is guaranteed three ways** — `close_stream()` (app exit /
  device change), `__del__`, and a **weakref** `atexit` hook. Recorder↔stream is a
  reference cycle, and letting the cycle collector reclaim it while PortAudio was
  still inside the callback **segfaulted the interpreter** (hit while running the
  suite). The `atexit` registration must stay a weakref: a bound method would pin
  every recorder forever and `__del__` would never run.
- **Push-to-talk hooks EVERY key in the combo, not one "trigger" key**
  (`_setup_hotkeys`). This was the headline reliability bug: with the modifier-only
  combo `ctrl+alt`, the derived trigger was `alt`, so Ctrl's keydown had no hook at
  all — pressing **Alt before Ctrl did nothing**, and since two keys pressed together
  land in arbitrary order, dictation silently failed to start about half the time.
  Releasing *any* combo key now ends the hold too. Locked down by
  `tests/integration/test_hotkey_register.py` (those tests fail against the old logic).
- **Read-aloud reads the selection via UIA FIRST** (`uia.py`). The Ctrl+C sentinel was
  the ONLY mechanism and it fails on copy-blocked content, on apps that remap Ctrl+C,
  whenever Windows refuses the focus switch — and it was actively dangerous with a
  terminal focused. Measured: UIA reads a full selection in **70–116 ms**, from the
  SerialWorker, **without focus**. Coverage isn't universal (20 of 30 open windows
  exposed UIA text; Acrobat and some Electron apps did not), which is why all three
  tiers exist. Gotchas, all learned the hard way:
  - **COM is apartment-threaded** — the client is cached PER THREAD and CoInitialize is
    called there, because this runs on SelectionReader's worker, not the GUI thread.
  - **Search order matters.** Do NOT "walk down from the window for the first
    TextPattern": in Chrome that finds the ADDRESS BAR (measured: 31 characters). The
    selection lives on the FOCUSED element; window descendants are only a fallback
    (Notepad/Word keep TextPattern on a child, so the window element alone finds
    nothing).
  - Traversal is time-boxed (`_BUDGET_S`) — UIA calls cross a process boundary and can
    block on a busy app.
- **Never send Ctrl+C into a console** (`winapi.is_console_window`). There it means
  INTERRUPT: read-aloud used to kill whatever command was running in the focused
  terminal. Covers conhost, Windows Terminal, ConEmu, mintty, PuTTY.
- **The read hotkey is SUPPRESSED.** It is a dedicated action key, and letting it
  through means it ALSO fires whatever the focused app binds to it — `ctrl+m` is
  Send/Receive in Outlook and indent in Word, so reading a selection was quietly
  acting on the user's documents.
- **A dedicated solo key is SUPPRESSED; a modifier never is** (`DEDICATED_SOLO_KEYS`,
  `should_suppress_hotkey`). Caps Lock is the best push-to-talk key available — home row,
  huge, and its scan code (58) is the only kind that does **not** overlap anything used in
  normal typing — but binding it un-suppressed would toggle caps on every dictation. So a
  single dedicated key (caps lock / scroll lock / insert / menu / num lock) is hooked with
  `suppress=True` and the app swallows it. Modifiers must never be suppressed —
  swallowing `ctrl` would break Ctrl system-wide.
  - **The suppressing callbacks MUST return falsy.** `keyboard` blocks a suppressed event
    only when the handler returns a falsy value, and in **PySide6 `Signal.emit()` returns
    `True`** — so the obvious `lambda e: sig.emit()` silently stops suppressing and Caps
    Lock starts toggling caps again. `_setup_hotkeys` uses explicit `def`s that emit as a
    statement; a test pins the falsy return.
  - **Right-hand modifiers are NOT usable as solo keys:** `keyboard.key_to_scan_codes`
    maps `right ctrl` → `(57629, 29, 57373)` — the *same set* as generic `ctrl` — and
    `hook_key` registers under every one, so `right ctrl` fires on LEFT Ctrl (every
    Ctrl+C would start a dictation). Same for `right alt`. `pause` shares a code with
    `ctrl` too. Don't "fix" this by re-adding them.
  - Note when testing hooks: `keyboard` passes its **own injected** events straight
    through (`is_replaying`), so `kb.press(scan_code)` does NOT fire your hooks. Synthetic
    keypresses cannot verify hook behavior here — reason from the scan-code tables.
- **Never trust a keyup — poll the key state** (`_on_ptt_watchdog`, 100 ms). Windows
  silently drops a low-level keyboard hook whose callback exceeds
  `LowLevelHooksTimeout`, and elevated/secure-desktop windows eat events. When that
  happened the release never fired and recording ran to the 120 s cap — `debug.log`
  shows it three times (08-08, 08-13, 08-17), once next to the overflow message,
  i.e. exactly the under-load hook-timeout case. A lost keyup now costs ~100 ms.
- **The model loads from the CACHE first** (`Transcriber._open_model`). faster-whisper
  otherwise revalidates against huggingface.co on EVERY launch: measured **176.3 s vs
  7.0 s** for an already-cached `large-v3`, i.e. ~3 minutes after each boot where the
  hotkey only says "model still loading" and the words are lost — plus an undocumented
  external call in an app whose premise is local dictation. The network is touched
  exactly once per model. **The cache/download decision is deliberately SEPARATE from
  the CUDA→CPU device fallback**: a cuDNN failure must not be mistaken for a cache miss
  (pointless multi-GB download) and a download must not silently pin the session to CPU.
- **Degraded is not the same as broken, and must still be LOUD** (`degraded` signal on
  `Transcriber` and `OCREngine` → `MainWindow._on_degraded`). A CUDA→CPU fallback is
  10–20x slower; as a log line plus a label behind a tray icon it is experienced as
  "dictation got slow" with no cause. It now raises a tray balloon and marks the model
  label DEGRADED until restart.
- **The app measures its own reliability** (`metrics.py`). Six capture bugs survived a
  month because the USER was the monitoring system and "feels unreliable" is not
  actionable. Every dictation now records hold/audio duration, peak, overflow count,
  decode latency, retry, outcome and paste result; `python main.py --report` summarises
  them (`--last=N` to window it). **Privacy: transcript text is NEVER written, only a
  character count** — same rule as `applog`. `metrics.jsonl` is gitignored and rolls at
  512 KB. If dictation ever feels off again, read the report before touching code.
- **A dead mic must be loud, not silent.** The tick watchdog notices a stream that
  stopped delivering, reports it (`stream_state`), and reopens. Recording into a
  dead device looks identical to success until the empty transcript arrives.
- **Silent drops are surfaced on the pill.** The app lives in the tray, so a
  status-bar-only "Recording ignored" was invisible and read as "it just didn't
  work". The gate also measures the user's **hold** time, not the padded slice.
- **VAD with a no-VAD retry** (`transcriber.py` `_run_transcribe`): VAD trims silence so
  Whisper stops hallucinating repeats/junk on pauses — but if the VAD pass returns empty,
  it retries **without** VAD so quiet/short speech is never lost. This was a real
  regression once; don't remove the retry.
- **Decoding uses beam search + the temperature ladder** (`transcriber.py`), which are
  faster-whisper's own defaults. This ran greedy (`beam_size=1, best_of=1,
  temperature=0.0`) — the fastest and least accurate setting, and pinning temperature
  to `0.0` **disabled the fallback retry**, so a decode that tripped the
  compression/logprob thresholds just shipped its bad text. Measured cost of the
  upgrade: **+54 ms per dictation** on a 3070. Don't trade it back for latency.
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
  Internal tuning knobs (`min_record_seconds`, `min_record_peak`, `preroll_ms`) live in
  both `DEFAULTS` and `settings.json`. Debug logging is opt-in (`debug_logging`, default
  off). `whisper_prompt` (default `""`) optionally biases Whisper toward your vocabulary
  and casing — it is opt-in because a prompt can leak into the transcript.
- **Whisper model choice:** `medium` is the shipped factory default; **`large-v3` is the
  most accurate model that is SAFE here** (corpus gate 13/13; 3.2 GB VRAM, 449 ms/clip
  vs medium's 308 ms on a 3070 — 8.7x realtime). Switching downloads the model once.
  Put any model change through the gate first:
  `RUN_CORPUS=1 CORPUS_MODEL=<name> pytest tests/test_corpus_gate.py`.
- **NEVER ship a `distil-*` Whisper model here.** `distil-large-v3` looks ideal on paper
  (near-`large-v3` accuracy, far faster) and **fails this app's hallucination gate**: on
  pure room noise it emits "Thank you." with `no_speech_prob` **0.087–0.163** — it is
  *confident* the noise is speech — where `medium` reports **0.875–0.960** and is
  correctly dropped. The distilled decoder's no-speech head is unreliable, so no
  threshold can separate it from genuine quiet speech, and the invented phrase is one
  the denylist deliberately excludes (people really do dictate "Thank you"). Hallucinated
  text pasted into the focused window is the worst failure this app has; it is not worth
  any latency win. The Settings dropdown therefore does not offer distil models.
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

> **Recommended dictate key: `caps lock`** (what Josh runs). It is the only class of key
> whose scan code never overlaps normal typing, it is the most comfortable key to hold
> while speaking, and the app suppresses it so it no longer toggles caps. A modifier-only
> combo like `ctrl+alt` works but is the weakest option — every `Ctrl+Alt+<key>` shortcut
> also starts a recording.

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
  Validate a NON-default model with `CORPUS_MODEL=<name>` (the gate used to only ever
  test `DEFAULTS['whisper_model']`, so it could not vet what the user actually runs).
- **The chain test:** `RUN_E2E=1 pytest tests/integration/test_end_to_end_live.py -s`
  crosses every seam in one run (real handlers → real ring buffer → real Whisper →
  real cleanup → real paste). Every bug in the 2026-08-17 audit lived in a seam that
  no other test crossed. It has two tiers: run it from an INTERACTIVE terminal to get
  the real-Ctrl+V tier; an agent/CI run cannot obtain foreground privilege and falls
  back to asserting the exact (hwnd, text) handed to the paste worker.
  **Never 'fix' that by patching `winapi.get_foreground_window`** — the real Paster
  then believes focus is correct and fires a real Ctrl+V into whatever window is in
  front. That was tried during development and pasted into an unrelated window.
- **Watchpoints:** the job-bound target HWND (never reintroduce a shared field), the
  monotonic job-id guard, the silent-drop thresholds, the VAD retry (its segment guards
  must stay on BOTH passes), and the threading law (`workers.SerialWorker` — no ad-hoc
  threads). **Capture-path watchpoints:** the always-open stream + pre-roll (never
  reintroduce per-recording stream opens), the audio callback staying signal-free, the
  three-way recorder teardown (weakref `atexit` — a strong ref defeats `__del__` and
  reopens the segfault), hooking every combo key, and the PTT watchdog. These interact;
  change them deliberately and finish with a live end-to-end voice test (models can't
  hear a human in CI).
- **Tests must never touch real runtime state** (`tests/conftest.py`). One autouse
  fixture redirects `metrics.METRICS_PATH`, `applog.LOG_PATH` and
  `applog.CRASH_LOG_PATH` to tmp (and drops the cached `applog._logger`, which is
  built once against LOG_PATH). Both leaks were real: the flow tests wrote dozens of
  synthetic rows into the live `metrics.jsonl`, and the fault-injection tests wrote
  **279 lines** of `simulated: no capture device` / `worker 'test' job failed` into
  the live `debug.log` — the file the user is told to read when dictation misbehaves.
  Test noise in a diagnostic log is worse than no log at all.
- **`crash.log` may contain `Windows fatal exception: code 0x8001010d`**
  (`RPC_E_CANTCALLOUT_ININPUTSYNCCALL`). These are FIRST-CHANCE COM exceptions that
  faulthandler records and COM then handles internally — the app keeps running, and
  faulthandler writes straight to the file WITHOUT calling the tray notifier, so they
  are not user-visible. They come from input-synchronous UIA calls; the read path
  only READS (verified: 8/8 captures, zero dumps) — it is `TextRange.Select()`, used
  by `test_uia_selection_live.py` to create a selection, that provokes them.
- **Tests must never open a real capture device.** Every MainWindow harness stubs
  `VoiceRecorder.open_stream`/`close_stream`; a live stream outliving a fixture is what
  segfaulted the suite. Drive the recorder synchronously instead: `_audio_callback(...)`
  to feed audio, `_on_tick()` for cap/health, `_finish_capture()` for the tail timer.
- Local pytest runs may need `--basetemp` redirected (the default
  `%TEMP%\pytest-of-*` dir can end up ACL-locked, which shows as ~29 unrelated errors).
