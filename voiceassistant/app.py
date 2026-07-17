"""Application bootstrap: crash visibility first, then single-instance, then Qt."""

import os
import sys

from PySide6.QtWidgets import QApplication

from . import applog, winapi
from .config import CONFIG_DIR
from .theme import DARK_STYLE

# run.bat / the startup registry entry launch the root main.py shim.
ENTRY_SCRIPT = os.path.join(CONFIG_DIR, "main.py")


def main():
    # Self-diagnostic — fast, no GUI, no single-instance lock. Run this on a
    # new machine (or after a restart) to confirm every component is present.
    if "--check" in sys.argv:
        from .selfcheck import run_selfcheck

        sys.exit(run_selfcheck(deep="--deep" in sys.argv))

    # Crash handlers FIRST — pythonw discards stderr, so anything before this
    # point failing is invisible. After this line, every unhandled exception
    # (main thread, any worker) lands in debug.log + crash.log.
    applog.install_crash_handlers()

    if not winapi.acquire_single_instance_lock():
        return

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Voice Assistant")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)

    from .window import MainWindow  # deferred: heavy imports after the mutex

    window = MainWindow(entry_script=ENTRY_SCRIPT)
    if "--minimized" in sys.argv or window.config.get("start_minimized", True):
        window.hide()
    else:
        window.show()

    sys.exit(app.exec())
