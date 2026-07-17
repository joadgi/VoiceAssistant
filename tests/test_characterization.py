"""Characterization tests — lock in CURRENT behavior of the pure logic.

These encode what the code does TODAY (including behaviors Phase 2 will change
deliberately — those are marked). If a refactor changes any of this
unintentionally, these tests catch it.

Run: venv/Scripts/python.exe -m pytest tests/test_characterization.py -q
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
from config import DEFAULTS, normalize_hotkey, validate_hotkey, sanitize_settings
from main import (
    clean_transcript,
    collapse_repeated_phrases,
    is_probable_hallucination,
    sanitize_for_paste,
    strip_fillers,
)
from voice_engine import TranscriptionResult


# ---------------------------------------------------------------------------
# collapse_repeated_phrases
# ---------------------------------------------------------------------------
class TestCollapseRepeats:
    def test_word_double(self):
        assert collapse_repeated_phrases("the the cat") == "the cat"

    def test_word_triple(self):
        assert collapse_repeated_phrases("the the the cat") == "the cat"

    def test_phrase_double(self):
        assert collapse_repeated_phrases("send the file send the file") == "send the file"

    def test_sentence_double_identical_punct(self):
        # Both copies end with '.', so tokens match -> collapsed.
        assert (
            collapse_repeated_phrases("Send a file. Send a file.") == "Send a file."
        )

    def test_content_word_double_preserved(self):
        # Phase 2 policy: content words CAN be deliberately repeated —
        # a 2x run of a non-function word is kept.
        assert collapse_repeated_phrases("Hello hello world") == "Hello hello world"

    def test_content_word_long_run_collapsed(self):
        # …but a 4+ run of the same word is a Whisper artifact.
        assert collapse_repeated_phrases("go go go go go now") == "go now"

    def test_no_repeats_untouched(self):
        s = "this sentence has no repeated phrases at all"
        assert collapse_repeated_phrases(s) == s

    def test_punctuation_insensitive_match_FIXED_M4(self):
        # Phase 2 fix: 'home,' and 'home.' compare equal; last copy's
        # punctuation is kept.
        assert collapse_repeated_phrases("I went home, I went home.") == "I went home."

    def test_intentional_repeat_preserved_FIXED_M5(self):
        # Phase 2 fix: deliberate short repeats survive (Whisper transcribes
        # them correctly; the old collapse was what ate them).
        assert collapse_repeated_phrases("no no no") == "no no no"
        assert collapse_repeated_phrases("very very good") == "very very good"

    def test_sentence_boundary_is_grammar_not_stutter(self):
        # Review finding F1: '…it. It…' across a sentence boundary is normal
        # English, not a stutter — collapsing it deleted words and periods.
        for s in (
            "I like it. It works.",
            "Look at this. This is the one.",
            "There it was. Was it good?",
        ):
            assert collapse_repeated_phrases(s) == s, s

    def test_grammatical_doubles_preserved(self):
        # Review finding F5: 'that that' / 'in in' are legitimate English.
        assert collapse_repeated_phrases("I know that that is true") == "I know that that is true"
        assert collapse_repeated_phrases("He walked in in a hurry") == "He walked in in a hurry"

    def test_capitalization_merged_on_collapse(self):
        # Review finding F6: keep-last must not decapitalize a sentence start.
        assert (
            collapse_repeated_phrases("The the file is ready.")
            == "The file is ready."
        )

    def test_mixed_token_run_not_mangled(self):
        # Review finding F11: 'wait - wait - wait' must not half-collapse.
        s = "wait - wait - wait"
        assert collapse_repeated_phrases(s) == s

    def test_empty(self):
        assert collapse_repeated_phrases("") == ""


# ---------------------------------------------------------------------------
# sanitize_for_paste
# ---------------------------------------------------------------------------
class TestSanitizeForPaste:
    def test_control_chars_stripped(self):
        assert sanitize_for_paste("a\x00b\x07c\x1bd") == "abcd"

    def test_newlines_tabs_flattened(self):
        assert sanitize_for_paste("line one\nline two\ttabbed") == "line one line two tabbed"

    def test_whitespace_collapsed_and_trimmed(self):
        assert sanitize_for_paste("  a   b  ") == "a b"

    def test_pure_control_becomes_empty(self):
        assert sanitize_for_paste("\x00\x01\n\t ") == ""


# ---------------------------------------------------------------------------
# clean_transcript — the full cleanup chain (fillers -> collapse -> finish)
# ---------------------------------------------------------------------------
class TestCleanTranscript:
    def test_fillers_removed(self):
        assert clean_transcript("um I think uh this works") == "I think this works."

    def test_forces_capital_and_period_CURRENT_BEHAVIOR(self):
        # KNOWN GAP (M3, made optional in Phase 4): every dictation is
        # capitalized and gets a trailing period.
        assert clean_transcript("lowercase text") == "Lowercase text."

    def test_keeps_existing_terminal_punctuation(self):
        assert clean_transcript("is this a question?") == "Is this a question?"

    def test_space_before_punct_removed(self):
        assert clean_transcript("hello , world .") == "Hello, world."

    def test_bracket_passthrough(self):
        assert clean_transcript("[No speech detected]") == "[No speech detected]"

    def test_function_word_stutter_collapsed(self):
        assert clean_transcript("the the file") == "The file."

    def test_filler_ordering_FIXED_M4(self):
        # Phase 2 fix: fillers are stripped BEFORE the repeat collapse, so a
        # filler wedged inside a stutter no longer shields the repeat.
        assert clean_transcript("the um the file") == "The file."

    def test_intentional_repeat_survives_full_chain_FIXED_M5(self):
        assert clean_transcript("no no no") == "No no no."

    def test_hyphen_prefix_stutter_stripped(self):
        # True prefix stutters collapse; real hyphenated words survive (F10).
        assert strip_fillers("th-the file") == "the file"
        assert strip_fillers("b-because") == "because"
        assert strip_fillers("no-no problem") == "no-no problem"
        assert strip_fillers("win-win deal") == "win-win deal"

    def test_light_false_still_collapses_repeats(self):
        out = clean_transcript("send the file send the file", light=False)
        assert out == "send the file"


# ---------------------------------------------------------------------------
# Hallucination backstop (denylist applies only to short VAD-empty retries)
# ---------------------------------------------------------------------------
def _result(text, duration_s, retried):
    return TranscriptionResult(
        text=text, job_id=1, duration_s=duration_s, retried=retried
    )


class TestHallucinationBackstop:
    def test_short_retry_artifact_suppressed(self):
        assert is_probable_hallucination(_result("you", 0.6, True), "You.")
        assert is_probable_hallucination(
            _result("Thanks for watching!", 0.9, True), "Thanks for watching!"
        )

    def test_real_word_not_suppressed(self):
        assert not is_probable_hallucination(_result("yes", 0.6, True), "Yes.")

    def test_real_phrases_never_suppressed(self):
        # Review finding F2: people genuinely dictate these — silently eating
        # them is the historical "quiet speech lost" regression. They must
        # NOT be in the denylist.
        for phrase in ("Thank you.", "Bye.", "See you.", "Cheers.", "Goodbye."):
            assert not is_probable_hallucination(
                _result(phrase, 0.9, True), phrase
            ), phrase

    def test_vad_passed_text_never_suppressed(self):
        # If the VAD pass produced it, it is speech — even "you".
        assert not is_probable_hallucination(_result("you", 0.6, False), "You.")

    def test_long_clip_never_suppressed(self):
        assert not is_probable_hallucination(_result("you", 2.5, True), "You.")


# ---------------------------------------------------------------------------
# Hotkey normalization / validation — the DYNAMIC hotkey contract:
# everything is user-configurable; only bindings that would break basic
# function are rejected.
# ---------------------------------------------------------------------------
class TestHotkeys:
    def test_normalize_aliases_and_case(self):
        assert normalize_hotkey("Control+Shift+R") == "ctrl+shift+r"
        assert normalize_hotkey("Win+F9") == "windows+f9"
        assert normalize_hotkey(" option + a ") == "alt+a"

    def test_single_function_key_valid(self):
        assert validate_hotkey("f9")
        assert validate_hotkey("caps lock")

    def test_bare_typing_keys_invalid(self):
        # Would fire on every normal keystroke.
        for k in ("a", "7", "space", ".", "enter"):
            assert not validate_hotkey(k), k

    def test_single_bare_modifier_invalid(self):
        for k in ("ctrl", "shift", "alt", "windows"):
            assert not validate_hotkey(k), k

    def test_escape_invalid(self):
        assert not validate_hotkey("escape")

    def test_normal_combo_valid(self):
        assert validate_hotkey("ctrl+shift+r")
        assert validate_hotkey("alt+f4")  # allowed: user's choice

    def test_modifier_only_combo_valid_USER_DIRECTIVE(self):
        # Deliberate product decision: 2+ modifiers are a supported
        # hold-to-talk choice (e.g. ctrl+alt). Docs carry the AltGr caveat.
        assert validate_hotkey("ctrl+alt")
        assert validate_hotkey("ctrl+shift")
        assert validate_hotkey("ctrl+alt+shift")

    def test_empty_invalid(self):
        assert not validate_hotkey("")
        assert not validate_hotkey(None)

    def test_unknown_single_token_accepted_CURRENT_BEHAVIOR(self):
        # KNOWN GAP (L11, tightened in Phase 4): validate_hotkey accepts ANY
        # unknown single token ("banana", "f13", even a whole garbage string
        # that contains no '+') because it is neither a typing key nor a
        # modifier. Registration fails gracefully at runtime (caught per-key,
        # status message shown) so this cannot crash startup — but sanitize
        # does NOT reset it.
        assert validate_hotkey("banana")
        assert validate_hotkey("not a real key combo +++")  # -> one token

    def test_sanitize_settings_resets_rule_invalid_hotkey(self):
        data = dict(DEFAULTS)
        data["hotkey_record"] = "q"  # bare typing key — rejected by rule
        cleaned = sanitize_settings(data)
        assert cleaned["hotkey_record"] == DEFAULTS["hotkey_record"]

    def test_sanitize_settings_dedupe_hole_FIXED(self):
        # Phase 2 fix: resetting a collision now draws from a reserve pool, so
        # the replacement can never itself be the colliding combo.
        data = dict(DEFAULTS)
        data["hotkey_record"] = "ctrl+shift+s"       # steals screen_read's default
        data["hotkey_screen_read"] = "ctrl+shift+s"
        cleaned = sanitize_settings(data)
        vals = [cleaned[k] for k in ("hotkey_record", "hotkey_screen_read", "hotkey_read_aloud")]
        assert len(set(vals)) == 3, f"hotkeys not unique after sanitize: {vals}"

    def test_sanitize_settings_dedupes_when_default_differs(self):
        # The dedupe DOES work when the reset target isn't itself the dupe.
        data = dict(DEFAULTS)
        data["hotkey_screen_read"] = "ctrl+shift+r"  # steals record's default
        cleaned = sanitize_settings(data)
        # record keeps its default; screen_read collides and resets to ITS
        # default (ctrl+shift+s), which is free -> unique again.
        vals = [cleaned[k] for k in ("hotkey_record", "hotkey_screen_read", "hotkey_read_aloud")]
        assert len(set(vals)) == 3, f"hotkeys not unique after sanitize: {vals}"


# ---------------------------------------------------------------------------
# Config persistence — corrupt/missing/partial settings.json
# ---------------------------------------------------------------------------
class TestConfig:
    def _cfg(self, tmp_path, monkeypatch, contents=None):
        path = str(tmp_path / "settings.json")
        if contents is not None:
            with open(path, "w") as f:
                f.write(contents)
        monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
        return config_mod.Config(), path

    def test_missing_file_uses_defaults(self, tmp_path, monkeypatch):
        cfg, _ = self._cfg(tmp_path, monkeypatch)
        assert cfg["whisper_model"] == DEFAULTS["whisper_model"]

    def test_corrupt_file_falls_back_to_defaults_CURRENT_BEHAVIOR(self, tmp_path, monkeypatch):
        # KNOWN GAP (M8, Phase 3): silently resets everything, no backup.
        cfg, _ = self._cfg(tmp_path, monkeypatch, contents="{ this is not json")
        assert cfg["hotkey_record"] == DEFAULTS["hotkey_record"]

    def test_partial_file_merges_over_defaults(self, tmp_path, monkeypatch):
        cfg, _ = self._cfg(tmp_path, monkeypatch, contents=json.dumps({"tts_speed": 2.2}))
        assert cfg["tts_speed"] == 2.2
        assert cfg["whisper_model"] == DEFAULTS["whisper_model"]

    def test_user_hotkey_choice_survives_roundtrip(self, tmp_path, monkeypatch):
        # The dynamic-hotkey contract: a user's modifier-only choice persists.
        cfg, path = self._cfg(
            tmp_path, monkeypatch, contents=json.dumps({"hotkey_record": "ctrl+alt"})
        )
        assert cfg["hotkey_record"] == "ctrl+alt"
        cfg.set("hotkey_record", "ctrl+windows")
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["hotkey_record"] == "ctrl+windows"

    def test_invalid_saved_hotkey_reset_on_load(self, tmp_path, monkeypatch):
        cfg, _ = self._cfg(
            tmp_path, monkeypatch, contents=json.dumps({"hotkey_record": "q"})
        )
        assert cfg["hotkey_record"] == DEFAULTS["hotkey_record"]
