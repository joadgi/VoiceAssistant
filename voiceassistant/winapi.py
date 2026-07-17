"""ALL Win32/ctypes calls live in this module — nothing else touches ctypes.

Keeping the platform surface in one file makes every other module mockable
and gives Win32 changes exactly one place to break.
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

from . import applog

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Declare handle-returning functions as pointer-width. Without this, ctypes
# defaults their return to C int and SIGN-TRUNCATES HWNDs to 32 bits on 64-bit
# Windows — so a handle from GetForegroundWindow() could never compare equal to
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
    physical coordinates — the same space mss captures in. Qt's own
    QCursor.pos() is in LOGICAL (scaled) coordinates and was the reason OCR
    grabbed the wrong region on 125%/150% displays.
    """
    pt = _POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def is_window(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def set_foreground_window(hwnd):
    """Bring a window to front and VERIFY it actually took.

    Windows enforces a foreground lock: SetForegroundWindow can be silently
    refused (returns without effect) when the calling process isn't the
    current foreground process. If we don't verify, the caller believes it
    refocused the target and pastes Ctrl+V into whatever window is REALLY in
    front — silently mis-delivering the user's dictation. So we confirm
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
    # The switch is asynchronous — poll briefly for it to actually take.
    for _ in range(15):
        if get_foreground_window() == hwnd:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# Synthetic keystrokes
# ---------------------------------------------------------------------------
def send_ctrl_v():
    """Send Ctrl+V via raw Win32 API — no keyboard-library involvement."""
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.015)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.015)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def send_ctrl_c():
    """Send Ctrl+C via raw Win32 API (selection copy for read-aloud)."""
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.015)
    user32.keybd_event(VK_C, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.015)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def send_escape():
    """Tap Escape (used ONLY to dismiss the Start menu after a Windows-key
    hotkey — never inject Escape into an ordinary target window)."""
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
