"""Configuration and settings for Voice Assistant."""

import json
import os

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "whisper_model": "base",
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
    "dark_mode": True,
    "font_size": 13,
    "sample_rate": 16000,
}


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

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key, default=None):
        return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value
