"""ALL Win32/ctypes calls live in this module — nothing else touches ctypes.

Keeping the platform surface in one file makes every other module mockable
and gives Win32 changes exactly one place to break.
"""

import ctypes
import os
import sys
import time

from . import applog

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_C = 0x43
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


# ---------------------------------------------------------------------------
# Foreground window
# ---------------------------------------------------------------------------
def get_foreground_window():
    """Return the HWND of the currently focused window."""
    return user32.GetForegroundWindow()


def is_window(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def set_foreground_window(hwnd):
    """Bring a window to front. Uses AttachThreadInput trick for reliability."""
    if not is_window(hwnd):
        return False
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    if current_thread != target_thread:
        user32.AttachThreadInput(current_thread, target_thread, True)
    user32.SetForegroundWindow(hwnd)
    if current_thread != target_thread:
        user32.AttachThreadInput(current_thread, target_thread, False)
    return True


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
