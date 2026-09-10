"""Read-aloud chord detection without suppressing or replaying modifiers.

The keyboard library's suppressing-hotkey state machine delays Ctrl/Alt and
replays them. Use a small event-driven matcher instead. Only a non-modifier
trigger's matched down/up pair may be consumed; modifiers always pass through.

A hook only ever knows the events it is delivered, so this matcher's own
`_held` set is NOT independent evidence that a key is physically down. One
dropped key-up latches a modifier in it permanently, and `--report` confirms
Windows really does drop key-ups on this machine. Measured consequences, with
the shipped `ctrl+alt` read chord: after a lost Ctrl key-up EVERY later Alt
press fired read-aloud (so Alt+Tab triggered it), and for a chord with a
normal trigger such as `ctrl+shift+t` a plain capital T both fired read-aloud
AND was swallowed before reaching the document. If every combo key latches,
`_latched` can never reset and read-aloud goes silently dead until restart.

So `_held` is reconciled against Windows' accepted state on every event, via
an injected probe. The probe is a constructor argument rather than an import
so this module keeps making no Windows calls of its own and stays hermetically
testable. Reconciliation is limited to MODIFIERS and skips the current
event's own key — see `_reconcile`. Cost is bounded and measured; no
clipboard work, sleeps, or model work run in this handler.
"""


class ReadHotkey:
    def __init__(self, combo, callback, keyboard, released_probe=None):
        """`released_probe(codes) -> set` names which of `codes` Windows
        reports as physically released. Optional: without it the matcher
        behaves exactly as before, on cached hook state alone."""
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
        self._released_probe = released_probe
        self.last_error = None

    def _reconcile(self, current_code):
        """Drop keys from `_held` that Windows says are no longer down.

        Two exclusions, both load-bearing:

        * `current_code` is never checked. A low-level hook runs BEFORE the
          event reaches the rest of the system, so Windows' accepted state can
          still read "up" for the very key being delivered. Checking it would
          drop the key we just received and break every normal chord.
        * The probe reports MODIFIERS only. A key this matcher consumed never
          reaches Windows' accepted state, so that state cannot be used to
          judge it; modifiers are always passed through, so it can. This is
          the same evidence boundary the PTT watchdog observes for suppressed
          Caps Lock.

        `_blocked` is deliberately untouched, which is what keeps a consumed
        down/up pair matched even if reconciliation drops a modifier between
        the two edges.
        """
        if self._released_probe is None:
            return
        candidates = self._held - {current_code}
        if not candidates:
            return
        try:
            released = self._released_probe(candidates)
        except Exception as exc:
            # Never let a probe failure escape into the hook: an exception
            # here would leak a down event whose key-up is consumed. Falling
            # back to cached state is the safe direction.
            self.last_error = str(exc)
            return
        self._held -= released

    def __call__(self, event):
        code = event.scan_code
        down = event.event_type == "down"
        if down:
            self._held.add(code)
        else:
            self._held.discard(code)
        # Reconcile BEFORE deciding whether the chord is active, so a stale
        # modifier can neither fire the action nor consume a keystroke.
        self._reconcile(code)
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
