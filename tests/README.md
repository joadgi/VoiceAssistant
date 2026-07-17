# Voice Assistant — Test Suite

## Layout

| File | What it covers | Speed |
|---|---|---|
| `test_characterization.py` | Pure logic: repeat-collapse, paste sanitizing, light cleanup, hotkey normalize/validate (the **dynamic-hotkey contract**), config persistence | <1s |
| `test_tts_stall.py` | TTS network-stall regression: stall → offline fallback, `stop()` unwedges, fast-fail path intact | ~20s |
| `corpus_runner.py` | **Golden-audio corpus**: drives the real Whisper pipeline (gate → VAD pass → no-VAD retry → cleanup) over `fixtures/audio/*.wav` | ~1 min (GPU) |
| `generate_fixtures.py` | Regenerates the corpus WAVs (offline SAPI speech + programmatic silence/noise at controlled levels) | ~30s |

## Running

```powershell
venv\Scripts\python.exe -m pytest tests -q          # unit suites
venv\Scripts\python.exe tests\generate_fixtures.py   # (re)create WAV fixtures
venv\Scripts\python.exe tests\corpus_runner.py       # run corpus, print results
venv\Scripts\python.exe tests\corpus_runner.py baseline  # rewrite baseline.json
```

## Corpus design

- **WAV fixtures are gitignored** (binary, machine-specific SAPI voice); the
  generator script is committed and rebuilds them deterministically per machine.
- **`fixtures/baseline.json` is committed** — it records what the real pipeline
  produced (raw VAD pass, whether the no-VAD retry fired, final text, cleanup
  output, per-pass latency, device). It is the reference point for "did this
  change make dictation better or worse?"
- Corpus runs **locally only** (needs the cached Whisper model and ideally the
  GPU). Do not wire it into cloud CI.
- Speech assertions must be **tolerance-based** (contains-words / no-denylist
  tokens), never exact-match — Whisper on GPU is not bit-deterministic.

## What the 2026-07-17 baseline proved

- `breath_click_0p35s.wav` → VAD pass empty → **no-VAD retry hallucinated
  `"you"`** → `"You."` would be pasted. The retry is the hallucination vector
  (bug B1), reproducible on demand.
- `intentional_no_no_no.wav` → Whisper transcribed `"No no no."` **correctly**;
  our own `_dedupe_repeated` collapsed it to `"No."` (bug M5 — cleanup, not
  Whisper).
- `quiet_yes.wav` has mean RMS **0.0013** — any Phase 2 RMS gate must sit well
  below that or real quiet speech gets dropped.
- `sanitize_settings` dedupe hole: resetting a colliding hotkey to a default
  that IS the collision leaves two actions on one combo (found by
  `test_sanitize_settings_dedupe_hole_CURRENT_BEHAVIOR`).

Tests marked `*_CURRENT_BEHAVIOR` document known gaps on purpose — Phase 2/3/4
flip them deliberately, one by one, never by accident.
