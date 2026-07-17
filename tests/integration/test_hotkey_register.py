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
    * MainWindow._hotkey_trigger_key derivation
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


def _parts(combo):
    """Mirror of MainWindow._hotkey_parts (asserted equivalent below)."""
    return [p for p in normalize_hotkey(combo).split("+") if p]


def _trigger_key(combo):
    """Mirror of MainWindow._hotkey_trigger_key (asserted equivalent below).

    Kept local so the registration tier depends only on `keyboard`, not on a
    fully constructed Qt MainWindow. test_trigger_key_matches_mainwindow ties
    this mirror to the real method so it can never silently drift.
    """
    non_mod = [p for p in _parts(combo) if p not in _MODIFIERS]
    return non_mod[-1] if non_mod else _parts(combo)[-1]


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
# 4. TRIGGER-KEY logic
# ---------------------------------------------------------------------------
def test_trigger_key_matches_mainwindow(main_window):
    w = main_window
    assert w._hotkey_trigger_key("ctrl+shift+r") == "r"       # real key wins
    assert w._hotkey_trigger_key("f9") == "f9"                # single key
    assert w._hotkey_trigger_key("ctrl+alt") == "alt"         # modifier-only -> last part
    # Tie the registration tier's local mirror to the real method.
    for combo in VALID_COMBOS:
        assert _trigger_key(combo) == w._hotkey_trigger_key(combo)


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

    trigger = _trigger_key(combo)
    try:
        # (a) Record / push-to-talk path: watch the trigger key press + release.
        kb.on_press_key(trigger, cb)
        kb.on_release_key(trigger, cb)
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
