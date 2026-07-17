"""Generate the dictation QUALITY battery — a diverse, reproducible speech corpus.

This is the sibling of ``tests/generate_fixtures.py`` (the golden-audio corpus),
but aimed at *accuracy + latency measurement* rather than the trust contract.
Speech is synthesized offline via Windows SAPI (pyttsx3) so the battery is
reproducible on this machine with zero privacy concerns; noise/silence clips are
generated programmatically at controlled levels; noisy speech is the clean float
array with seeded gaussian noise mixed in at a target SNR.

Every clip carries an EXPECTED transcript (what the pipeline should paste),
written alongside the WAVs in ``manifest.json`` — the test module reads that to
score word-error-rate. WAV files are gitignored (``*.wav``); this script
(committed) regenerates them and rewrites the committed ``manifest.json``.

Run:  venv/Scripts/python.exe tests/integration/gen_speech_battery.py

Then the quality gate can consume them:
  $env:RUN_DICT_QUALITY="1"; venv\\Scripts\\python.exe -m pytest tests/integration/test_dictation_quality.py -v -s
"""

import json
import os
import sys
import tempfile
import wave

import numpy as np

SR = 16000
SEED = 42
GEN_VERSION = 1

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MANIFEST_PATH = os.path.join(FIXTURES_DIR, "manifest.json")


