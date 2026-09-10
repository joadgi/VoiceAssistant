# Voice Assistant — Project Context

> **Canonical project doc.** `CLAUDE.md` and `AGENTS.md` are kept byte-identical so
> every tool reads the same truth — edit both, or copy one over the other.
> User-facing setup/usage lives in `README.md`; this file is the developer/agent map.

## What this is

A **local, private, Windows desktop dictation app** — and the dictation engine is the
point. Dictation (Whisper), OCR, and the default Kokoro neural read-aloud voices run
entirely on the user's own machine (GPU/CPU as described below). Optional voices marked
`[Online Neural]` use Microsoft's `edge-tts` service and send the selected text to
Microsoft for synthesis; explicitly selected SAPI voices are also fully offline. Never
describe every voice as local — scope the claim to the selected backend.
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
| Text-to-speech | Local Kokoro ONNX neural synthesis → one raw PCM VLC stream (default); optional online `edge-tts`; explicit `pyttsx3` SAPI option |
| Screen capture | `mss` |
| OCR | **Windows-native `Windows.Media.Ocr`** via `winsdk` (default — ~10ms, zero heavy deps; DPI-correct physical-pixel capture); EasyOCR optional fallback (`pip install easyocr`, pulls the multi-GB torch stack) |
| Global hotkeys | `keyboard` |
| Paste/copy | `pyperclip` + checked Win32 `SendInput` batches (via `ctypes`) |

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
| `voiceassistant/tts.py` | `TTSEngine` — local Kokoro blocks or one online edge-tts generator → one VLC stream, per-utterance generations, explicit pyttsx3 option, bounded waits. Carries the STOP CONTRACT and VOICE CONTRACT (in-file). |
| `voiceassistant/ocr.py` | `ScreenCapture` (mss) + `OCREngine` (Windows-native OCR default, EasyOCR fallback) + `RegionSelector`. |
| `voiceassistant/chord_hotkey.py` | `ChordHotkey` — the global chord matcher shared by read-aloud AND OCR; modifiers always pass through, only matched non-modifier down/up pairs can be consumed, cached key state reconciled against Windows. The app contains no `keyboard.add_hotkey`. |
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
| `tests/` | Characterization + fault-injection suites (fast), the keyboard-safety suite (`test_keyboard_safety.py` — modifier passthrough, checked input batches, stale-key reconciliation), the source-encoding gate (`test_source_encoding.py`), the TTS stop/voice regression suite, and the golden-audio corpus gate (local, `RUN_CORPUS=1`). |
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

**TTS:** the default local path phonemizes the complete selection once, then streams
Kokoro's native punctuation-aware batches through the TTS `SerialWorker`. The first
batch starts one raw 24 kHz PCM VLC callback stream; later batches fill the same stream
faster than playback consumes them while retaining Kokoro's sentence/clause pauses.
Speed is snapshotted once per Speak. Kokoro generates at no more than a quality-safe
1.2x; PyAV/FFmpeg `atempo` supplies higher speeds up to 2.6x without changing pitch.
VLC `set_rate` is not used; slider changes apply to the next read. Optional online voices use
one `edge-tts.Communicate` generator and one buffered VLC stream. LibVLC's native
callback consumes either stream, so there is no extra Python producer thread. A neural
selection never changes into SAPI; `pyttsx3` runs only when explicitly selected.

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
- **Read-aloud and OCR never suppress or replay modifiers, and share ONE
  matcher.** `ChordHotkey` observes both press orders of modifier-only combos,
  latches one action per hold, and always returns True for Ctrl/Alt/Shift/Win.
  For a chord with a normal key it consumes only a matched trigger down/up pair.
  OCR used to run on `kb.add_hotkey`, which does **not** suppress, so
  `ctrl+shift+s` also reached the focused window — arriving in Chrome and VS
  Code as Save As. Routing it through the same matcher removed the **last**
  `add_hotkey` in the app; do not reintroduce it in either form, since the
  non-suppressing variant runs the same modifier state machine that was found
  delaying and replaying Ctrl/Alt.
  Two consequences worth knowing before changing a binding:
  - A **modifier-only** chord can never be kept out of the focused app (a
    modifier must pass through), so `ctrl+alt` also fires every
    `Ctrl+Alt+<key>` shortcut. Measured: it is the ONLY shape the app cannot
    swallow. That is why read-aloud ships on a dedicated key.
  - A **single-key** binding also captures its modifier variants: with
    `scroll lock` bound, Shift+ScrollLock and Ctrl+ScrollLock fire too, because
    the chord only requires that key to be held. Pick a key whose variants the
    user does not need — this rules out `insert` (Shift+Insert pastes in
    terminals) and F-keys (Shift/Ctrl+F9 are live in editors).
