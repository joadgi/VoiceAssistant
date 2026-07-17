"""Voice Assistant — entry shim.

The application lives in the `voiceassistant` package (see its __init__ for
the module map). This shim exists so run.bat, the desktop shortcut, and the
start-with-Windows registry entry keep working unchanged.
"""

from voiceassistant.app import main

if __name__ == "__main__":
    main()
