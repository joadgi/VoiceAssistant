"""Generate the golden-audio corpus fixtures (16 kHz mono int16 WAVs).

Speech clips are synthesized offline via Windows SAPI (pyttsx3) so the corpus is
reproducible on this machine with zero privacy concerns; silence/noise clips are
generated programmatically with controlled levels. WAV files are gitignored —
this script (committed) regenerates them; `baseline.json` (committed) records
what the real pipeline produced from them.

Run:  venv/Scripts/python.exe tests/generate_fixtures.py
"""

import os
import sys
import wave
import tempfile

import numpy as np

SR = 16000
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "audio")


# ---------------------------------------------------------------------------
# WAV helpers
# ---------------------------------------------------------------------------
def save_wav(name, audio_f32):
    """Save float32 [-1, 1] mono @16k as int16 PCM WAV."""
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
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
    print(f"  {name:32s} {dur:5.2f}s  peak={peak:.4f}  rms={rms:.4f}")
    return path


def load_wav_as_f32(path):
    """Load any PCM WAV -> mono float32 @16k."""
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
# Speech synthesis (offline SAPI)
# ---------------------------------------------------------------------------
def tts_to_f32(text, rate=170):
    """Synthesize text with SAPI -> float32 mono @16k."""
    import pyttsx3

    tmp = os.path.join(tempfile.gettempdir(), "va_fixture_tts.wav")
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
    # Tiny dither so buffers aren't pathological all-zero.
    return (np.random.default_rng(7).normal(0, 1e-6, int(SR * seconds))).astype(
        np.float32
    )


def gap(ms):
    return silence(ms / 1000.0)


# ---------------------------------------------------------------------------
# Fixture definitions
# ---------------------------------------------------------------------------
def main():
    rng = np.random.default_rng(42)
    print(f"Writing corpus to {OUT_DIR}")

    # --- Non-speech: these must NEVER produce pasted text -------------------
    save_wav("silence_2s.wav", silence(2.0))

    # White noise at RMS ~0.006: peak clears the current 0.008 peak-gate, so it
    # reaches Whisper — the exact "noises" trigger.
    noise = rng.normal(0.0, 0.006, SR * 3).astype(np.float32)
    save_wav("room_noise_3s.wav", noise)

    # Quieter noise, borderline at the current gate.
    noise_q = rng.normal(0.0, 0.002, SR * 3).astype(np.float32)
    save_wav("room_noise_quiet_3s.wav", noise_q)

    # A 0.35s click/breath-like burst (band-limited noise with an envelope).
    n = int(SR * 0.35)
    burst = rng.normal(0.0, 1.0, n).astype(np.float32)
    env = np.exp(-np.linspace(0.0, 6.0, n)).astype(np.float32)
    burst = peak_normalize(burst * env, 0.05)
    save_wav("breath_click_0p35s.wav", np.concatenate([gap(80), burst, gap(200)]))

    # --- Real speech: these must NEVER be dropped ---------------------------
    yes = tts_to_f32("Yes.")
    save_wav("quiet_yes.wav", peak_normalize(yes, 0.02))

    sent = tts_to_f32(
        "The quarterly report is ready for review and the numbers look strong."
    )
    save_wav("normal_sentence.wav", peak_normalize(sent, 0.30))

    nums = tts_to_f32("The meeting is on July twenty first at three thirty PM.")
    save_wav("numbers_dates.wav", peak_normalize(nums, 0.30))

    para = tts_to_f32(
        "Here is the first point. The second point follows from it. "
        "Finally, the third point wraps everything up."
    )
    # 2.5s trailing silence — the classic hallucinated-tail trigger.
    save_wav(
        "long_paragraph_trailing_silence.wav",
        np.concatenate([peak_normalize(para, 0.30), silence(2.5)]),
    )

    # --- Repeat semantics ----------------------------------------------------
    # Artifact-style stutter: the same word 3x with tight gaps.
    the = peak_normalize(tts_to_f32("the"), 0.30)
    save_wav(
        "stutter_the_x3.wav",
        np.concatenate([the, gap(150), the, gap(150), the, gap(250)]),
    )

    # Artifact-style phrase doubling.
    phrase = peak_normalize(tts_to_f32("send the file"), 0.30)
    save_wav(
        "repeat_phrase_x2.wav", np.concatenate([phrase, gap(200), phrase, gap(250)])
    )

    # INTENTIONAL repeat spoken naturally as one utterance — must be preserved.
    nono = tts_to_f32("no no no")
    save_wav("intentional_no_no_no.wav", peak_normalize(nono, 0.30))

    print("Done.")


if __name__ == "__main__":
    main()