- **Copy and paste share one clipboard/input transaction lock.** Workers wait
  for Windows modifier state to be clear and abort safely on timeout. One
  checked SendInput batch contains Ctrl-down, key-down/up, Ctrl-up; partial or
  exceptional delivery attempts release-only cleanup, never repeats the action.
  Zero accepted events do not trigger cleanup because the app owns no key.
  Paste failure retains the dictation for manual paste; copy restores its
  clipboard snapshot in a finally block. No transcript text goes into logs.
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
- **The PTT watchdog has an explicit evidence boundary.** Unsuppressed
  bindings use native Windows state, independent of the hook cache. Suppressed
  Caps Lock/solo keys never reach Windows' accepted state, so polling that
  state would incorrectly end recording. Those bindings retain hook-event
  state and the recording-duration cap. They are not independently protected
  against a completely lost raw key-up; do not claim otherwise.
- **A hook's own cached key state is NOT evidence that a key is held**
  (`chord_hotkey.py` `_reconcile`). This is the half of the 2026-09-09 Ctrl fix
  that was missed: that commit stopped the app *sending* keystrokes while a
  modifier is held, but the matcher still *believed* a stale cached one, because
  a hook only knows the events it is delivered. One dropped key-up latched a
  modifier forever with no recovery short of a restart. Measured with the shipped
  `ctrl+alt` chord: after a lost Ctrl key-up **every later Alt press fired
  read-aloud**, so Alt+Tab read the selection; with a normal trigger the same
  stale modifier **also swallowed the keystroke** (a plain `M` fired read-aloud
  and never reached the document); and if every combo key latches, `active` never
  goes false, so `_latched` never resets and read-aloud goes **silently dead**.
  The held set is now reconciled against Windows' accepted state on every event.
  Two exclusions carry the correctness, do not remove either:
  - **The current event's own key is never checked.** A low-level hook runs
    BEFORE the event reaches the rest of the system, so that state can still read
    "up" for the key being delivered. Checking it drops the key just received and
    breaks every chord.
  - **Modifiers only.** A key this matcher CONSUMED never reaches Windows'
    accepted state, so that state cannot judge it — the same boundary as the PTT
    watchdog above. `MapVirtualKeyW` is also untrustworthy for some codes
    `keyboard` enumerates (measured: ctrl's `57629` → VK_PAUSE, the Windows key's
    `91`/`92` → `0xF1`/`0xEA`, scroll lock's `57414` → VK_CANCEL), so the probe
    maps first and then requires a known modifier VK.
  `_blocked` is deliberately untouched — that is what keeps a consumed down/up
  pair matched when a modifier is reconciled away between the two edges. The
  probe is INJECTED, not imported, so the module still makes no Windows calls of
  its own and stays hermetically testable, and it fails **open**: a probe error
  falls back to cached state rather than cutting a genuine hold short. Cost over
  20,000 events: **0.9 µs** median with nothing held (the probe is skipped
  outright), **32 µs** median / **1.07 ms** worst case with three modifiers
  cached, against a `LowLevelHooksTimeout` budget of ~300 ms.
- **A dropped hook is a KNOWN, UNMITIGATED failure mode — do not claim
  otherwise.** `keyboard`'s listener is a bare `GetMessage` pump
  (`_winkeyboard.listen`) with no hook-health check and no re-registration, so if
  Windows unhooks it (LowLevelHooksTimeout, UAC, secure desktop) **every** hotkey
  dies silently until restart. An auto-recovering watchdog was designed and
  deliberately NOT built: the only hook-independent liveness signal is
  `GetLastInputInfo`, which counts mouse input too, so "system input recent but
  our hook silent" cannot be distinguished from a user who moused for minutes
  without typing — acting on it would re-register hooks in the input path
  spuriously. Probing liveness directly would mean injecting a key into the
  user's focused window, which is the exact bug class the 2026-09-09/09-10 work
  removed. 292 logged hotkey presses show no instance of it here, so it stays
  documented rather than mitigated. Revisit only with log evidence.
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
- **Stop must interrupt EVERY TTS backend, and `runAndWait()` is not one**
  (`tts.py` STOP CONTRACT). `pyttsx3.runAndWait()` blocks inside the SAPI
  driver loop until the whole text has been spoken, so on the offline path
  `stop()` was a **complete no-op**: measured, the old code did not unwind
  within a 5 s cap on a ~20 s passage — audio ran to the end while `stop()`
  had already set `_speaking = False`, so `is_speaking` said idle and the next
  hotkey press queued a SECOND utterance behind the still-playing first. The
  offline path now runs through pyttsx3's **external** loop (`startLoop(False)` +
  `iterate()`), checking the job's stop event every ~10 ms, and is chunked so
  even a driver without external-loop support over-runs by one sentence rather
  than the whole document. Measured after: neural **172 ms**, offline
  **234 ms** to full unwind, with `stop()` itself costing 0.1–20 ms on the
  calling thread. **Never reintroduce a blocking speak-until-finished call.**
