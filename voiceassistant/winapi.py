"""ALL Win32/ctypes calls live in this module — nothing else touches ctypes.

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
VK_BACK = 0x08
VK_C = 0x43
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


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


# Window classes that treat Ctrl+C as INTERRUPT rather than copy. Sending our
# synthetic Ctrl+C into one of these kills whatever command is running — a real
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


def get_window_app(hwnd):
    """Best-effort executable name for a window, e.g. "chrome.exe".

    WHY: "inline typing failed 3 times" is not actionable; "inline typing
    failed 3 times in chrome.exe" is. Apps with aggressive autocomplete fight
    injected keystrokes, and the only way to answer "which app" later is to
    record it at the time.

    PRIVACY: an executable name is not content. Never put window TITLES here —
    titles routinely contain document names, subject lines and URLs, which is
    exactly the payload class this app refuses to log.

    Falls back to the window class, then "" — never raises.
    """
    if not hwnd:
        return ""
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            # PROCESS_QUERY_LIMITED_INFORMATION: works for most processes
            # without elevation (plain QUERY_INFORMATION does not).
            handle = kernel32.OpenProcess(0x1000, False, pid.value)
            if handle:
                try:
                    size = wintypes.DWORD(260)
                    buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(
                            handle, 0, buf, ctypes.byref(size)):
                        return os.path.basename(buf.value)
                finally:
                    kernel32.CloseHandle(handle)
    except Exception:
        pass
    try:
        return get_window_class(hwnd) or ""
    except Exception:
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


def released_modifier_scan_codes(codes):
    """Of `codes`, the MODIFIER scan codes Windows reports as physically UP.

    Lets a hook reconcile its own cached key state against reality. A hook
    only ever sees the events it is delivered, so one dropped key-up leaves a
    modifier latched in that cache forever — and CLAUDE.md documents both
    causes (Windows drops the low-level hook past LowLevelHooksTimeout; UAC
    and secure-desktop switches eat events outright).

    Deliberately restricted to MODIFIERS, for two independent reasons:

    * A modifier is never suppressed anywhere in this app, so Windows'
      accepted state is unconditionally authoritative for it. A key the app
      CONSUMED never reaches that state, so the same check would wrongly call
      a physically-held key released — the identical evidence boundary the PTT
      watchdog observes for suppressed Caps Lock.
    * MapVirtualKeyW is not trustworthy for every code `keyboard` enumerates:
      measured here, ctrl's 57629 maps to VK_PAUSE (0x13), the Windows key's
      non-extended 91/92 map to 0xF1/0xEA, and scroll lock's 57414 maps to
      VK_CANCEL. Mapping first and then requiring the result to be a known
      modifier VK rejects all of those.

    Fails OPEN: on any error nothing is reported released, so a probe failure
    degrades to the previous cache-only behaviour rather than cutting a
    genuine hold short.
    """
    released = set()
    try:
        for code in codes:
            vk = user32.MapVirtualKeyW(code, 3)  # MAPVK_VSC_TO_VK_EX
            if vk in _MODIFIER_VKS and not (user32.GetAsyncKeyState(vk) & 0x8000):
                released.add(code)
    except Exception:
        return set()
    return released


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


# ---------------------------------------------------------------------------
# Direct text injection (inline typing — see inline_typist.py)
# ---------------------------------------------------------------------------
# Characters per SendInput batch. Each character is a down/up pair, so this is
# 2x events. Bounded so one rejected batch cannot leave a long half-typed run.
_TYPE_CHUNK = 96
# Backspaces per batch, same reasoning.
_BACKSPACE_CHUNK = 32


def _unicode_batch(text):
    """Send `text` as KEYEVENTF_UNICODE events. Returns events accepted.

    Unicode injection carries the CHARACTER, not a key, so it is immune to the
    user's keyboard layout and — critically — needs no modifier held. That is
    why inline typing uses this and not synthesized key presses: a synthesized
    Shift+key would be a modifier the app holds down in the user's session,
    which this app does not do.
    """
    events = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:  # non-BMP: two surrogate events, each its own pair
            code -= 0x10000
            units = (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
        else:
            units = (code,)
        for unit in units:
            events.append((unit, False))
            events.append((unit, True))
    if not events:
        return 0
    inputs = (_INPUT * len(events))()
    for index, (unit, up) in enumerate(events):
        inputs[index].type = 1  # INPUT_KEYBOARD
        inputs[index].ki = _KEYBDINPUT(
            0, unit, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
            0, 0x56414B42)
    return int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT))), len(events)


def _guarded(expected_hwnd):
    """Shared precondition for every injected batch, checked inside the lock.

    Both halves matter. Foreground: text typed into the wrong window is the
    failure this whole feature has to make impossible, and focus can change
    between the caller's check and the batch. Modifiers: a character sent
    while the user holds Ctrl reaches the app as a SHORTCUT, not text.
    """
    if expected_hwnd and get_foreground_window() != expected_hwnd:
        applog.info("text injection skipped: focus left the dictation target")
        return False
    if modifiers_down():
        applog.info("text injection skipped: a modifier is held")
        return False
    return True


def send_text(text, expected_hwnd=None):
    """Type `text` into the focused window as characters.

    Returns `(ok, sent)`. `sent` is how many characters definitely reached the
    window — `None` means UNKNOWN, and an unknown count is the one state the
    caller must never issue a correcting backspace against, because
    backspacing past our own text deletes the user's.

    A refusal (wrong window, modifier held) stops before any event, so it
    reports `(False, 0)`: nothing changed and the accounting still holds. Only
    a batch that Windows accepted in part is unknown.
    """
    if not text:
        return True, 0
    with clipboard_input_lock:
        if not _guarded(expected_hwnd):
            return False, 0
        for start in range(0, len(text), _TYPE_CHUNK):
            chunk = text[start:start + _TYPE_CHUNK]
            try:
                inserted, expected = _unicode_batch(chunk)
            except Exception:
                applog.exception("text injection failed")
                return False, None
            if inserted != expected:
                applog.error(
                    f"text injection incomplete: {inserted}/{expected} events "
                    f"after {start} chars")
                return False, None
            done = start + len(chunk)
            if done < len(text) and not _guarded(expected_hwnd):
                return False, done  # focus moved between batches: exact count
        return True, len(text)


def send_backspaces(count, expected_hwnd=None):
    """Send `count` backspaces to the focused window.

    Returns `(ok, sent)` with the same contract as `send_text`: `sent` is
    None only when a batch was partially accepted. The caller owns the
    accounting — this deletes exactly what it is told to.
    """
    if count <= 0:
        return True, 0
    with clipboard_input_lock:
        if not _guarded(expected_hwnd):
            return False, 0
        sent = 0
        while sent < count:
            n = min(_BACKSPACE_CHUNK, count - sent)
            events = []
            for _ in range(n):
                events.append((VK_BACK, False))
                events.append((VK_BACK, True))
            try:
                inserted = _input_batch(events)
            except Exception:
                applog.exception("backspace injection failed")
                return False, None
            if inserted != len(events):
                applog.error(
                    f"backspace injection incomplete: {inserted}/{len(events)} events")
                return False, None
            sent += n
            if sent < count and not _guarded(expected_hwnd):
                return False, sent
        return True, sent


# ---------------------------------------------------------------------------
# Lock-key state (Caps Lock etc.)
# ---------------------------------------------------------------------------
# The lock keys this app can bind AND swallow. Only Caps Lock is ever cleared
# automatically: Num Lock off breaks the numeric keypad and Scroll Lock is
# harmless, so neither is touched.
VK_CAPITAL = 0x14
_CAPS_SCAN = 0x3A


def lock_key_is_on(vk=VK_CAPITAL):
    """True when the lock key's toggle state is currently ON."""
    try:
        return bool(user32.GetKeyState(vk) & 1)
    except Exception:
        return False


def clear_caps_lock():
    """Turn Caps Lock OFF if it is on. Returns True if it changed.

    WHY THIS EXISTS: binding Caps Lock as push-to-talk means the app SWALLOWS
    the key, so Windows never sees it — which also means the user can no longer
    press it to turn caps off. If caps is ever left on (the app was not running
    when it was pressed, or it crashed, or it was restarted between the key's
    down and up), the user is stuck in capitals with no way out that does not
    involve quitting the app. Measured on this machine after several restarts
    during development: exactly that happened.

    A tap is sent, not a state poke, because the toggle lives in the keyboard
    driver. The caller must only do this while our own hook is NOT suppressing
    the key, or we swallow our own fix.
    """
    if not lock_key_is_on(VK_CAPITAL):
        return False
    try:
        user32.keybd_event(VK_CAPITAL, _CAPS_SCAN, 0, 0)
        user32.keybd_event(VK_CAPITAL, _CAPS_SCAN, KEYEVENTF_KEYUP, 0)
    except Exception:
        applog.exception("could not clear caps lock")
        return False
    applog.info("caps lock was on and has been cleared")
    return True


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
