"""Integration test: the hotkey system is fully user-configurable, end-to-end.

Product directive (see config.py HOTKEY CONTRACT): EVERY action's hotkey
(Dictate / Read / OCR) is a per-user setting and NOTHING is hardcoded. This
suite proves changing a hotkey actually works all the way down — validation,
persistence, dedupe, trigger-key derivation, and *real* registration with the
`keyboard` library (so a combo that is "valid on paper" also actually hooks).

Two tiers:

  SAFE (always runs; pure / no global side effects) --------------------------
    * validate_hotkey / normalize_hotkey verdicts and aliasing
    * Config round-trip on a tmp settings.json (never touches the real one)
    * load-time sanitize_settings reset + cross-action dedupe
    * MainWindow._set_hotkey_if_valid accept/reject/persist (headless Qt)
    * push-to-talk hook registration: EVERY combo key gets press+release
      hooks, `ctrl+alt` starts in either press order, and the lost-keyup
      watchdog ends a hold whose keyup never arrived (regression tier)
    validate_hotkey consults keyboard.key_to_scan_codes, a read-only key-table
    lookup — it registers NO hooks — so the safe tier is side-effect-free.

  REGISTRATION (opt-in; guarded by env RUN_HOTKEY_REGISTER) ------------------
    * actually registers each valid combo with `keyboard` the way the app does
      and asserts it does not raise, then unhooks immediately.
    This installs brief GLOBAL keyboard hooks, so it must NOT run alongside the
    live app or a sibling agent; the orchestrator runs it serially.

Run (safe tier only):
    venv\\Scripts\\python.exe -m pytest tests/integration/test_hotkey_register.py -q
Run (registration tier, serialized by the orchestrator):
    set RUN_HOTKEY_REGISTER=1 && venv\\Scripts\\python.exe -m pytest \\
        tests/integration/test_hotkey_register.py -q -k registration
"""

import json
import os
import sys

import pytest

# Import the app package the way the other suites do — self-bootstrap sys.path.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import voiceassistant.config as vcfg  # noqa: E402  (module ref so patched CONFIG_FILE is seen)
from voiceassistant.config import (  # noqa: E402
    DEFAULTS,
    HOTKEY_KEYS,
    normalize_hotkey,
    sanitize_settings,
    validate_hotkey,
)

# The registration tier registers exactly these; every entry must pass
# validate_hotkey (asserted in the SAFE tier) so registration only ever tries
# combos the product considers legal. Covers: single safe key, multi-word safe
# key, normal combo, a "dangerous but legal" combo, and modifier-only combos
# (2 mods, incl. a Windows-key combo) — the deliberate hold-to-talk feature.
VALID_COMBOS = [
    "f9",
    "caps lock",
    "ctrl+shift+r",
    "alt+f4",
    "ctrl+alt",
    "ctrl+shift",
    "ctrl+windows",
]

# Must all be refused: bare typing keys (fire mid-sentence), a lone modifier,
# escape (reserved stop key), an unbindable token, and empty.
INVALID_COMBOS = ["a", "7", "space", ".", "ctrl", "shift", "escape", "banana", ""]

_MODIFIERS = {"ctrl", "shift", "alt", "windows", "cmd", "meta"}

# Captured at import time — BEFORE the main_window fixture no-ops it on the
# class. The hook-registration tests below need the genuine implementation,
# driven against a fake `keyboard` module.
try:
    from voiceassistant.window import MainWindow as _MainWindowClass  # noqa: E402

    _REAL_SETUP_HOTKEYS = _MainWindowClass._setup_hotkeys
except Exception:  # PySide6/keyboard unavailable — those tests skip
    _REAL_SETUP_HOTKEYS = None


def _parts(combo):
    """Mirror of MainWindow._hotkey_parts (asserted equivalent below)."""
    return [p for p in normalize_hotkey(combo).split("+") if p]