- **VLC's `play()` is asynchronous, so the warm-up poll must honour stop too.**
  The 40×25 ms wait for `is_playing()` had no stop check — a Stop pressed there
  kept playing for up to a further second and then called `set_rate` on it. A
  `_vlc_lock` also closes the inversion where `stop()` (GUI thread) landed
  between `set_media()` and `play()` (worker thread) and the next chunk started
  anyway.
- **Local neural is the reliable default; speed is generated into its PCM.** Live
  measurement proved the Microsoft service could not sustain high-speed playback:
  600 characters at `+110%` took **36.3 s to synthesize** but contained only **18.5 s
  of audio**, so any finite streaming lead eventually ran dry. Kokoro CPU inference on
  this machine runs materially faster than its 2.6x output and has no network jitter.
  It feeds one raw PCM VLC callback stream from Kokoro's native prepared batches.
- **Prepare local text ONCE and freeze one speed per utterance.** An extra 180-character
  splitter looked continuous at the VLC layer but erased Kokoro's punctuation pause at
  every artificial boundary. A live 5,553-character read became **39 independently
  prepared voice segments**. Reading the slider during generation made it worse: one
  queued stream contained 2.60x, 2.50x, 2.20x, and 1.90x blocks. The full selection is
  now phonemized once, native batch pauses are preserved, and the requested speed is
  snapshotted when Speak starts. A slider change deliberately applies to the next read;
  the Playback controls make this visible and lock Voice/Speed during a local read.
- **Normalize ambiguous domain syntax before phonemization.** Amazon terms need
  deterministic spoken forms — `ASIN`/`SKU`, mixed
  letter-number identifiers, and dollar prices are expanded before synthesis because
  the raw phonemizer otherwise drops plurals, invents identifier syllables, and reads
  `$69.99` without clear dollar/cents units. Ordinary prose and years stay unchanged.
- **Strip source markup before phonemization.** Read-aloud can receive Markdown source,
  not rendered prose. On the user's exact report excerpt, eSpeak literally phonemized
  `1\.` and every `\-` bullet as "backslash," then pronounced the hidden local path in
  `[label](<C:/...>)` as invented words. `prepare_text_for_speech` keeps visible labels
  and prose, removes Markdown escapes/link targets, and punctuates list items. Logs may
  record input/spoken character counts, never either payload.
