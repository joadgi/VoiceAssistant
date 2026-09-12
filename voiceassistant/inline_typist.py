"""Inline typing — the draft lands in the text box while you speak (Stage 2).

Stage 1 showed the rolling draft on the app's own pill. This puts it in the
window you are dictating into: as words settle, they are typed there; when the
final, more accurate transcription arrives, the difference between what was
typed and what the final pass produced is corrected in place.

This module is the POLICY and the ARITHMETIC, with no Windows calls and no
threads, because both are the parts that have to be provably right:

  * `plan_edit` decides how many characters to delete and what to type so the
    window ends up holding exactly the target text.
  * `block_reason` decides whether inline typing is allowed to run at all.

The injection itself lives in `winapi.send_text`/`send_backspaces`, and the
state machine that owns "what have we typed so far" lives on the paste worker
(`paste.py`), because that accounting must be strictly serialized with the
injection it describes.

THE SAFETY CONTRACT — every line of it was chosen against a specific way this
feature can damage the user's document, and none of it is decorative:

1. **We only ever delete our own text.** `plan_edit` computes deletions from
   the app's own record of what it typed, never from a guess about the
   window's contents. If that record is ever UNCERTAIN — a SendInput batch
   Windows accepted only in part — corrections stop permanently for that
   dictation. Backspacing past our own text eats the user's work, and no
   feature is worth that.
2. **A correction is bounded.** Past `MAX_STREAM_BACKSPACES` mid-sentence the
   plan is refused rather than issuing a long delete burst into a live
   document. The final reconciliation is allowed more, because it happens once
   and it is what makes the pasted result accurate.
3. **Never run while the dictate hotkey holds a modifier.** With `ctrl+shift+r`
   held, Ctrl is physically down for the whole utterance, so every injected
   character arrives as a SHORTCUT — "select all", "new tab", "bold". The
   binding must be a plain key (Josh's `caps lock` qualifies).
4. **Never type into a console.** A terminal is the one window where stray
   text is not merely wrong, and it is the same boundary read-aloud already
   observes for Ctrl+C.
5. **Never type into our own window**, which is the panel fallback's job.

Stage 1 remains the default. This is opt-in (`inline_typing`), and when it is
refused the app falls back to exactly the behaviour that shipped before it.
"""

import re

# Mid-utterance corrections are bounded: a long delete burst into a live
# document is not something to do on a guess about a word that is still moving.
MAX_STREAM_BACKSPACES = 48
# The final reconciliation happens once, with the accurate text, and is what
# makes the result trustworthy — so it may correct a whole sentence.
MAX_FINAL_BACKSPACES = 400

# Modifier names as `config.validate_hotkey` spells them.
_MODIFIERS = {"ctrl", "shift", "alt", "windows", "cmd", "meta"}


def hotkey_holds_modifier(hotkey):
    """True when holding this binding means a modifier is down while speaking."""
    if not hotkey:
        return False
    return any(part.strip() in _MODIFIERS for part in str(hotkey).split("+"))


def block_reason(enabled, hotkey, hwnd, is_own_window, is_console):
    """Why inline typing must not run, as a user-facing sentence (None = run).

    Order matters only for which message the user sees first; each condition
    is independently disqualifying.
    """
    if not enabled:
        return "Inline typing is off"
    if not hwnd:
        return "No target window was captured"
    if is_own_window:
        return "Dictating into the Voice Assistant window"
    if is_console:
        return "The target is a terminal — typing into it is not safe"
    if hotkey_holds_modifier(hotkey):
        return (f"The dictate hotkey ({hotkey}) holds a modifier while you speak, "
                "so typed characters would arrive as shortcuts. Bind a plain key "
                "like Caps Lock to use inline typing.")
    return None


def polish_stream_text(text):
    """Make streamed text match how the FINAL text will start.

    `finish_transcript` capitalizes the first character of the final
    transcription. If the stream typed a lower-case first letter, the two
    strings would differ at character zero and the reconciliation would
    retype the entire utterance to fix one letter. Matching it up front makes
    the common case a zero-backspace append.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def common_prefix_len(a, b):
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def plan_edit(typed, target, max_backspaces):
    """How to turn `typed` into `target` in the window: (backspaces, to_type).

    Returns None when the edit would exceed `max_backspaces` — the caller then
    stops inline typing rather than issuing a long delete burst.

    Both strings are what THIS APP believes it put in the window; the user's
    own surrounding text is never part of either, so the deletions computed
    here can only ever remove the app's own characters.
    """
    if typed == target:
        return 0, ""
    keep = common_prefix_len(typed, target)
    backspaces = len(typed) - keep
    if backspaces > max_backspaces:
        return None
    return backspaces, target[keep:]


def stream_target(stable_text):
    """The text inline typing should have in the window for a given draft.

    Only the STABLE half of the draft is typed. The moving tail is deliberately
    left on the pill: typing a word that is still being revised means deleting
    it again a moment later, which reads as flickering in the user's document.
    A word arrives in the window once two consecutive decodes agree on it.
    """
    return polish_stream_text(stable_text or "")
