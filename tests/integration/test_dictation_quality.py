"""Dictation QUALITY gate — drives the REAL faster-whisper model on real
synthesized speech and measures accuracy + latency empirically.

This is the objective, numbers-on-the-table answer to "how good is dictation,
really?" — the corpus gate (tests/test_corpus_gate.py) proves the trust contract
(noise never pastes, quiet speech never drops); this proves the *quality*:

  * ACCURACY — word-error-rate (Levenshtein over normalized words) vs the
    expected transcript. Clean prose must land WER <= 0.15; noisy speech
    <= 0.35; adversarial-formatting/jargon clips carry a per-clip bound
    (manifest ``max_wer``). Noise/silence must produce NOTHING pasteable.
  * LATENCY — the VAD pass, the no-VAD retry (when it fires), and the total are
    timed; real-time-factor (RTF = processing_sec / audio_sec) is asserted
    against a device-aware budget (tight on GPU, lenient on CPU fallback).
  * NO STATE BLEED — 5 clips fed back-to-back through ONE Transcriber each come
    back as themselves (no cross-contamination) and total time stays ~linear.

Reuses the heavy machinery from tests/corpus_runner.py: ``build_transcriber()``
(real model, CUDA -> CPU int8 fallback) and ``load_wav()``. Local-only and
model-loading; opt in explicitly (like RUN_CORPUS gates the corpus):

    venv\\Scripts\\python.exe tests/integration/gen_speech_battery.py   # once
    $env:RUN_DICT_QUALITY="1"; venv\\Scripts\\python.exe -m pytest tests/integration/test_dictation_quality.py -v -s

The ``-s`` surfaces the summary table.
"""

import json
import os
import re
import sys
import time

import pytest

# Repo root on sys.path so `tests.corpus_runner` / `voiceassistant` import
# (mirrors tests/test_corpus_gate.py — there is no conftest/ini).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

FIXTURES_DIR = os.path.join(_HERE, "fixtures")
MANIFEST_PATH = os.path.join(FIXTURES_DIR, "manifest.json")

RUN = bool(os.environ.get("RUN_DICT_QUALITY"))
pytestmark = pytest.mark.skipif(
    not RUN,
    reason="dictation quality gate is a local, model-loading run — set RUN_DICT_QUALITY=1",
)

# Headline WER bounds (the spec defaults); individual clips may loosen via the
# manifest's per-clip ``max_wer`` (adversarial number/URL/jargon formatting).
WER_CLEAN = 0.15
WER_NOISY = 0.35

# Latency budgets, branched on the device the model actually loaded on.
RTF_BUDGET = {"cuda": 1.0, "cpu": 8.0}
TOTAL_MS_CEILING = {"cuda": 15000, "cpu": 120000}
# RTF is only asserted on clips long enough to amortize fixed per-call overhead.
RTF_MIN_AUDIO_SEC = 2.5


# ===========================================================================
# WER — pure stdlib, unit-checkable without the model
# ===========================================================================
_ORDINAL_WORDS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
    "eleventh": "11", "twelfth": "12", "thirteenth": "13", "fourteenth": "14",
    "fifteenth": "15", "sixteenth": "16", "seventeenth": "17", "eighteenth": "18",
    "nineteenth": "19", "twentieth": "20", "thirtieth": "30",
}
_CARDINAL_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
}


