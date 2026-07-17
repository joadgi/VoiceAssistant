"""Pure text logic for dictation cleanup — no Qt, no Win32, no I/O.

Everything here is unit-tested to the character (tests/test_characterization.py)
and exercised end-to-end by the golden-audio corpus (tests/test_corpus_gate.py).
"""

import re

# ---------------------------------------------------------------------------
# Repeat collapse
# ---------------------------------------------------------------------------
# Function words that are never grammatically doubled in English — a 2x run of
# one of these is a stutter/artifact. Deliberately EXCLUDES words that can
# legitimately double across grammar ("that that", "in in", "had had", "it. It")
# — for dictation trust, corrupting correct output is worse than missing an
# artifact. Content words ("no no no", "very very") can be deliberate, so
# single content words only collapse at longer runs (4+).
_STUTTER_DOUBLE_WORDS = {
    "the", "a", "an", "of", "to", "and", "but", "or", "with",
}

_SENTENCE_END = (".", "!", "?")

_TOKEN_TRIM = ".,!?;:\"'…—-"


def _norm_token(word):
    """Comparison form of a token: case-folded, surrounding punctuation trimmed.

    'Home,' and 'home.' must compare equal — punctuation-attached comparison is
    what let 'I went home, I went home.' slip through the old collapse.
    """
    return word.strip(_TOKEN_TRIM).lower()


def collapse_repeated_phrases(text):
    """Collapse consecutive repeated phrases (1–8 words) — Whisper artifacts.

    - Multi-word phrase repeats ('send the file send the file') collapse at 2+
      reps: nobody dictates the same phrase back-to-back on purpose.
    - Single-word repeats collapse at 2+ reps only for function words (see
      _STUTTER_DOUBLE_WORDS); content words need 4+ reps so intentional
      repeats like 'no no no' survive (confirmed: Whisper transcribes them
      correctly — the OLD collapse was what ate them).
    - A sentence boundary inside a run ('…like it. It works…') is grammar,
      not a stutter — the long-run threshold applies.
    - Comparison is case- and punctuation-insensitive; the LAST copy's tokens
      are kept (terminal punctuation survives) with the FIRST copy's leading
      capitalization merged in.
    """
    words = text.split()
    out = []
    i = 0
    n = len(words)
    while i < n:
        matched = False
        max_plen = min(8, (n - i) // 2)
        for plen in range(max_plen, 0, -1):
            phrase = [_norm_token(w) for w in words[i:i + plen]]
            if not any(phrase):
                continue
            # A "phrase" of one repeated word ('go go', 'wait - wait') is a
            # single-word run in disguise — defer to the plen==1 policy and
            # its thresholds instead of the permissive phrase rule.
            if plen > 1 and len({t for t in phrase if t}) == 1:
                continue
            reps = 1
            j = i + plen
            while [_norm_token(w) for w in words[j:j + plen]] == phrase:
                reps += 1
                j += plen
            if reps < 2:
                continue
            if plen == 1:
                run = words[i:j]
                crosses_sentence = any(
                    w.endswith(_SENTENCE_END) for w in run[:-1]
                )
                if crosses_sentence or phrase[0] not in _STUTTER_DOUBLE_WORDS:
                    needed = 4
                else:
                    needed = 2
                if reps < needed:
                    continue
            # Keep one copy — the LAST, so terminal punctuation survives; but
            # merge the FIRST copy's leading capitalization so a collapse at a
            # sentence start doesn't decapitalize it.
            kept = list(words[j - plen:j])
            first = words[i]
            if first[:1].isupper() and kept[0][:1].islower():
                kept[0] = kept[0][0].upper() + kept[0][1:]
            out.extend(kept)
            i = j
            matched = True
            break
        if not matched:
            out.append(words[i])
            i += 1
    return " ".join(out)


# ---------------------------------------------------------------------------
# Cleanup chain
# ---------------------------------------------------------------------------
def strip_fillers(text):
    """Remove filler words and hyphen-joined prefix stutters ('th-the' → 'the').

    The stutter pattern requires a TRUE prefix (the fragment is shorter than
    the word it stutters into), so real hyphenated words like 'no-no' and
    'win-win' are untouched. Space-separated doubles ('the the') are NOT
    handled here — that policy lives in collapse_repeated_phrases so
    intentional repeats are judged in exactly one place.
    """
    cleaned = re.sub(r"\b(um+|uh+|erm|ah+)\b[, ]*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(\w{1,3})-(\1\w+)\b", r"\2", cleaned, flags=re.IGNORECASE)
    return cleaned


def finish_transcript(text):
    """Final tidy: spacing, punctuation spacing, leading capital, terminal period."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
        if cleaned[-1] not in ".!?":
            cleaned += "."
    return cleaned


def clean_transcript(text, light=True):
    """The dictation cleanup chain, in the correct order.

    Fillers are stripped BEFORE the repeat collapse — the old order let
    'the um the file' survive as 'the the file' because the collapse ran
    first and the filler removal then created a new adjacent repeat.
    Repeat collapse always runs; fillers/finishing follow the light_cleanup
    setting.
    """
    if not text or text.startswith("["):
        return text
    if light:
        text = strip_fillers(text)
    text = collapse_repeated_phrases(text)
    if light:
        text = finish_transcript(text)
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Hallucination backstop
# ---------------------------------------------------------------------------
# Classic Whisper silence-hallucinations (YouTube-outro artifacts). Only ever
# consulted for SHORT clips whose VAD pass found nothing (the retry produced
# the text). DELIBERATELY EXCLUDES phrases people actually dictate standalone
# ("thank you", "bye", "cheers", "see you") — silently eating a real quiet
# utterance is the historical regression this app must never repeat. The
# per-segment no_speech_prob filter is the primary defense; this list is a
# narrow backstop for phrases essentially never dictated alone in under 1.2s.
HALLUCINATION_DENYLIST = {
    "you", "thanks for watching", "thank you for watching",
    "thanks for listening", "subscribe", "the end",
}


def is_probable_hallucination(result, cleaned_text):
    """True when a transcription is almost certainly invented from noise.

    Criteria: the VAD pass judged the clip silent (retry produced the text),
    the clip is sub-1.2s, and the cleaned text is a known artifact phrase.
    `result` needs `.retried` and `.duration_s` (see TranscriptionResult).
    """
    if not result.retried or result.duration_s >= 1.2:
        return False
    norm = re.sub(r"[^\w\s]", "", cleaned_text).strip().lower()
    return norm in HALLUCINATION_DENYLIST


# ---------------------------------------------------------------------------
# Paste hygiene
# ---------------------------------------------------------------------------
def sanitize_for_paste(text):
    """Make text safe to paste: drop control chars and flatten newlines/tabs.

    Stray newlines/control chars are what make single-line and chat inputs emit
    the Windows 'ding' on paste.
    """
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
