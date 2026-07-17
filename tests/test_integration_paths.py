"""Integration-path tests for four previously-untested wirings.

These four paths each cross a module boundary that the unit/characterization
suites do NOT exercise, yet each is load-bearing for correctness or a clean
exit. All of it runs headless on the Qt "offscreen" platform and is fully
hermetic — no focus steal, no audio/GPU/model loads, no registry writes, no
real hotkey hooks — so it runs in the default suite (no RUN_INTEGRATION gate
needed). The process-global excepthooks touched in area 4 are saved and
restored so the pytest process is never poisoned.

Areas:
  1. RegionSelector — the DPI fix: it DRAWS in Qt logical coords but EMITS the
     physical rectangle from winapi.get_cursor_pos(). This is the only check
     that the scaled-monitor OCR capture rectangle is computed correctly.
  2. MainWindow._on_settings — the settings apply/persist wiring.
  3. MainWindow.closeEvent — close-to-tray vs. force-quit teardown.
  4. applog crash handlers — the safety net: worker-thread and main-thread
     unhandled exceptions reach the rotating log + the UI notifier, and dbg()
     honors the opt-in debug flag.
"""

import json
import logging
import os
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PySide6")
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ===========================================================================
# Area 1 — RegionSelector coordinates + DPI
# ===========================================================================
# The selector draws its rubber-band using Qt logical coordinates
# (event.globalPosition), but must EMIT the region in PHYSICAL pixels
# (winapi.get_cursor_pos) — the space mss captures in. On a 125%/150% monitor
# the two spaces differ, and the emitted rectangle must track the physical one
# or OCR grabs the wrong area.
class TestRegionSelectorDpi:
    def _selector(self, qapp):
        from voiceassistant.ocr import RegionSelector

        sel = RegionSelector()
        got = []
        cancels = []
        sel.region_selected.connect(lambda x, y, w, h: got.append((x, y, w, h)))
        sel.cancelled.connect(lambda: cancels.append(True))
        return sel, got, cancels

    def _script_cursor(self, monkeypatch, points):
        """winapi.get_cursor_pos returns each point in `points` in turn."""
        import voiceassistant.winapi as winapi

        seq = list(points)
        state = {"i": 0}

        def fake():
            pt = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            return pt

        monkeypatch.setattr(winapi, "get_cursor_pos", fake)

    def _press(self, sel, logical_global):
        ev = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(0, 0),
            QPointF(*logical_global),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        sel.mousePressEvent(ev)

    def _release(self, sel):
        ev = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(0, 0),
            QPointF(0, 0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        sel.mouseReleaseEvent(ev)

    def _esc(self, sel):
        sel.keyPressEvent(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                      Qt.KeyboardModifier.NoModifier)
        )

    def test_esc_midselection_then_release_does_not_double_emit(self, qapp, monkeypatch):
        # Regression: press -> Esc -> button-up. Esc must fully reset the
        # state machine so the implicit-grab button-up (delivered to the now
        # hidden widget) does NOT fire a second region_selected from stale
        # coords after cancel. Before the fix this emitted an unwanted OCR
        # region right after the user cancelled.
        sel, got, cancels = self._selector(qapp)
        self._script_cursor(monkeypatch, [(200, 150), (500, 450)])
        self._press(sel, logical_global=(160, 120))
        self._esc(sel)
        self._release(sel)
        assert got == [], f"Esc-cancelled selection still emitted a region: {got}"
        assert cancels == [True], f"expected exactly one cancel, got {cancels}"
        assert sel._is_selecting is False and sel._start_phys is None

    def test_emits_physical_rectangle_not_logical(self, qapp, monkeypatch):
        sel, got, cancels = self._selector(qapp)
        # Physical cursor: start (200,150), end (500,450).
        # Qt logical global at press is deliberately DIFFERENT (160,120) —
        # as it would be under display scaling. If the code mistakenly used
        # logical coords the emitted rect would not match the physical one.
        self._script_cursor(monkeypatch, [(200, 150), (500, 450)])

        self._press(sel, logical_global=(160, 120))
        # Press captures BOTH spaces separately.
        assert (sel._start_pos.x(), sel._start_pos.y()) == (160, 120), \
            "logical draw-coord not captured from the event"
        assert sel._start_phys == (200, 150), \
            "physical coord not captured from winapi.get_cursor_pos"

        self._release(sel)
        # Emitted rectangle is PHYSICAL: min-corner (200,150), size (300,300).
        assert got == [(200, 150, 300, 300)], got
        assert cancels == []

    def test_normalizes_min_corner_and_abs_size(self, qapp, monkeypatch):
        # Drag up-and-left: physical start (500,450) -> end (200,150).
        # Result must still be the SAME normalized rect (min corner + abs size).
        sel, got, cancels = self._selector(qapp)
        self._script_cursor(monkeypatch, [(500, 450), (200, 150)])
        self._press(sel, logical_global=(400, 360))
        self._release(sel)
        assert got == [(200, 150, 300, 300)], got
        assert cancels == []

    def test_tiny_drag_under_10px_cancels(self, qapp, monkeypatch):
        # A sub-10px drag is a click, not a region — must cancel, not emit.
        sel, got, cancels = self._selector(qapp)
        self._script_cursor(monkeypatch, [(300, 300), (305, 304)])
        self._press(sel, logical_global=(300, 300))
        self._release(sel)
        assert got == [], f"tiny drag wrongly emitted a region: {got}"
        assert cancels == [True]

    def test_escape_cancels(self, qapp, monkeypatch):
        sel, got, cancels = self._selector(qapp)
        self._script_cursor(monkeypatch, [(200, 150)])
        self._press(sel, logical_global=(200, 150))
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                       Qt.KeyboardModifier.NoModifier)
        sel.keyPressEvent(ev)
        assert cancels == [True]
        assert got == []


