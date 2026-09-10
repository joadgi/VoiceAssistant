"""ALL Win32/ctypes calls live in this module â€” nothing else touches ctypes.

Keeping the platform surface in one file makes every other module mockable
and gives Win32 changes exactly one place to break.
"""

import ctypes
import os
import sys
import time
import threading
from ctypes import wintypes

from . import applog

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Declare handle-returning functions as pointer-width. Without this, ctypes
# defaults their return to C int and SIGN-TRUNCATES HWNDs to 32 bits on 64-bit
# Windows â€” so a handle from GetForegroundWindow() could never compare equal to
# a full-width handle from Qt's winId(), silently breaking the is-own-window /
# focus checks. (Found by the live paste test.)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]

VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_C = 0x43
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


# ---------------------------------------------------------------------------
# Foreground window
# ---------------------------------------------------------------------------
def get_foreground_window():
    """Return the HWND (int) of the currently focused window, or 0."""
    return int(user32.GetForegroundWindow() or 0)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    """Cursor position in PHYSICAL screen pixels.

    Qt6 makes the process per-monitor DPI aware, so GetCursorPos returns true
    physical coordinates â€” the same space mss captures in. Qt's own
    QCursor.pos() is in LOGICAL (scaled) coordinates and was the reason OCR
    grabbed the wrong region on 125%/150% displays.
    """
    pt = _POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# Window classes that treat Ctrl+C as INTERRUPT rather than copy. Sending our
# synthetic Ctrl+C into one of these kills whatever command is running â€” a real
# data-loss hazard, and read-aloud used to do it unconditionally.
CONSOLE_WINDOW_CLASSES = {
    "consolewindowclass",              # conhost (cmd, classic PowerShell)
    "cascadia_hosting_window_class",   # Windows Terminal
    "virtualconsoleclass",             # ConEmu / Cmder
    "mintty",                          # Git Bash / MSYS2
    "putty",
    "vte",
}


def get_window_class(hwnd):
    """Class name of a window, or "" if it cannot be read."""
    if not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(int(hwnd), buf, 256):
            return buf.value
    except Exception:
        pass
    return ""


def is_console_window(hwnd):
    """True when sending Ctrl+C to this window would interrupt a program."""
    cls = get_window_class(hwnd).lower()
    if not cls:
        return False
    return cls in CONSOLE_WINDOW_CLASSES or "console" in cls or "terminal" in cls


def is_window(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def set_foreground_window(hwnd):
    """Bring a window to front and VERIFY it actually took.

    Windows enforces a foreground lock: SetForegroundWindow can be silently
    refused (returns without effect) when the calling process isn't the
    current foreground process. If we don't verify, the caller believes it
    refocused the target and pastes Ctrl+V into whatever window is REALLY in
    front â€” silently mis-delivering the user's dictation. So we confirm
    GetForegroundWindow() == hwnd (briefly polling for the async switch) and
    return False if the refocus did not take; the paste path treats False as
    "leave the text on the clipboard + panel" rather than blindly pasting.
    """
    if not is_window(hwnd):
        return False
    hwnd = int(hwnd)
    if get_foreground_window() == hwnd:
        return True
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached = current_thread != target_thread
    if attached:
        user32.AttachThreadInput(current_thread, target_thread, True)
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)
    # The switch is asynchronous â€” poll briefly for it to actually take.
    for _ in range(15):
        if get_foreground_window() == hwnd:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Synthetic keystrokes
# ---------------------------------------------------------------------------
# Shared by clipboard copy and paste workers. Hold it for the complete
# clipboard transaction, not just the individual keystroke sequence.
clipboard_input_lock = threading.RLock()


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

_MODIFIER_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)


def modifiers_down():
    """Windows' accepted input state, not keyboard's cached hook events."""
    return tuple(vk for vk in _MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000)


def wait_for_modifiers_released(timeout=2.0):
    deadline = time.monotonic() + timeout
    while modifiers_down():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def hotkey_is_down(combo):
    """Independent state for NON-suppressed shortcuts; never use for blocked Caps."""
    import keyboard
    direct = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10,
              "left ctrl": 0xA2, "right ctrl": 0xA3,
              "left alt": 0xA4, "right alt": 0xA5,
              "left shift": 0xA0, "right shift": 0xA1}
    for part in combo.split("+"):
        if part in ("windows", "cmd", "meta"):
            vks = (0x5B, 0x5C)
        elif part in direct:
            vks = (direct[part],)
        else:
            vks = tuple(user32.MapVirtualKeyW(code, 3)
                        for code in keyboard.key_to_scan_codes(part))
        if not any(vk and user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks):
            return False
    return True