def normalize_for_wer(text):
    """Canonicalize text for word-error scoring.

    Applied IDENTICALLY to reference and hypothesis, so every transform is
    safe: it can only ever help two spellings of the same thing agree, never
    manufacture a mismatch. Folds away the differences that don't matter for
    dictation quality — case, punctuation, and the symbol/word and
    digit/word renderings of numbers, currency, times, emails and URLs
    ('$1,250' == '1250', 'March 3rd' == 'march third', 'josh dot gibson at
    example dot com' == 'josh.gibson@example.com').
    """
    t = (text or "").lower().strip()
    # email / URL: symbol <-> spoken-word equivalence
    t = t.replace("@", " at ")
    t = re.sub(r"(?<=\w)\.(?=\w)", " dot ", t)   # example.com -> example dot com
    t = t.replace("/", " slash ")
    # currency / grouping / times: bare digits compare
    t = t.replace("$", " ")
    t = re.sub(r"(?<=\d),(?=\d)", "", t)         # 1,250 -> 1250
    t = t.replace(":", " ")                       # 4:45 -> 4 45
    # ordinals -> cardinal digits: 3rd -> 3, third -> 3
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", t)
    for word, digit in _ORDINAL_WORDS.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    for word, digit in _CARDINAL_WORDS.items():
        t = re.sub(rf"\b{word}\b", digit, t)
    # drop remaining punctuation, collapse whitespace
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _edit_distance(ref, hyp):
    """Levenshtein distance between two token lists (word-level)."""
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        ri = ref[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ri == hyp[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[m]


def wer(reference, hypothesis):
    """Word error rate = word-level edit distance / reference length.

    Empty reference: 0.0 if the hypothesis is also empty, else 1.0.
    """
    ref = normalize_for_wer(reference).split()
    hyp = normalize_for_wer(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


# ===========================================================================
# Manifest (read at import so tests can parametrize over clip names)
# ===========================================================================
def _load_manifest():
    if not os.path.exists(MANIFEST_PATH):
        return {"clips": []}
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_MANIFEST = _load_manifest()
_CLIPS = {c["file"]: c for c in _MANIFEST["clips"]}
_CLEAN = [c["file"] for c in _MANIFEST["clips"] if c["kind"] == "speech" and not c["noisy"]]
_NOISY = [c["file"] for c in _MANIFEST["clips"] if c["kind"] == "speech" and c["noisy"]]
_NONSPEECH = [c["file"] for c in _MANIFEST["clips"] if c["kind"] == "noise"]

# Five clips with mutually-exclusive distinctive tokens, for the no-state-bleed
# rapid-sequential test. Each token appears in exactly one clip's expected text.
_SEQ = [
    ("cmd_open_report.wav", "report"),
    ("rate_med.wav", "quarterly"),
    ("questions_punct.wav", "laptop"),
    ("numbers_currency.wav", "march"),
    ("email_url.wav", "example"),
]


# ===========================================================================
# Pipeline helpers — mirror the app's transcribe path (verbatim logic)
# ===========================================================================
def _run_pipeline_timed(transcriber, audio):
    """VAD pass -> no-VAD retry when empty; time each pass. (App logic verbatim.)"""
    import numpy as np

    audio_sec = len(audio) / 16000.0
    t0 = time.perf_counter()
    vad_text = transcriber._run_transcribe(audio, use_vad=True)
    t1 = time.perf_counter()
    retried = False
    final = vad_text
    if not vad_text:
        retried = True
        final = transcriber._run_transcribe(audio, use_vad=False)
    t2 = time.perf_counter()
    total_ms = (t2 - t0) * 1000.0
    return {
        "final": final,
        "retried": retried,
        "vad_ms": (t1 - t0) * 1000.0,
        "retry_ms": ((t2 - t1) * 1000.0) if retried else 0.0,
        "total_ms": total_ms,
        "audio_sec": audio_sec,
        "peak": float(np.max(np.abs(audio))) if len(audio) else 0.0,
        "rtf": (total_ms / 1000.0) / audio_sec if audio_sec > 0 else float("inf"),
    }


def _decide(final, retried, audio_sec):
    """Replicate _on_transcription_ready: what (if anything) would be pasted."""
    from voiceassistant.text import clean_transcript, is_probable_hallucination
    from voiceassistant.transcriber import TranscriptionResult

    result = TranscriptionResult(
        text=final, job_id=0, duration_s=audio_sec, retried=retried, no_speech=not final
    )
    if result.no_speech:
        return {"cleaned": "", "would_paste": False, "verdict": "no_speech"}
    cleaned = clean_transcript(final, light=True)
    if is_probable_hallucination(result, cleaned):
        return {"cleaned": cleaned, "would_paste": False, "verdict": "suppressed_hallucination"}
    return {"cleaned": cleaned, "would_paste": bool(cleaned.strip()), "verdict": "paste"}


def _passes_gate(audio):
    """The app's silent-drop gate (min_record_seconds / min_record_peak)."""
    import numpy as np

    from voiceassistant.config import DEFAULTS

    duration = len(audio) / 16000.0 if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    return duration >= float(DEFAULTS["min_record_seconds"]) and peak >= float(
        DEFAULTS["min_record_peak"]
    )


# ===========================================================================
# Fixtures — one model load per module (VRAM-friendly), warmed before timing
# ===========================================================================
@pytest.fixture(scope="module")
def transcriber():
    if not os.path.exists(MANIFEST_PATH):
        pytest.skip(
            "no battery fixtures — run: "
            "venv/Scripts/python.exe tests/integration/gen_speech_battery.py"
        )
    from tests.corpus_runner import build_transcriber, load_wav

    t = build_transcriber()
    # Warm up: one untimed pass so CUDA kernel/JIT warmup doesn't tax RTF on the
    # first real clip. Any present clip works.
    for name in _CLIPS:
        try:
            t._run_transcribe(load_wav(os.path.join(FIXTURES_DIR, name)), use_vad=True)
            break
        except Exception:
            continue
    return t


@pytest.fixture(scope="module")
def battery(transcriber):
    """Run every clip through the real pipeline once; key results by filename."""
    from tests.corpus_runner import load_wav

    results = {}
    for name, clip in _CLIPS.items():
        audio = load_wav(os.path.join(FIXTURES_DIR, name))
        if clip["kind"] == "noise":
            if not _passes_gate(audio):
                entry = {
                    "cleaned": "", "would_paste": False, "verdict": "dropped_by_gate",
                    "wer": None, "total_ms": 0.0, "vad_ms": 0.0, "retry_ms": 0.0,
                    "rtf": None, "audio_sec": len(audio) / 16000.0, "retried": False,
                }
            else:
                timed = _run_pipeline_timed(transcriber, audio)
                dec = _decide(timed["final"], timed["retried"], timed["audio_sec"])
                entry = {**timed, **dec, "wer": None}
        else:
            timed = _run_pipeline_timed(transcriber, audio)
            dec = _decide(timed["final"], timed["retried"], timed["audio_sec"])
            entry = {**timed, **dec}
            entry["wer"] = wer(clip["expected"], dec["cleaned"])
        results[name] = entry

    data = {
        "meta": {
            "model": transcriber.model_size,
            "device": transcriber.device,
            "compute_type": transcriber.compute_type,
        },
        "results": results,
    }
    _print_summary(data)
    return data


def _print_summary(data):
    meta = data["meta"]
    print(
        f"\n=== Dictation quality battery — {meta['model']} on {meta['device']} "
        f"({meta['compute_type']}) ==="
    )
    hdr = f"{'clip':32s} {'kind':6s} {'WER':>6s} {'total_ms':>9s} {'RTF':>6s}  verdict"
    print(hdr)
    print("-" * len(hdr))
    for name in sorted(data["results"]):
        e = data["results"][name]
        clip = _CLIPS[name]
        kind = "noisy" if (clip["kind"] == "speech" and clip["noisy"]) else clip["kind"]
        werf = f"{e['wer']:.3f}" if e["wer"] is not None else "  -  "
        rtf = f"{e['rtf']:.3f}" if e.get("rtf") is not None else "  -  "
        print(
            f"{name:32s} {kind:6s} {werf:>6s} {e['total_ms']:9.0f} {rtf:>6s}  "
            f"{e['verdict']}"
        )


# ===========================================================================
# Tests
# ===========================================================================
def test_fixtures_present():
    assert os.path.exists(MANIFEST_PATH), (
        "battery not generated — run: "
        "venv/Scripts/python.exe tests/integration/gen_speech_battery.py"
    )
    assert _CLEAN and _NONSPEECH, "manifest has no clips; regenerate the battery"
    for name in _CLIPS:
        assert os.path.exists(os.path.join(FIXTURES_DIR, name)), f"missing WAV: {name}"


@pytest.mark.parametrize("name", _CLEAN)
def test_clean_speech_accuracy(battery, name):
    clip = _CLIPS[name]
    entry = battery["results"][name]
    bound = clip.get("max_wer") or WER_CLEAN
    assert entry["would_paste"], (
        f"{name} produced nothing pasteable (verdict={entry['verdict']}); "
        f"expected {clip['expected']!r}"
    )
    assert entry["wer"] <= bound, (
        f"{name}: WER {entry['wer']:.3f} > {bound:.2f}\n"
        f"  expected: {clip['expected']!r}\n"
        f"  got:      {entry['cleaned']!r}"
    )


@pytest.mark.parametrize("name", _NOISY)
def test_noisy_speech_accuracy(battery, name):
    clip = _CLIPS[name]
    entry = battery["results"][name]
    bound = clip.get("max_wer") or WER_NOISY
    assert entry["would_paste"], (
        f"{name} produced nothing pasteable (verdict={entry['verdict']}); "
        f"expected {clip['expected']!r}"
    )
    assert entry["wer"] <= bound, (
        f"{name} (SNR {clip['snr_db']} dB): WER {entry['wer']:.3f} > {bound:.2f}\n"
        f"  expected: {clip['expected']!r}\n"
        f"  got:      {entry['cleaned']!r}"
    )


@pytest.mark.parametrize("name", _NONSPEECH)
def test_nonspeech_never_pastes(battery, name):
    entry = battery["results"][name]
    assert not entry["would_paste"], (
        f"{name} produced pasteable text {entry['cleaned']!r} "
        f"(verdict={entry['verdict']}) — noise/silence must never dictate"
    )


def test_latency_rtf_within_budget(battery):
    device = battery["meta"]["device"]
    rtf_budget = RTF_BUDGET.get(device, RTF_BUDGET["cpu"])
    ms_ceiling = TOTAL_MS_CEILING.get(device, TOTAL_MS_CEILING["cpu"])

    # Hard wall-clock ceiling on every clip that actually ran.
    for name, e in battery["results"].items():
        if e.get("total_ms"):
            assert e["total_ms"] < ms_ceiling, (
                f"{name}: {e['total_ms']:.0f}ms exceeds {ms_ceiling}ms on {device}"
            )

    # RTF on the median of the longer speech clips (amortizes fixed overhead;
    # median shrugs off a single cold outlier).
    rtfs = sorted(
        e["rtf"]
        for name, e in battery["results"].items()
        if _CLIPS[name]["kind"] == "speech"
        and e.get("rtf") is not None
        and e["audio_sec"] >= RTF_MIN_AUDIO_SEC
    )
    assert rtfs, "no speech clip long enough to measure RTF"
    median_rtf = rtfs[len(rtfs) // 2]
    assert median_rtf <= rtf_budget, (
        f"median RTF {median_rtf:.3f} > {rtf_budget} on {device} "
        f"(all: {[round(x, 3) for x in rtfs]})"
    )


def test_rapid_sequential_no_state_bleed(transcriber, battery):
    """Five clips back-to-back through one Transcriber: each returns as itself
    (no cross-contamination) and the total stays roughly linear."""
    from tests.corpus_runner import load_wav

    seq = [(n, tok) for n, tok in _SEQ if n in _CLIPS]
    assert len(seq) >= 3, "need the sequential clips in the manifest"
    all_tokens = [tok for _, tok in seq]

    t0 = time.perf_counter()
    outputs = []
    for name, _tok in seq:
        audio = load_wav(os.path.join(FIXTURES_DIR, name))
        timed = _run_pipeline_timed(transcriber, audio)
        dec = _decide(timed["final"], timed["retried"], timed["audio_sec"])
        outputs.append(dec["cleaned"].lower())
    total_ms = (time.perf_counter() - t0) * 1000.0

    for (name, token), out in zip(seq, outputs):
        clip = _CLIPS[name]
        own_words = set(re.findall(r"\w+", normalize_for_wer(clip["expected"])))
        assert token in out, (
            f"{name}: expected distinctive token {token!r} in output {out!r} "
            f"(dropped or corrupted)"
        )
        # A foreign clip's token only signals bleed if it is NOT also part of
        # THIS clip's own sentence. (e.g. the rate clip legitimately says
        # "quarterly report", so "report" appearing there is not contamination.)
        leaked = [o for o in all_tokens
                  if o != token and o in out and o not in own_words]
        assert not leaked, (
            f"{name}: STATE BLEED — output {out!r} contains other clips' "
            f"tokens {leaked}"
        )
        # Strongest no-bleed signal: the output must WER-match its OWN expected
        # transcript. If a neighbouring clip had contaminated it, this spikes.
        own_wer = wer(clip["expected"], out)
        assert own_wer <= (clip.get("max_wer") or WER_NOISY), (
            f"{name}: output {out!r} does not match its own expected "
            f"{clip['expected']!r} (WER {own_wer:.2f}) — possible contamination"
        )

    # Roughly linear: sequential total shouldn't exceed ~1.6x the sum of the
    # same clips' standalone times (both share the warmed module transcriber).
    baseline = sum(
        battery["results"][name]["total_ms"] for name, _ in seq
    )
    assert total_ms <= 1.6 * baseline + 1000.0, (
        f"sequential {total_ms:.0f}ms >> standalone sum {baseline:.0f}ms "
        f"— non-linear slowdown suggests state/resource leak"
    )
