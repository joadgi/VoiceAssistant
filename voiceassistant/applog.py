"""Privacy-safe application logging + crash visibility.

Rules:
  * NEVER log payloads — no dictated text, no clipboard contents, no window
    titles. Lengths, handles, and flags only.
  * debug logging is OPT-IN (config "debug_logging", default off); errors and
    crashes are always recorded.
  * rotating file — the log can never grow unbounded again (it once hit
    1.3 MB from a hotkey-autorepeat storm).
  * crash handlers make failures visible: unhandled exceptions in the main
    thread, any worker thread, and hard faults (faulthandler) all reach the
    log; an optional notifier surfaces them in the UI/tray.
"""

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading

_LOG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(_LOG_DIR, "debug.log")
CRASH_LOG_PATH = os.path.join(_LOG_DIR, "crash.log")

_logger = None
_debug_enabled = False
_notify_cb = None
_crash_file = None


def _get_logger():
    global _logger
    if _logger is None:
        _logger = logging.getLogger("voiceassistant")
        _logger.setLevel(logging.DEBUG)
        _logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname).1s %(message)s",
                              datefmt="%m-%d %H:%M:%S")
        )
        _logger.addHandler(handler)
    return _logger


def set_debug(enabled):
    """Toggle opt-in debug logging (config: debug_logging)."""
    global _debug_enabled
    _debug_enabled = bool(enabled)


def is_debug():
    return _debug_enabled


def dbg(msg):
    """Debug trace — written only when debug logging is enabled."""
    if _debug_enabled:
        try:
            _get_logger().debug(msg)
        except Exception:
            pass


def info(msg):
    try:
        _get_logger().info(msg)
    except Exception:
        pass


def error(msg):
    """Always recorded, debug flag or not."""
    try:
        _get_logger().error(msg)
    except Exception:
        pass


def exception(context):
    """Log the current exception with traceback. Always recorded."""
    try:
        _get_logger().exception(context)
    except Exception:
        pass


def set_notifier(cb):
    """Register a UI-safe callable(str) used to surface crash notices.

    The callable must be safe to invoke from ANY thread (e.g. emit a Qt
    signal); crash handlers run wherever the crash happened.
    """
    global _notify_cb
    _notify_cb = cb


def _notify(msg):
    if _notify_cb is not None:
        try:
            _notify_cb(msg)
        except Exception:
            pass


def install_crash_handlers():
    """Route every unhandled failure into the log (and the notifier).

    pythonw.exe discards stderr, so without this a crash simply vanishes —
    the app disappears and a worker exception silently kills its feature.
    """
    global _crash_file

    def _sys_hook(exc_type, exc, tb):
        try:
            _get_logger().critical(
                "UNHANDLED EXCEPTION (main thread)",
                exc_info=(exc_type, exc, tb),
            )
        except Exception:
            pass
        _notify(f"Voice Assistant error: {exc_type.__name__}: {exc}")

    def _thread_hook(args):
        try:
            _get_logger().critical(
                f"UNHANDLED EXCEPTION (thread {args.thread.name if args.thread else '?'})",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            pass
        _notify(f"Voice Assistant background error: {args.exc_type.__name__}")

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook

    # Hard faults (access violations, deadlocked C extensions on fatal
    # signals) — faulthandler needs a real file handle kept open.
    try:
        _crash_file = open(CRASH_LOG_PATH, "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file)
    except Exception:
        pass