def _input_batch(events):
    inputs = (_INPUT * len(events))()
    for index, (vk, up) in enumerate(events):
        inputs[index].type = 1  # INPUT_KEYBOARD
        inputs[index].ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0,
                                       0, 0x56414B42)
    return int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT)))


def _send_ctrl_shortcut(key):
    with clipboard_input_lock:
        # Recheck immediately before the indivisible batch, even if the caller
        # already waited. Never force-release a modifier the user is holding.
        if modifiers_down():
            applog.info("keyboard input deferred: a modifier is still held")
            return False
        inserted = None
        attempted = False
        try:
            attempted = True
            inserted = _input_batch(((VK_CONTROL, False), (key, False),
                                     (key, True), (VK_CONTROL, True)))
            if inserted != 4:
                applog.error(f"keyboard input incomplete: {inserted}/4 events")
                return False
            return True
        except Exception:
            applog.exception("keyboard input failed")
            return False
        finally:
            # A failed or interrupted batch may have inserted Ctrl-down. Send
            # only releases, never retry the action and risk duplicate pasting.
            # Zero accepted events own no keys: do not release user input.
            if attempted and (inserted is None or 0 < inserted < 4):
                try:
                    released = _input_batch(((key, True), (VK_CONTROL, True)))
                    if released != 2:
                        applog.error("keyboard input cleanup was not accepted")
                except Exception:
                    applog.exception("keyboard input cleanup failed")


def send_ctrl_v():
    return _send_ctrl_shortcut(VK_V)


def send_ctrl_c():
    return _send_ctrl_shortcut(VK_C)


def send_escape():
    """Tap Escape (used ONLY to dismiss the Start menu after a Windows-key
    hotkey â€” never inject Escape into an ordinary target window)."""
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)


# ---------------------------------------------------------------------------
# Single instance + show-window handshake
# ---------------------------------------------------------------------------
_MUTEX_NAME = r"Local\VoiceAssistant.MainInstance"
_EVENT_NAME = r"Local\VoiceAssistant.ShowWindow"
_single_instance_mutex = None
_show_window_event = None


def request_existing_instance_show():
    """Ask the already-running app to show its main window."""
    EVENT_MODIFY_STATE = 0x0002
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, _EVENT_NAME)
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)
        return True
    return False


def create_show_window_event():
    global _show_window_event
    _show_window_event = kernel32.CreateEventW(None, False, False, _EVENT_NAME)
    return _show_window_event


def acquire_single_instance_lock():
    """Prevent two app copies from registering the same global hotkeys."""
    global _single_instance_mutex
    ERROR_ALREADY_EXISTS = 183
    mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not mutex:
        return True
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        request_existing_instance_show()
        applog.info("second instance detected; requested existing window show")
        kernel32.CloseHandle(mutex)
        return False
    _single_instance_mutex = mutex
    create_show_window_event()
    return True


def show_requested():
    """True once when another launch has signaled the show-window event."""
    if not _show_window_event:
        return False
    WAIT_OBJECT_0 = 0
    return kernel32.WaitForSingleObject(_show_window_event, 0) == WAIT_OBJECT_0


def release_single_instance_lock():
    global _single_instance_mutex, _show_window_event
    for handle_name in ("_show_window_event", "_single_instance_mutex"):
        handle = globals()[handle_name]
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
            globals()[handle_name] = None


# ---------------------------------------------------------------------------
# Start with Windows
# ---------------------------------------------------------------------------
def set_start_with_windows(enabled, entry_script):
    """Register or remove the tray-first startup command for this user."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                pythonw = sys.executable
                if pythonw.lower().endswith("python.exe"):
                    pythonw = pythonw[:-10] + "pythonw.exe"
                command = f'"{pythonw}" "{os.path.abspath(entry_script)}" --minimized'
                winreg.SetValueEx(key, "VoiceAssistant", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "VoiceAssistant")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        applog.error(f"startup registration failed: {e}")
        return False
