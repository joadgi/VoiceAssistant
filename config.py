"""Configuration and settings for Voice Assistant."""

import json
import os

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
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
    "screen_capture_width": 600,
    "screen_capture_height": 300,

    "hotkey_record": "ctrl+shift+r",
    "hotkey_screen_read": "ctrl+shift+s",
    "hotkey_read_aloud": "ctrl+shift+t",
    "hotkey_stop": "escape",

    "audio_device": -1,
    "dictation_mode": True,
    "auto_paste": True,

    "always_on_top": False,
    "start_with_windows": True,
    "start_minimized": True,
    "dark_mode": True,
    "font_size": 13,
    "sample_rate": 16000,
    "min_record_seconds": 0.2,
    "min_record_peak": 0.008,
    "light_cleanup": True,
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
        # e.g. f1-f12, caps lock, right ctrl, insert, pause — fine alone.
        return key not in TYPING_KEYS and key not in MODIFIER_KEYS
    # Multi-key combo: must include at least one non-modifier.
    return any(part not in MODIFIER_KEYS for part in parts)


def sanitize_settings(data):
    """Clean persisted settings so one bad hotkey cannot break startup."""
    cleaned = dict(data)
    seen = set()
    for key in HOTKEY_KEYS:
        value = normalize_hotkey(cleaned.get(key, DEFAULTS[key]))
        if not validate_hotkey(value) or value in seen:
            value = DEFAULTS[key]
        cleaned[key] = value
        seen.add(value)
    return cleaned


class Config:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    saved = json.load(f)
                self._data.update(saved)
            except (json.JSONDecodeError, IOError):
                pass
        sanitized = sanitize_settings(self._data)
        if sanitized != self._data:
            self._data = sanitized
            self.save()

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key, default=None):
        return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key, value):
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self.save()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self.set(key, value)