# ---------------------------------------------------------------------------
# WAV helpers (same on-disk format as tests/generate_fixtures.py: int16 mono @16k)
# ---------------------------------------------------------------------------
def save_wav(name, audio_f32):
    """Save float32 [-1, 1] mono @16k as int16 PCM WAV; return (path, dur, peak, rms)."""
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    path = os.path.join(FIXTURES_DIR, name)
    pcm = np.clip(audio_f32, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    dur = len(audio_f32) / SR
    peak = float(np.max(np.abs(audio_f32))) if len(audio_f32) else 0.0
    rms = float(np.sqrt(np.mean(audio_f32**2))) if len(audio_f32) else 0.0
    print(f"  {name:34s} {dur:6.2f}s  peak={peak:.4f}  rms={rms:.4f}")
    return path


def load_wav_as_f32(path):
    """Load a PCM WAV -> mono float32 @16k (resamples/downmixes if needed)."""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != SR:
        n_out = int(round(len(x) * SR / sr))
        x = np.interp(
            np.linspace(0.0, len(x) - 1.0, n_out), np.arange(len(x)), x
        ).astype(np.float32)
    return x


# ---------------------------------------------------------------------------
# Speech synthesis (offline SAPI) + signal helpers
# ---------------------------------------------------------------------------
def tts_to_f32(text, rate=175):
    """Synthesize text with SAPI at a given rate -> float32 mono @16k."""
    import pyttsx3

    tmp = os.path.join(tempfile.gettempdir(), "va_battery_tts.wav")
    engine = pyttsx3.init()
    engine.setProperty("rate", rate)
    engine.save_to_file(text, tmp)
    engine.runAndWait()
    engine.stop()
    x = load_wav_as_f32(tmp)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return x


def peak_normalize(x, peak):
    cur = float(np.max(np.abs(x))) if len(x) else 0.0
    if cur <= 0:
        return x
    return (x * (peak / cur)).astype(np.float32)


def silence(seconds):
    # Tiny dither so buffers aren't pathological all-zero (peak stays below the
    # record gate, so these are dropped BEFORE Whisper ever sees them).
    return np.random.default_rng(7).normal(0, 1e-6, int(SR * seconds)).astype(np.float32)


def add_gaussian_noise(clean, snr_db, rng):
    """Mix seeded gaussian noise into a clean speech array at a target SNR (dB).

    SNR is computed over the whole clip (signal power / noise power). The final
    mix is scaled down uniformly only if it would clip — uniform scaling leaves
    the SNR unchanged, so the label stays honest.
    """
    sig_power = float(np.mean(clean.astype(np.float64) ** 2))
    if sig_power <= 0:
        return clean.astype(np.float32).copy()
    noise_power = sig_power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(0.0, np.sqrt(noise_power), len(clean)).astype(np.float32)
    mixed = clean.astype(np.float32) + noise
    peak = float(np.max(np.abs(mixed)))
    if peak > 0.99:
        mixed = mixed * (0.99 / peak)
    return mixed.astype(np.float32)


# ---------------------------------------------------------------------------
# Battery definition
# ---------------------------------------------------------------------------
# ~60-word paragraph (spoken-friendly prose — the strongest clean-accuracy signal).
PARAGRAPH = (
    "Thanks for joining the call today. I want to walk through the plan for the "
    "next quarter and make sure we are aligned. First, we will finalize the budget "
    "and confirm the timeline with each team. Then we will review the open risks, "
    "assign clear owners, and agree on the metrics that tell us whether we are "
    "actually making progress."
)
RATE_SENTENCE = (
    "The quarterly report is ready for review and the numbers look strong."
)


def main():
    rng = np.random.default_rng(SEED)
    print(f"Writing dictation quality battery to {FIXTURES_DIR}")
    clips = []

    def add_speech(file, text, f32, max_wer, note, rate=175, noisy=False, snr_db=None):
        save_wav(file, f32)
        clips.append(
            {
                "file": file,
                "kind": "speech",
                "expected": text,
                "noisy": noisy,
                "snr_db": snr_db,
                "rate": rate,
                "max_wer": max_wer,
                "note": note,
            }
        )

    def add_noise(file, f32, note):
        save_wav(file, f32)
        clips.append(
            {
                "file": file,
                "kind": "noise",
                "expected": "",
                "noisy": True,
                "snr_db": None,
                "rate": None,
                "max_wer": None,
                "note": note,
            }
        )

    # --- (a) short command --------------------------------------------------
    open_cmd = peak_normalize(tts_to_f32("Open the report.", 175), 0.30)
    add_speech("cmd_open_report.wav", "Open the report.", open_cmd, 0.15, "short command")

    # --- (b) long ~60-word paragraph ---------------------------------------
    para = peak_normalize(tts_to_f32(PARAGRAPH, 175), 0.30)
    add_speech("paragraph_60w.wav", PARAGRAPH, para, 0.15, "~60-word paragraph")

    # --- (c) numbers / dates / currency (adversarial FORMATTING) ------------
    # SAPI speaks these as words; Whisper re-condenses to digits/symbols, but the
    # exact rendering ($1,250 vs "1250 dollars", 3rd vs third, PM vs p.m.) is a
    # coin-flip. The WER normalizer folds most of that away; the looser bound
    # covers the rest. This clip tests survival, not glyph-perfect formatting.
    num_text = "Transfer $1,250 on March 3rd at 4:45 PM."
    num = peak_normalize(tts_to_f32(num_text, 170), 0.30)
    add_speech("numbers_currency.wav", num_text, num, 0.40, "numbers/dates/currency", rate=170)

    # --- (d) email + URL ----------------------------------------------------
    email_text = (
        "Email me at josh dot gibson at example dot com, "
        "or visit example dot com slash docs."
    )
    email = peak_normalize(tts_to_f32(email_text, 165), 0.30)
    add_speech("email_url.wav", email_text, email, 0.30, "email + URL", rate=165)

    # --- (e) technical / jargon --------------------------------------------
    jargon_text = (
        "Our microservice authenticates with an OAuth token "
        "and caches the response in Redis."
    )
    jargon = peak_normalize(tts_to_f32(jargon_text, 175), 0.30)
    add_speech("technical_jargon.wav", jargon_text, jargon, 0.30, "technical jargon")

    # --- (f) mixed punctuation & question ----------------------------------
    q_text = (
        "Are you ready for the review? If so, please bring the slides, "
        "the budget, and your laptop."
    )
    q = peak_normalize(tts_to_f32(q_text, 175), 0.30)
    add_speech("questions_punct.wav", q_text, q, 0.15, "mixed punctuation & question")

    # --- (g) one sentence at 3 speaking rates ------------------------------
    rate_arrays = {}
    for tag, r, mw in (("slow", 130, 0.15), ("med", 175, 0.15), ("fast", 215, 0.20)):
        arr = peak_normalize(tts_to_f32(RATE_SENTENCE, r), 0.30)
        rate_arrays[tag] = (arr, r)
        add_speech(f"rate_{tag}.wav", RATE_SENTENCE, arr, mw, f"speaking rate {r} wpm", rate=r)

    # --- (h) noisy variants of representative clean sentences --------------
    # Two SNR levels (20 dB = light hiss, 10 dB = clearly noisy). Applied to the
    # prose clips where WER is the real signal; the adversarial-formatting and
    # jargon clips are left clean (noise on top would multiply runtime without
    # adding accuracy signal).
    noisy_bases = [
        ("cmd_open_report", "Open the report.", open_cmd, 175),
        ("rate_med", RATE_SENTENCE, rate_arrays["med"][0], 175),
        ("questions_punct", q_text, q, 175),
    ]
    for base, text, clean, r in noisy_bases:
        for snr in (20, 10):
            noisy = add_gaussian_noise(clean, snr, rng)
            add_speech(
                f"{base}_noise{snr}db.wav",
                text,
                noisy,
                0.35,
                f"{base} @ {snr} dB SNR",
                rate=r,
                noisy=True,
                snr_db=snr,
            )

    # --- (i) noise-only / near-silence: must NEVER produce pasteable text ---
    add_noise("near_silence_2s.wav", silence(2.0), "near-silence (dropped by record gate)")
    room = rng.normal(0.0, 0.006, SR * 3).astype(np.float32)
    add_noise("room_noise_3s.wav", room, "room noise, clears gate (must not paste)")
    white = rng.normal(0.0, 0.02, SR * 3).astype(np.float32)
    add_noise("white_noise_loud_3s.wav", white, "louder white-noise stress (must not paste)")

    manifest = {
        "sample_rate": SR,
        "seed": SEED,
        "generator_version": GEN_VERSION,
        "note": (
            "Expected transcripts are what the pipeline SHOULD paste. Speech "
            "assertions are WER-tolerance based, never exact-match (Whisper on "
            "GPU is not bit-deterministic). Regenerate WAVs with "
            "gen_speech_battery.py; this manifest is committed."
        ),
        "clips": clips,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    n_speech = sum(1 for c in clips if c["kind"] == "speech")
    n_noise = sum(1 for c in clips if c["kind"] == "noise")
    print(f"\nWrote {len(clips)} clips ({n_speech} speech, {n_noise} non-speech).")
    print(f"Manifest: {MANIFEST_PATH}")


if __name__ == "__main__":
    sys.exit(main())
