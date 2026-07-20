"""Configuration and settings for Voice Assistant.

Settings live in settings.json next to the app (per-user, gitignored).
Writes are ATOMIC (temp file + os.replace) so a crash mid-save can never
truncate the file, and a corrupt file is backed up — never silently discarded.

HOTKEY CONTRACT (user directive — never hardcode): every action's hotkey
(Dictate / Read / OCR) is a per-user setting, editable in the UI and stored
here; DEFAULTS are factory defaults only. Modifier-only combos (ctrl+alt) are
a supported deliberate choice. Only bindings that would break basic function
are rejected (bare typing keys, a single bare modifier, escape).
"""

import json
import os
import shutil
import tempfile

from . import applog

# The app root is the parent of this package — settings.json stays where it
# has always lived, next to run.bat.
CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "whisper_model": "medium",
    "whisper_language": "en",
    "whisper_device": "cuda",
    "whisper_compute_type": "float16",

    "tts_rate": 175,
    "tts_speed": 1.0,
    "tts_voice": "en-US-AndrewNeural",
    "tts_volume": 1.0,

    "ocr_languages": ["en"],
    "ocr_gpu": True,
    # "auto" = Windows-native OCR (fast, zero heavy deps), EasyOCR fallback.
    "ocr_backend": "auto",
    "screen_capture_width": 600,
    "screen_capture_height": 300,

    "hotkey_record": "ctrl+shift+r",
    "hotkey_screen_read": "ctrl+shift+s",
    "hotkey_read_aloud": "ctrl+shift+t",

    "audio_device": -1,
    "dictation_mode": True,
    "auto_paste": True,

    "always_on_top": False,
    # Release-safe default: never silently register OS auto-start — the user
    # opts in via Settings. (Existing users' saved True persists untouched.)
    "start_with_windows": False,
    "start_minimized": True,
    "dark_mode": True,
    "font_size": 13,
    "sample_rate": 16000,
    "min_record_seconds": 0.2,
    "min_record_peak": 0.008,
    "max_record_seconds": 120,  # safety cap: auto-stop a forgotten/stuck hold

    "light_cleanup": True,
    "debug_logging": False,
}


MODIFIER_KEYS = {"ctrl", "shift", "alt", "windows", "cmd", "meta"}
HOTKEY_KEYS = ("hotkey_record", "hotkey_screen_read", "hotkey_read_aloud")

# Bare keys we refuse to bind alone — you type these constantly, so a single-key
# hotkey on one of them would fire during normal typing.
TYPING_KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789") | {
    "space", "tab", "enter", "backspace",
    "-", "=", "[", "]", ";", "'", ",", ".", "/", "\\", "`",
}


def normalize_hotkey(value):
    """Return a canonical keyboard-library hotkey string."""
    parts = [p.strip().lower() for p in str(value or "").split("+") if p.strip()]
    aliases = {
        "control": "ctrl",
        "win": "windows",
        "command": "cmd",
        "option": "alt",
    }
    return "+".join(aliases.get(part, part) for part in parts)


def validate_hotkey(value):
    """Accept a modifier combo (ctrl+shift+f9) OR a safe standalone key (f9, caps lock).

    A single key is allowed as long as it isn't a key you type all day (letters,
    digits, space, punctuation) or a bare generic modifier.
    """
    combo = normalize_hotkey(value)
    if not combo or combo == "escape":
        return False
    parts = [p for p in combo.split("+") if p]
    if not parts:
        return False
    if len(parts) == 1:
        key = parts[0]
        if key in TYPING_KEYS or key in MODIFIER_KEYS:
            return False
        # e.g. f1-f12, caps lock, right ctrl, insert, pause — fine alone,
        # but it must be a key the keyboard library can actually bind
        # ("banana" used to validate and then silently fail to register).
        try:
            import keyboard as _kb

            _kb.key_to_scan_codes(key)
            return True
        except (ValueError, ImportError):
            return False
        except Exception:
            return True  # unexpected lookup failure: don't block the user
    # Multi-key combo: valid if it has a real key (ctrl+shift+f9) OR is a combo
    # of two or more modifiers held together (e.g. ctrl+alt).
    if any(part not in MODIFIER_KEYS for part in parts):
        return True
    return all(part in MODIFIER_KEYS for part in parts) and len(parts) >= 2


def sanitize_settings(data):
    """Clean persisted settings so one bad hotkey cannot break startup.

    When a duplicate/invalid hotkey is reset, the replacement must itself be
    unique — resetting to a default that IS the colliding value used to leave
    two actions bound to one combo. A reserve pool guarantees a free combo.
    """
    fallback_pool = [DEFAULTS[k] for k in HOTKEY_KEYS] + [
        "ctrl+shift+f9", "ctrl+shift+f10", "ctrl+shift+f11",
    ]
    cleaned = dict(data)
    seen = set()
    for key in HOTKEY_KEYS:
        value = normalize_hotkey(cleaned.get(key, DEFAULTS[key]))
        if not validate_hotkey(value) or value in seen:
            value = DEFAULTS[key]
            if value in seen:
                value = next(v for v in fallback_pool if v not in seen)
        cleaned[key] = value
        seen.add(value)
    return cleaned


class Config:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self._dirty = False
        self.load_error = None  # set when a corrupt file was backed up
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    saved = json.load(f)
                self._data.update(saved)
            except (json.JSONDecodeError, IOError) as e:
                # Never silently discard the user's settings — back the file
                # up so it can be inspected/recovered, and surface the event.
                backup = CONFIG_FILE + ".corrupt.bak"
                try:
                    shutil.copyfile(CONFIG_FILE, backup)
                    self.load_error = (
                        f"settings.json was unreadable ({e.__class__.__name__}); "
                        f"backed up to {os.path.basename(backup)} and reset to defaults"
                    )
                except OSError:
                    self.load_error = "settings.json was unreadable; reset to defaults"
                applog.error(self.load_error)
        sanitized = sanitize_settings(self._data)
        if sanitized != self._data:
            self._data = sanitized
            self.save()

    def save(self):
        """Atomic write: temp file in the same directory, then os.replace.

        A crash or power loss mid-write can no longer truncate settings.json
        (which previously reset every preference to defaults on next launch).
        """
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=CONFIG_DIR, prefix="settings.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp_path, CONFIG_FILE)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            self._dirty = False
        except OSError as e:
            applog.error(f"settings save failed: {e}")

    def get(self, key, default=None):
        return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key, value, defer_save=False):
        """Set a value. defer_save=True marks dirty without writing — used by
        high-frequency callers (the speed slider fired a disk write per tick);
        call flush() when the interaction ends."""
        if self._data.get(key) == value:
            return
        self._data[key] = value
        if defer_save:
            self._dirty = True
        else:
            self.save()

    def flush(self):
        """Write deferred changes, if any."""
        if self._dirty:
            self.save()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self.set(key, value)