# ===========================================================================
# Shared offscreen MainWindow harness (areas 2 & 3)
# ===========================================================================
@pytest.fixture
def main_window(qapp, monkeypatch, tmp_path):
    """Real MainWindow with all side effects neutralized (ui_smoke pattern).

    CONFIG_DIR is also redirected to tmp so config.save() (atomic temp-file +
    os.replace) writes stay entirely within tmp — no litter in the app dir.
    """
    import voiceassistant.config as cfg
    import voiceassistant.ocr as ocr
    import voiceassistant.transcriber as tr
    import voiceassistant.winapi as winapi
    from voiceassistant.window import MainWindow

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(cfg, "CONFIG_FILE", str(cfg_dir / "settings.json"))
    monkeypatch.setattr(tr.Transcriber, "load_model", lambda self: None)
    monkeypatch.setattr(ocr.OCREngine, "load_model", lambda self: None)
    monkeypatch.setattr(winapi, "set_start_with_windows", lambda *a, **k: True)
    monkeypatch.setattr(MainWindow, "_setup_hotkeys", lambda self: None)
    monkeypatch.setattr(MainWindow, "_setup_tray", lambda self: None)

    w = MainWindow(entry_script="main.py")
    yield w
    try:
        w._show_request_timer.stop()
    except Exception:
        pass
    for name in ("tts", "paster"):
        try:
            getattr(w, name).shutdown()
        except Exception:
            pass


class _ShutdownSpy:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self, *a, **k):
        self.shutdown_called = True


class _FakeTray:
    def __init__(self, visible):
        self._visible = visible

    def isVisible(self):
        return self._visible


class _FakeRecorder:
    def __init__(self, recording):
        self._recording = recording
        self.stop_called = False

    @property
    def is_recording(self):
        return self._recording

    def stop(self):
        self.stop_called = True


class _FakeCloseEvent:
    def __init__(self):
        self.ignored = False
        self.accepted = False

    def ignore(self):
        self.ignored = True

    def accept(self):
        self.accepted = True