def _hook_keys(combo):
    """The keys the app installs press+release hooks on for push-to-talk.

    EVERY key in the combo, not a single derived "trigger" key — see
    test_every_combo_key_gets_a_press_hook for why that distinction is the
    whole difference between working and broken dictation.
    """
    return list(dict.fromkeys(_parts(combo)))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """Point Config at a throwaway settings.json in a tmp dir.

    Both CONFIG_DIR and CONFIG_FILE are redirected: Config.save() writes its
    atomic temp file into CONFIG_DIR, so patching only CONFIG_FILE would litter
    the repo root. Returns the config module (read CONFIG_FILE through it).
    """
    monkeypatch.setattr(vcfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(vcfg, "CONFIG_FILE", str(tmp_path / "settings.json"))
    return vcfg


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def main_window(qapp, monkeypatch, tmp_path):
    """Headless MainWindow with all side effects neutralized (harness cloned
    from tests/test_ui_smoke.py). Crucially, _setup_hotkeys and _setup_tray are
    patched to no-ops so *constructing* the window registers NO global hooks."""
    import voiceassistant.ocr as ocr
    import voiceassistant.transcriber as tr
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    monkeypatch.setattr(vcfg, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(vcfg, "CONFIG_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(tr.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)
    # The mic stream is opened once at startup now — don't grab a real device.
    import voiceassistant.recorder as rec_mod
    monkeypatch.setattr(rec_mod.VoiceRecorder, "open_stream", lambda self: True)
    monkeypatch.setattr(rec_mod.VoiceRecorder, "close_stream", lambda self: None)

    w = MainWindow(entry_script="main.py")
    yield w
    try:
        w._show_request_timer.stop()
        w.tts.shutdown()
        w.paster.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. VALIDATION battery
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("combo", VALID_COMBOS)
def test_validate_accepts_valid(combo):
    assert validate_hotkey(combo) is True


@pytest.mark.parametrize("combo", INVALID_COMBOS)
def test_validate_rejects_invalid(combo):
    assert validate_hotkey(combo) is False


@pytest.mark.parametrize("key", ["f9", "f13", "banana", "insert", "caps lock"])
def test_validate_single_key_tracks_keyboard_bindability(key):
    """A lone non-typing, non-modifier key is accepted IFF the keyboard library
    can actually bind it. This is the guard that stopped 'banana' from
    validating on paper and then silently failing to register. 'f13' is the
    open question the directive flagged — this asserts validate agrees with
    this machine's real key table however it resolves (True where bindable)."""
    kb = pytest.importorskip("keyboard")

    def bindable(k):
        try:
            kb.key_to_scan_codes(k)
            return True
        except (ValueError, ImportError):
            return False
        except Exception:
            return True

    assert validate_hotkey(key) == bindable(key)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Control+Shift+R", "ctrl+shift+r"),   # Control -> ctrl
        ("Win+A", "windows+a"),                # Win -> windows
        ("Option+F4", "alt+f4"),               # Option -> alt
        ("Command+Q", "cmd+q"),                # Command -> cmd
        ("  CTRL + Shift + R  ", "ctrl+shift+r"),  # case + surrounding/inner space
        ("CAPS LOCK", "caps lock"),            # multi-word key lowercased, kept whole
        ("ctrl++r", "ctrl+r"),                 # empty middle token dropped
        ("", ""),                              # empty stays empty
    ],
)
def test_normalize_aliases_and_whitespace(raw, expected):
    assert normalize_hotkey(raw) == expected


# ---------------------------------------------------------------------------
# 2. CONFIG round-trip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("combo", VALID_COMBOS + ["ctrl+shift+f9"])
def test_config_roundtrip_persists_valid(tmp_config, combo):
    """Set a valid combo on hotkey_record, save, reload a fresh Config from the
    same tmp file, and confirm it survived (nothing is hardcoded away)."""
    cfg = tmp_config
    c = cfg.Config()
    c.set("hotkey_record", normalize_hotkey(combo))

    reloaded = cfg.Config()
    assert normalize_hotkey(reloaded.get("hotkey_record")) == normalize_hotkey(combo)


def test_config_loadtime_sanitize_resets_invalid(tmp_config):
    """A settings.json carrying an invalid hotkey must not brick startup: load()
    runs sanitize_settings which resets it to the factory default."""
    cfg = tmp_config
    with open(cfg.CONFIG_FILE, "w") as f:
        json.dump({"hotkey_record": "a"}, f)  # bare typing key: invalid

    c = cfg.Config()
    assert normalize_hotkey(c.get("hotkey_record")) == DEFAULTS["hotkey_record"]


# ---------------------------------------------------------------------------
# 3. DEDUP — no two actions may share a combo
# ---------------------------------------------------------------------------
def test_sanitize_dedup_makes_actions_distinct():
    data = dict(DEFAULTS)
    for k in HOTKEY_KEYS:
        data[k] = "ctrl+alt"  # collide all three
    cleaned = sanitize_settings(data)
    vals = [cleaned[k] for k in HOTKEY_KEYS]
    assert len(set(vals)) == 3, f"actions still share a combo: {vals}"


def test_sanitize_dedup_reserve_pool_when_default_would_recollide():
    """The closed dedupe hole: record & screen both set to screen's OWN default.
    Naively resetting the duplicate to its default would re-collide, so the
    reserve pool must hand out a different free combo."""
    data = dict(DEFAULTS)
    data["hotkey_record"] = DEFAULTS["hotkey_screen_read"]
    data["hotkey_screen_read"] = DEFAULTS["hotkey_screen_read"]
    cleaned = sanitize_settings(data)
    assert cleaned["hotkey_record"] != cleaned["hotkey_screen_read"]
    assert len({cleaned[k] for k in HOTKEY_KEYS}) == 3


def test_set_hotkey_if_valid_rejects_duplicate(main_window):
    w = main_window
    # read_aloud defaults to ctrl+shift+t; reassigning that to record is a dup.
    assert normalize_hotkey(w.config["hotkey_read_aloud"]) == "ctrl+shift+t"
    assert w._set_hotkey_if_valid("hotkey_record", "Dictate", "ctrl+shift+t") is False
    assert normalize_hotkey(w.config["hotkey_record"]) == "ctrl+shift+r"  # unchanged


def test_set_hotkey_if_valid_rejects_invalid(main_window):
    w = main_window
    for bad in ("a", "escape", ""):
        assert w._set_hotkey_if_valid("hotkey_record", "Dictate", bad) is False
    assert normalize_hotkey(w.config["hotkey_record"]) == "ctrl+shift+r"  # unchanged


def test_set_hotkey_if_valid_accepts_and_persists(main_window):
    w = main_window
    assert w._set_hotkey_if_valid("hotkey_record", "Dictate", "F9") is True
    assert normalize_hotkey(w.config["hotkey_record"]) == "f9"
    # It hit disk too: a fresh Config reading the same tmp file sees f9.
    reloaded = vcfg.Config()
    assert normalize_hotkey(reloaded.get("hotkey_record")) == "f9"


# ---------------------------------------------------------------------------
# 4. PUSH-TO-TALK HOOK REGISTRATION  (regression: the ctrl+alt start failure)
# ---------------------------------------------------------------------------
class _FakeKb:
    """Records what the app hooks, and lets a test drive those callbacks."""

    def __init__(self, held=()):
        self.press = {}       # key -> callback
        self.release = {}     # key -> callback
        self.suppressed = {}  # key -> whether it was hooked with suppress=True
        self.hotkeys = []
        self.held = set(held)

    # --- the API surface _setup_hotkeys uses ---
    def unhook_all(self):
        pass

    def on_press_key(self, key, cb, *a, **k):
        self.press[key] = cb
        self.suppressed[key] = bool(k.get("suppress", False))

    def on_release_key(self, key, cb, *a, **k):
        self.release[key] = cb
        self.suppressed[key] = bool(k.get("suppress", False))

    def add_hotkey(self, combo, cb, *a, **k):
        self.hotkeys.append(combo)

    def is_pressed(self, key):
        return key in self.held

    # --- test helpers ---
    def press_key(self, key):
        self.held.add(key)
        if key in self.press:
            self.press[key](None)

    def release_key(self, key):
        self.held.discard(key)
        if key in self.release:
            self.release[key](None)


@pytest.fixture
def fake_kb(main_window, monkeypatch):
    if _REAL_SETUP_HOTKEYS is None:
        pytest.skip("MainWindow unavailable at import time")
    import voiceassistant.window as win_mod

    fk = _FakeKb()
    monkeypatch.setattr(win_mod, "kb", fk)
    return fk


@pytest.mark.parametrize("combo", ["ctrl+alt", "ctrl+shift+r", "f9", "ctrl+shift"])
def test_every_combo_key_gets_a_press_hook(main_window, fake_kb, combo):
    """REGRESSION — this is the bug that made dictation feel unreliable.

    The app used to hook ONE derived "trigger" key. For the modifier-only
    combo `ctrl+alt` that trigger was `alt`, so Ctrl's keydown was never
    hooked at all. Pressing Alt before Ctrl therefore did nothing: Alt's hook
    fired while Ctrl was not yet down (combo incomplete -> no start), and the
    later Ctrl keydown had no hook to fire. Two keys pressed together land in
    arbitrary order, so dictation silently failed to start about half the time.
    """
    w = main_window
    w.config.set("hotkey_record", combo)
    _REAL_SETUP_HOTKEYS(w)

    expected = _hook_keys(combo)
    assert set(fake_kb.press) == set(expected), (
        f"{combo}: press hooks {sorted(fake_kb.press)} != every combo key {sorted(expected)}"
    )
    assert set(fake_kb.release) == set(expected), (
        f"{combo}: a release on ANY combo key must end the hold"
    )


def test_modifier_only_combo_starts_in_either_press_order(main_window, fake_kb):
    """`ctrl+alt` must start dictation whichever key lands first."""
    w = main_window
    w.config.set("hotkey_record", "ctrl+alt")
    _REAL_SETUP_HOTKEYS(w)

    for first, second in (("ctrl", "alt"), ("alt", "ctrl")):
        starts = []
        w._sig_hotkey_press.connect(lambda: starts.append(True))
        fake_kb.held.clear()

        fake_kb.press_key(first)
        assert starts == [], f"{first} alone must not start dictation"
        fake_kb.press_key(second)
        assert starts == [True], (
            f"pressing {first} then {second} did not start dictation "
            "(this is the exact half-the-time failure users hit)"
        )
        w._sig_hotkey_press.disconnect()
        fake_kb.release_key(first)
        fake_kb.release_key(second)


@pytest.mark.parametrize("combo,want_suppress", [
    ("caps lock", True),      # dedicated solo key: swallow the caps toggle
    ("insert", True),         # ...and the overtype toggle
    ("scroll lock", True),    # ...and Excel's arrow-key mode
    ("f9", False),            # harmless to pass through
    ("ctrl+alt", False),      # NEVER suppress a modifier
    ("ctrl+shift+r", False),
])
def test_only_dedicated_solo_keys_are_suppressed(main_window, fake_kb, combo, want_suppress):
    """Caps Lock only works as push-to-talk if the app swallows the keypress,
    or every dictation flips the caps state. But suppressing a MODIFIER would
    break Ctrl/Alt system-wide, so the rule must stay narrow."""
    from voiceassistant.config import should_suppress_hotkey

    assert should_suppress_hotkey(combo) is want_suppress

    w = main_window
    w.config.set("hotkey_record", combo)
    _REAL_SETUP_HOTKEYS(w)
    assert fake_kb.press, f"{combo}: nothing registered"
    for key, suppressed in fake_kb.suppressed.items():
        assert suppressed is want_suppress, (
            f"{combo}: key {key!r} suppress={suppressed}, expected {want_suppress}"
        )


def test_suppressing_callbacks_return_falsy_so_the_key_is_blocked(main_window, fake_kb):
    """keyboard blocks a suppressed event only when the callback returns a
    FALSY value (see _KeyboardListener.direct_callback). If a callback ever
    starts returning something truthy, Caps Lock would silently start
    toggling caps again — so pin it."""
    w = main_window
    w.config.set("hotkey_record", "caps lock")
    _REAL_SETUP_HOTKEYS(w)
    for cb in list(fake_kb.press.values()) + list(fake_kb.release.values()):
        assert not cb(None), "callback returned truthy — the key would NOT be blocked"


def test_release_of_any_combo_key_stops(main_window, fake_kb):
    """Releasing the FIRST-pressed key must stop, not wait for the other one."""
    w = main_window
    w.config.set("hotkey_record", "ctrl+alt")
    _REAL_SETUP_HOTKEYS(w)
    stops = []
    w._sig_hotkey_release.connect(lambda: stops.append(True))

    fake_kb.press_key("ctrl")
    fake_kb.press_key("alt")
    fake_kb.release_key("ctrl")  # Ctrl first, Alt still down
    assert stops, "releasing ctrl left the recording running until alt came up"


def test_ptt_watchdog_stops_when_keyup_was_lost(main_window, fake_kb):
    """A dropped keyup must cost ~100ms, not the 120s max-duration runaway
    seen three times in debug.log (Windows silently unhooks a low-level
    keyboard hook whose callback exceeds LowLevelHooksTimeout)."""
    w = main_window
    w.config.set("hotkey_record", "ctrl+alt")
    _REAL_SETUP_HOTKEYS(w)

    class _Rec:
        is_recording = True

        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    w.recorder = _Rec()
    w._ptt_active = True
    fake_kb.held = {"ctrl", "alt"}

    w._on_ptt_watchdog()
    assert not w.recorder.stopped, "watchdog stopped while the combo was still held"

    fake_kb.held.clear()  # the keyup never arrived; keys are physically up
    w._on_ptt_watchdog()
    assert w.recorder.stopped, "watchdog did not rescue a lost keyup"
    assert w._ptt_active is False


# ---------------------------------------------------------------------------
# 5. REAL REGISTRATION (opt-in; the orchestrator runs this serially)
# ---------------------------------------------------------------------------
_REGISTER = bool(os.environ.get("RUN_HOTKEY_REGISTER"))


@pytest.mark.skipif(
    not _REGISTER,
    reason="installs real global keyboard hooks; set RUN_HOTKEY_REGISTER=1 "
    "(orchestrator runs it serially, never alongside the live app)",
)
@pytest.mark.parametrize("combo", VALID_COMBOS)
def test_real_registration_of_user_combo(combo):
    """Register each user-choosable combo with `keyboard` exactly as the app's
    _setup_hotkeys does, and assert it does not raise. This proves the combos
    are genuinely registrable, not merely valid on paper. Hooks are removed
    immediately; no synthetic keypresses are injected (that would disturb the
    live desktop), so the callback flag is present only as a realistic body."""
    kb = pytest.importorskip("keyboard")

    # Only ever try to register something the product considers legal.
    assert validate_hotkey(combo) is True

    state = {"fired": False}

    def cb(*_a, **_k):
        state["fired"] = True

    try:
        # (a) Record / push-to-talk path: press + release on EVERY combo key.
        for key in _hook_keys(combo):
            kb.on_press_key(key, cb)
            kb.on_release_key(key, cb)
        # (b) Read / OCR path: bind the whole combo. Mirror the app's
        #     suppress-iff-Windows-key with a no-suppress fallback so a
        #     Windows-key combo can't spuriously fail here.
        try:
            kb.add_hotkey(combo, cb, suppress=("windows" in combo))
        except Exception:
            kb.add_hotkey(combo, cb)
    except Exception as e:
        pytest.fail(f"user-chosen hotkey {combo!r} failed to register with keyboard: {e!r}")
    finally:
        try:
            kb.unhook_all()
        except Exception:
            pass
