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
from main import MainWindow, collapse_repeated_phrases, sanitize_for_paste


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

    def test_case_insensitive_keeps_first_casing(self):
        assert collapse_repeated_phrases("Hello hello world") == "Hello world"

    def test_no_repeats_untouched(self):
        s = "this sentence has no repeated phrases at all"
        assert collapse_repeated_phrases(s) == s

    def test_punctuation_defeats_match_CURRENT_BEHAVIOR(self):
        # KNOWN GAP (M4, fixed in Phase 2): differing punctuation blocks the
        # match because tokens are compared with punctuation attached.
        s = "I went home, I went home."
        assert collapse_repeated_phrases(s) == s  # NOT collapsed today

    def test_intentional_repeat_eaten_CURRENT_BEHAVIOR(self):
        # KNOWN GAP (M5, fixed in Phase 2): deliberate repeats are collapsed.
        assert collapse_repeated_phrases("no no no") == "no"

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
# _light_cleanup (called unbound; it does not use self)
# ---------------------------------------------------------------------------
def light_cleanup(text):
    return MainWindow._light_cleanup(None, text)


class TestLightCleanup:
    def test_fillers_removed(self):
        assert light_cleanup("um I think uh this works") == "I think this works."

    def test_forces_capital_and_period_CURRENT_BEHAVIOR(self):
        # KNOWN GAP (M3, made optional later): every dictation is capitalized
        # and gets a trailing period.
        assert light_cleanup("lowercase text") == "Lowercase text."

    def test_keeps_existing_terminal_punctuation(self):
        assert light_cleanup("is this a question?") == "Is this a question?"

    def test_space_before_punct_removed(self):
        assert light_cleanup("hello , world .") == "Hello, world."

    def test_bracket_passthrough(self):
        assert light_cleanup("[No speech detected]") == "[No speech detected]"

    def test_short_stutter_collapsed(self):
        assert light_cleanup("the the file") == "The file."


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

    def test_sanitize_settings_dedupe_hole_CURRENT_BEHAVIOR(self):
        # NEW BUG found by this suite (fixed in Phase 2): when a duplicate is
        # reset to its DEFAULT but that default IS the colliding value, the
        # duplicate survives. Here record takes screen_read's default combo;
        # screen_read is "reset"… right back to the same combo. Result: two
        # actions bound to one hotkey.
        data = dict(DEFAULTS)
        data["hotkey_record"] = "ctrl+shift+s"       # steals screen_read's default
        data["hotkey_screen_read"] = "ctrl+shift+s"
        cleaned = sanitize_settings(data)
        assert cleaned["hotkey_record"] == cleaned["hotkey_screen_read"]  # the hole

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
