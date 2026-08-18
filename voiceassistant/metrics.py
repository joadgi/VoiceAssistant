"""Per-dictation metrics — local, privacy-safe, rolling.

WHY THIS EXISTS: six real capture bugs survived for a month because the app
never noticed it was degrading — the USER was the monitoring system, and
"dictation feels unreliable" is not something you can act on. This module
turns that into numbers: how long you held the key, how much audio actually
arrived, whether it was dropped and why, how long transcription took, whether
the paste landed. `python main.py --report` summarises it.

PRIVACY: transcript text is NEVER written here, only a character COUNT (which
is what catches truncation). Same rule as applog: no payloads, ever. The file
is local and gitignored, and nothing is uploaded.
"""

import json
import os
import time

from . import applog
from .config import CONFIG_DIR

METRICS_PATH = os.path.join(CONFIG_DIR, "metrics.jsonl")
MAX_BYTES = 512 * 1024  # then roll once to .1 — bounded disk use, no growth

# Outcomes, worst-to-best. Anything not "pasted" is a dictation the user had to
# think about, which is the number that matters.
OUTCOME_PASTED = "pasted"
OUTCOME_PANEL = "panel"           # no paste target (recorded into the window)
OUTCOME_PASTE_FAILED = "paste_failed"
OUTCOME_NO_SPEECH = "no_speech"
OUTCOME_HALLUCINATION = "hallucination"
OUTCOME_DROPPED_SHORT = "dropped_short"
OUTCOME_DROPPED_QUIET = "dropped_quiet"
OUTCOME_MIC_ERROR = "mic_error"

_BAD = (OUTCOME_PASTE_FAILED, OUTCOME_NO_SPEECH, OUTCOME_DROPPED_SHORT,
        OUTCOME_DROPPED_QUIET, OUTCOME_MIC_ERROR)


def _roll_if_needed():
    try:
        if os.path.exists(METRICS_PATH) and os.path.getsize(METRICS_PATH) > MAX_BYTES:
            old = METRICS_PATH + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(METRICS_PATH, old)
    except OSError:
        pass


def record(outcome, **fields):
    """Append one dictation record. Never raises — metrics must not break
    dictation, ever."""
    try:
        _roll_if_needed()
        row = {"ts": round(time.time(), 3), "outcome": outcome}
        for k, v in fields.items():
            if v is None:
                continue
            row[k] = round(v, 4) if isinstance(v, float) else v
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        applog.dbg("metrics write failed")


def load(limit=None, path=None):
    """Read records back, oldest first. Skips any corrupt line."""
    target = path or METRICS_PATH
    rows = []
    for candidate in (target + ".1", target):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return rows[-limit:] if limit else rows


def _pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return s[idx]


def summarize(rows):
    """Aggregate records into the numbers worth acting on."""
    total = len(rows)
    counts = {}
    for r in rows:
        counts[r.get("outcome", "?")] = counts.get(r.get("outcome", "?"), 0) + 1
    lat = [r["transcribe_ms"] for r in rows if isinstance(r.get("transcribe_ms"), (int, float))]
    holds = [r["hold_s"] for r in rows if isinstance(r.get("hold_s"), (int, float))]
    peaks = [r["peak"] for r in rows if isinstance(r.get("peak"), (int, float))]
    overflow_runs = sum(1 for r in rows if r.get("overflows"))
    retried = sum(1 for r in rows if r.get("retried"))
    rescued = sum(1 for r in rows if r.get("keyup_lost"))
    bad = sum(counts.get(k, 0) for k in _BAD)
    return {
        "total": total,
        "counts": counts,
        "success_rate": (total - bad) / total if total else 0.0,
        "transcribe_ms_p50": _pct(lat, 0.50),
        "transcribe_ms_p95": _pct(lat, 0.95),
        "hold_s_p50": _pct(holds, 0.50),
        "peak_p05": _pct(peaks, 0.05),
        "overflow_dictations": overflow_runs,
        "vad_retries": retried,
        "lost_keyups_rescued": rescued,
        "models": sorted({r.get("model") for r in rows if r.get("model")}),
        "devices": sorted({r.get("device") for r in rows if r.get("device")}),
    }


def format_report(rows):
    """Human-readable summary for `--report`. ASCII only (cp1252 consoles)."""
    if not rows:
        return ("No dictation metrics recorded yet.\n"
                f"(Expected at {METRICS_PATH} once you start dictating.)")
    s = summarize(rows)
    out = []
    out.append("Voice Assistant - dictation report")
    out.append("=" * 52)
    out.append(f"  dictations recorded : {s['total']}")
    out.append(f"  clean success rate  : {s['success_rate'] * 100:.1f}%")
    out.append(f"  transcribe latency  : {s['transcribe_ms_p50']:.0f} ms median, "
               f"{s['transcribe_ms_p95']:.0f} ms p95")
    out.append(f"  typical hold        : {s['hold_s_p50']:.2f} s")
    out.append(f"  quietest 5% peak    : {s['peak_p05']:.4f}")
    out.append(f"  model / device      : {', '.join(s['models']) or '?'} on "
               f"{', '.join(s['devices']) or '?'}")
    out.append("")
    out.append("  outcomes:")
    for k in sorted(s["counts"], key=lambda k: -s["counts"][k]):
        flag = "  <-- worth looking at" if k in _BAD and s["counts"][k] else ""
        out.append(f"    {k:<18} {s['counts'][k]:>5}{flag}")
    out.append("")
    out.append(f"  gappy audio (overflow) : {s['overflow_dictations']}")
    out.append(f"  VAD no-speech retries  : {s['vad_retries']}")
    out.append(f"  lost keyups rescued    : {s['lost_keyups_rescued']}")
    if s["counts"].get(OUTCOME_DROPPED_QUIET):
        out.append("\n  Dropped-quiet clips mean the mic level is too low - check the")
        out.append("  Yeti's gain knob and that the right device is set in Settings.")
    if s["counts"].get(OUTCOME_PASTE_FAILED):
        out.append("\n  Paste failures leave the text on the clipboard; usually a window")
        out.append("  that refused focus (elevated/admin apps do this).")
    if s["lost_keyups_rescued"]:
        out.append("\n  Lost keyups were caught by the watchdog - dictation still worked,")
        out.append("  but it means Windows dropped the keyboard hook under load.")
    return "\n".join(out)
