"""Read-aloud chord detection without suppressing or replaying modifiers.

The keyboard library's suppressing-hotkey state machine delays Ctrl/Alt and
replays them. Use a small event-driven matcher instead. Only a non-modifier
trigger's matched down/up pair may be consumed; modifiers always pass through.
No Windows calls, clipboard work, sleeps, or model work run in this handler.
"""


class ReadHotkey:
    def __init__(self, combo, callback, keyboard):
        self._parts = tuple(
            frozenset(keyboard.key_to_scan_codes(part))
            for part in combo.split("+") if part
        )
        if not self._parts or any(not part for part in self._parts):
            raise ValueError("Read shortcut contains an unmapped key")
        self._modifiers = set()
        for name in ("ctrl", "alt", "shift", "windows", "alt gr"):
            try:
                self._modifiers.update(keyboard.key_to_scan_codes(name))
            except (ValueError, KeyError):
                pass
        self._held = set()
        self._blocked = set()
        self._latched = False
        self._callback = callback
        self.last_error = None

    def __call__(self, event):
        code = event.scan_code
        down = event.event_type == "down"
        if down:
            self._held.add(code)
        else:
            self._held.discard(code)
        active = all(part & self._held for part in self._parts)
        blocked = code in self._blocked
        fire = down and active and not self._latched
        if fire:
            self._latched = True
            if code not in self._modifiers:
                self._blocked.add(code)
                blocked = True
        if not active:
            self._latched = False
        if not down:
            self._blocked.discard(code)
        # Establish the matching key-up decision before calling user code.
        # A Qt Signal.emit() return value must never decide input suppression.
        if fire:
            try:
                self._callback()
            except Exception as exc:
                # Keep the already-decided down/up pairing even if Qt is
                # shutting down. Never let an exception leak a down event
                # while its later key-up is consumed. No I/O in the hook.
                self.last_error = str(exc)
        return True if code in self._modifiers else not blocked