- **High playback speed comes from pitch-preserving tempo compression, not high-rate
  Kokoro generation.** On the same 919-character excerpt, local Whisper recovered
  **99.1%** of words from Michael at model input 1.0 but only **84.0%** at 1.98; the
  generated audio contained mutations such as "combo-owner" and "fitter consequence."
  Generating at model input **1.2** and applying PyAV/FFmpeg `atempo` to the requested
  **1.98x** restored **99.1%** recovery. Higher requested rates use that same quality-safe
  model input. Tempo filtering runs synchronously on the TTS worker per native batch;
  no subprocess or Python thread is added. Never use VLC `set_rate()` here: VLC returned
  success for `2.1` on the raw callback stream but live wall time stayed at 1.0x.
- **Pace raw PCM reads to the sample clock.** VLC's raw-audio demux reads callback
  streams aggressively. On the user's exact 941-character passage, Kokoro produced
  **44.959 s** of ordered PCM but VLC reached `Ended` after **20.250 s**. That made the
  UI say complete while output was still queued; the next Speak stopped the remainder,
  experienced as skipped sections. `_PacedAudioStream` exposes a 250 ms lead and then
  meters bytes at 24 kHz mono s16 (**48,000 bytes/s**). The same passage completed in
  **45.218 s** (0.259 s from its PCM duration). Pacing waits must remain stop-aware.
- **A neural voice is a hard choice; never switch it to SAPI automatically.**
  The old fallback converted a transient neural startup delay into an unexpected
  robotic Windows voice. Stopping that voice and pressing Read again could then
  overlap the still-draining neural request. A neural failure now stops the job
  and reports `chosen neural voice unavailable`; SAPI speaks only when the user
  explicitly selects an `[Offline]` voice in the dropdown.
- **A failure mid-utterance TRUNCATES; it never re-reads.** The old handler
  fell back to `_speak_offline(text)` for the WHOLE text no matter how much had
  already played, so a VLC hiccup three sentences in made you hear the opening
  twice, the second time robotic. `_speak_job` tracks whether audio actually
  reached the speakers; any failure after that truncates and never re-reads.
- **Retry the neural path on FAST failures only** (`_speak_neural`). edge-tts
  throws transient websocket/403/DNS errors that one cheap retry recovers; a
  **stall** (our own `TimeoutError` after `FIRST_AUDIO_TIMEOUT`) is never
  retried, because that would buy a second full timeout of dead air before the
  error is finally reported.