def _swap_engines(mw, tts, paster):
    """Release the real tts/paster workers, then install shutdown spies."""
    for name in ("tts", "paster"):
        try:
            getattr(mw, name).shutdown()
        except Exception:
            pass
    mw.tts = tts
    mw.paster = paster


# ===========================================================================
# Area 2 — MainWindow._on_settings apply/persist wiring
# ===========================================================================
class TestSettingsApply:
    def test_accepted_dialog_applies_and_persists_everything(
        self, main_window, monkeypatch
    ):
        import voiceassistant.applog as applog
        import voiceassistant.config as cfg
        import voiceassistant.winapi as winapi
        from voiceassistant.settings_dialog import SettingsDialog

        mw = main_window
        # Baseline: the values we set must differ from current so the wiring
        # (esp. the change_model guard) is actually exercised.
        assert mw.config["whisper_model"] == "medium"
        assert mw.transcriber.language == "en"

        new_vals = {
            "audio_device": 3,
            "whisper_model": "small",        # changed -> must call change_model
            "whisper_language": "es",
            "font_size": 18,
            "always_on_top": True,
            "start_with_windows": True,      # changed -> registry call
            "start_minimized": False,
            "light_cleanup": False,
            "debug_logging": True,           # changed -> applog.set_debug
            # hotkeys are not edited in this dialog; pass current values through
            "hotkey_record": mw.config["hotkey_record"],
            "hotkey_read_aloud": mw.config["hotkey_read_aloud"],
            "hotkey_screen_read": mw.config["hotkey_screen_read"],
        }
        monkeypatch.setattr(SettingsDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(SettingsDialog, "get_values",
                            lambda self: dict(new_vals))

        change_model_calls = []
        monkeypatch.setattr(mw.transcriber, "change_model",
                            lambda m, *a, **k: change_model_calls.append(m))
        sww_calls = []

        def _rec_sww(enabled, script=None):
            sww_calls.append(enabled)
            return True

        monkeypatch.setattr(winapi, "set_start_with_windows", _rec_sww)
        set_debug_calls = []
        monkeypatch.setattr(applog, "set_debug",
                            lambda v: set_debug_calls.append(v))

        mw._on_settings()

        # --- engine wiring ---
        assert change_model_calls == ["small"], \
            "change_model not called with the new model size"
        assert mw.transcriber.language == "es", "language not pushed to transcriber"
        assert mw.recorder.device == 3, "audio device not applied to recorder"

        # --- persisted config (in memory) ---
        assert mw.config["whisper_model"] == "small"
        assert mw.config["whisper_language"] == "es"
        assert mw.config["font_size"] == 18
        assert mw.config["always_on_top"] is True
        assert mw.config["start_with_windows"] is True
        assert mw.config["start_minimized"] is False
        assert mw.config["light_cleanup"] is False
        assert mw.config["debug_logging"] is True
        assert mw.config["audio_device"] == 3

        # --- side wiring ---
        assert sww_calls == [True], "set_start_with_windows not called with new value"
        assert set_debug_calls == [True], "applog.set_debug not called"
        assert mw.text_output.font().pointSize() == 18, "font size not applied"

        # --- persisted to disk (proves config.save ran) ---
        on_disk = json.loads(open(cfg.CONFIG_FILE, encoding="utf-8").read())
        assert on_disk["whisper_model"] == "small"
        assert on_disk["font_size"] == 18
        assert on_disk["debug_logging"] is True

    def test_rejected_dialog_changes_nothing(self, main_window, monkeypatch):
        from voiceassistant.settings_dialog import SettingsDialog

        mw = main_window
        before_model = mw.config["whisper_model"]
        monkeypatch.setattr(SettingsDialog, "exec",
                            lambda self: QDialog.DialogCode.Rejected)
        called = []
        monkeypatch.setattr(SettingsDialog, "get_values",
                            lambda self: called.append(True) or {})

        mw._on_settings()

        assert called == [], "get_values read despite Cancel — apply ran on reject"
        assert mw.config["whisper_model"] == before_model


# ===========================================================================
# Area 3 — MainWindow.closeEvent teardown
# ===========================================================================
class TestCloseEventTeardown:
    def test_close_to_tray_does_not_teardown(self, main_window, monkeypatch):
        mw = main_window
        mw.tray = _FakeTray(visible=True)
        mw._force_quit = False
        tts, paster = _ShutdownSpy(), _ShutdownSpy()
        _swap_engines(mw, tts, paster)

        hide_calls = []
        monkeypatch.setattr(mw, "hide", lambda: hide_calls.append(True))

        ev = _FakeCloseEvent()
        mw.closeEvent(ev)

        assert ev.ignored is True, "close-to-tray must ignore() the event"
        assert ev.accepted is False
        assert hide_calls == [True], "window was not hidden to the tray"
        # Crucially, NOTHING was torn down — the app keeps running.
        assert tts.shutdown_called is False
        assert paster.shutdown_called is False
        assert mw._show_request_timer.isActive() is True

    def test_force_quit_tears_down_cleanly(self, main_window, monkeypatch):
        import keyboard as kb
        import voiceassistant.winapi as winapi

        mw = main_window
        # Tray is visible, but force_quit must win and fully tear down.
        mw.tray = _FakeTray(visible=True)
        mw._force_quit = True
        tts, paster = _ShutdownSpy(), _ShutdownSpy()
        _swap_engines(mw, tts, paster)
        mw.recorder = _FakeRecorder(recording=True)

        unhook_calls = []
        monkeypatch.setattr(kb, "unhook_all", lambda: unhook_calls.append(True))
        monkeypatch.setattr(winapi, "release_single_instance_lock", lambda: None)
        save_calls = []
        monkeypatch.setattr(mw.config, "save", lambda: save_calls.append(True))
        monkeypatch.setattr(mw.config, "flush", lambda: None)

        ev = _FakeCloseEvent()
        mw.closeEvent(ev)

        assert tts.shutdown_called is True, "tts worker not shut down (zombie)"
        assert paster.shutdown_called is True, "paste worker not shut down (zombie)"
        assert unhook_calls, "kb.unhook_all not attempted"
        assert mw.recorder.stop_called is True, "active recording not stopped"
        assert save_calls, "config not saved on quit"
        assert mw._show_request_timer.isActive() is False, "show-request timer left running"
        assert ev.accepted is True, "quit must accept() the close event"
        assert ev.ignored is False


# ===========================================================================
# Area 4 — applog crash handlers (the safety net)
# ===========================================================================
@pytest.fixture
def applog_tmp(tmp_path, monkeypatch):
    """Isolate applog onto a tmp log, install fresh crash handlers, and fully
    restore process-global state (excepthooks, logger handlers, faulthandler)
    afterwards so the pytest process is not poisoned."""
    import faulthandler

    import voiceassistant.applog as applog

    saved = dict(
        logger=applog._logger,
        debug=applog._debug_enabled,
        notify=applog._notify_cb,
        crash_file=applog._crash_file,
        sys_hook=sys.excepthook,
        thread_hook=threading.excepthook,
        fault_enabled=faulthandler.is_enabled(),
    )
    # Detach any existing handlers on the shared "voiceassistant" logging
    # singleton so nothing during the test touches the REAL debug.log.
    singleton = logging.getLogger("voiceassistant")
    saved_handlers = list(singleton.handlers)
    for h in saved_handlers:
        singleton.removeHandler(h)

    log_path = tmp_path / "debug.log"
    crash_path = tmp_path / "crash.log"
    monkeypatch.setattr(applog, "LOG_PATH", str(log_path))
    monkeypatch.setattr(applog, "CRASH_LOG_PATH", str(crash_path))
    applog._logger = None
    applog._debug_enabled = False
    applog._notify_cb = None
    applog._crash_file = None

    yield applog, log_path, crash_path

    # Restore process-global hooks FIRST — before anything else can raise.
    sys.excepthook = saved["sys_hook"]
    threading.excepthook = saved["thread_hook"]
    # Close + remove the tmp handler(s) we created, then restore the originals.
    for h in list(singleton.handlers):
        try:
            h.close()
        except Exception:
            pass
        singleton.removeHandler(h)
    for h in saved_handlers:
        singleton.addHandler(h)
    if applog._crash_file is not None:
        try:
            applog._crash_file.close()
        except Exception:
            pass
    try:
        faulthandler.disable()
        if saved["fault_enabled"]:
            faulthandler.enable()
    except Exception:
        pass
    applog._logger = saved["logger"]
    applog._debug_enabled = saved["debug"]
    applog._notify_cb = saved["notify"]
    applog._crash_file = saved["crash_file"]


class TestCrashHandlers:
    def test_worker_thread_exception_logged_and_notified(self, applog_tmp):
        applog, log_path, _crash_path = applog_tmp
        notices = []
        applog.install_crash_handlers()
        applog.set_notifier(notices.append)

        # A real worker-style thread raising an unhandled exception must route
        # through the installed threading.excepthook.
        def boom():
            raise ValueError("synthetic-thread-boom")

        t = threading.Thread(target=boom, name="boom-worker")
        t.start()
        t.join(5)
        assert not t.is_alive()

        content = log_path.read_text(encoding="utf-8")
        assert "UNHANDLED EXCEPTION" in content, "crash not written to the log"
        assert "Traceback (most recent call last)" in content, "no traceback logged"
        assert "ValueError" in content
        assert "synthetic-thread-boom" in content
        # The notifier (UI/tray surfacing) fired.
        assert notices, "crash notifier was never invoked"
        assert any("ValueError" in n for n in notices), notices

    def test_main_thread_excepthook_logged_and_notified(self, applog_tmp):
        applog, log_path, _crash_path = applog_tmp
        notices = []
        applog.install_crash_handlers()
        applog.set_notifier(notices.append)

        # Build a real traceback, then invoke the installed sys.excepthook the
        # way the interpreter would on an unhandled main-thread exception.
        try:
            raise RuntimeError("synthetic-main-boom")
        except RuntimeError as e:
            sys.excepthook(type(e), e, e.__traceback__)

        content = log_path.read_text(encoding="utf-8")
        assert "UNHANDLED EXCEPTION (main thread)" in content
        assert "RuntimeError" in content
        assert "synthetic-main-boom" in content
        assert any("RuntimeError" in n for n in notices), notices

    def test_notifier_failure_is_swallowed(self, applog_tmp):
        # A throwing notifier must never turn a crash into a second crash.
        applog, log_path, _crash_path = applog_tmp
        applog.install_crash_handlers()

        def bad_notifier(_msg):
            raise RuntimeError("notifier blew up")

        applog.set_notifier(bad_notifier)

        def boom():
            raise ValueError("boom-with-bad-notifier")

        t = threading.Thread(target=boom, name="boom-2")
        t.start()
        t.join(5)  # must not hang or propagate
        assert not t.is_alive()
        assert "boom-with-bad-notifier" in log_path.read_text(encoding="utf-8")


class TestDebugGate:
    def test_dbg_silent_when_disabled_then_writes_when_enabled(self, applog_tmp):
        applog, log_path, _crash_path = applog_tmp

        # Debug OFF (the default): dbg() must write nothing.
        applog.dbg("MARKER-should-not-appear")
        applog.info("logger-init")  # force logger/file creation
        content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        assert "MARKER-should-not-appear" not in content, \
            "dbg() wrote while debug logging was OFF"

        # Debug ON: dbg() must write.
        applog.set_debug(True)
        applog.dbg("MARKER-should-appear")
        content2 = log_path.read_text(encoding="utf-8")
        assert "MARKER-should-appear" in content2, \
            "dbg() did not write while debug logging was ON"