- **Online neural read-aloud is ONE PROTECTED STREAM, not sentence requests.** The old
  producer opened a fresh edge-tts websocket every `<=240` characters and
  played the first file immediately. At the user's `2.1x` speed that tiny lead
  repeatedly ran dry: a long silent pause at a sentence boundary, then
  catch-up when the next request completed. Waiting for the complete MP3 fixed
  that but made long selections take too long to start. Concurrent mini-MP3s
  improved startup but preserved independent encoder/prosody seams. The current
  path uses one `Communicate` generator for the whole selection and starts one
  VLC callback stream after its protected lead, while the same TTS worker keeps
  filling it. Short selections use a one-second lead; reads over 600 characters
  use three seconds to absorb service jitter at 2x+ speed. **Do not treat an
  empty buffer as an audible underrun:** VLC aggressively reads ahead into its
  own decoder/cache, so the callback waits for the bounded edge-tts request to
  write, finish, fail, or be stopped. A `0.75s` callback cutoff was tried and
  caused false `playback buffer ran dry` failures about three seconds into
  otherwise healthy reads. Stop cancels and closes the active async request and
  wakes the callback within ~50 ms; real network stalls remain bounded.
  The normal SAPI external-loop path likewise queues the complete selection
  once; sentence chunks exist only for a driver without interruptible loop
  support.
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
- **Tray Quit must stop `QApplication` explicitly.** `app.py` sets
  `setQuitOnLastWindowClosed(False)` so hiding/closing the main window does not
  end a tray-first app. After `closeEvent` performs the full recorder/worker/VLC
  teardown, `quit_app()` must call `QApplication.quit()` or a windowless
  pythonw process and stale tray objects survive.

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
  default OCR backend is Windows-native — no model download, no PyTorch. PyTorch is
  **no longer declared** (it only ever served EasyOCR); `requirements.txt` asks for the
  slim `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` wheels instead, which
  `Transcriber._add_nvidia_dll_dirs` registers.
  **But do NOT assume an existing venv matches that.** Measured 2026-09-10 on this
  machine: the nvidia wheels are **not installed at all**, `torch 2.5.1+cu121` is
  (**4.58 GB**), and GPU dictation is genuinely running on torch's CUDA DLLs —
  `--check` correctly reports `provided by torch`, and `_add_nvidia_dll_dirs` is a
  no-op here. So on this venv **torch is load-bearing for GPU transcription**;
  uninstalling it would drop dictation to CPU int8 (10–20x slower). The self-check's
  wording is the truth, not drift — believe it over this file.
  Reclaiming that 4.58 GB is a real but SEPARATE task, not a cleanup to fold into
  something else: install the two wheels, confirm `--check` reports
  `nvidia wheels: cublas, cudnn`, re-run
  `RUN_CORPUS=1 CORPUS_MODEL=large-v3 pytest tests/test_corpus_gate.py` and confirm
  `--report` still says `on cuda`, and only then remove torch. Both runtimes ship
  cuBLAS/cuDNN, so installing the wheels alongside torch risks a DLL version clash —
  do it deliberately, with the corpus gate as the arbiter, never as a drive-by.
- **TTS dependency decision (2026-08-27):** Kokoro ONNX is the default read-aloud
  backend because edge-tts could not produce audio as fast as the user's high-speed playback.
  `setup.bat` downloads the official 164 MB FP16 model plus 28 MB voice pack. Inference
  is deliberately forced to CPU: it measured faster than playback, while the available
  ONNX GPU wheel expected CUDA 13 and conflicted with the CUDA 12 Whisper runtime.
  Online Microsoft voices remain clearly marked optional choices; Piper remains deferred.

## Setup & run

1. `setup.bat` — creates the venv, installs dependencies, and downloads local Kokoro assets.
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
- **Playback / voices / speed / Stop** → `voiceassistant/tts.py`. Read the
  STOP CONTRACT and VOICE CONTRACT comments there BEFORE changing that file;
  `tests/test_tts_stop_and_voice.py` pins both (12 of its 15 cases fail against
  the pre-2026-08-26 engine).
- **Before ANY behavior change:** run `pytest tests -q` (fast suites) and, for anything
  touching the dictation pipeline, `RUN_CORPUS=1 pytest tests/test_corpus_gate.py`
  (the golden-audio gate — the objective definition of "dictation still works").
  For anything touching read-aloud, also run
  `RUN_TTS_EVAL=1 pytest tests/test_tts_eval.py tests/integration/test_tts_stress_live.py -v -s`;
  it is the objective word-fidelity, speed, voice, VLC, Stop, and replacement gate.
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
  threads). **Read-aloud watchpoints:** no blocking speak-until-finished call on
  ANY TTS backend, the VLC warm-up's stop check, the neural voice id never
  reaching SAPI, and truncate-don't-re-read on a mid-utterance failure.
  **Keyboard watchpoints:** modifiers are never suppressed or replayed; only a
  matched non-modifier down/up PAIR may be consumed; hook callbacks must return
  falsy to suppress (a Qt `Signal.emit()` returns `True`); `ChordHotkey._reconcile`
  keeps its two exclusions (current key exempt, modifiers only) and fails open;
  injected input stays one checked `SendInput` batch whose failure path releases
  only and never repeats the action.
  **Capture-path watchpoints:** the always-open stream + pre-roll (never
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
