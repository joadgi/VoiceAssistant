"""VoiceRecorder — ALWAYS-OPEN microphone capture with a pre-roll ring buffer.

WHY THIS SHAPE (the reliability fix, 2026-08-17):
The old design opened a fresh PortAudio InputStream on every hotkey press.
Measured on this machine (Yeti / MME): **117-137 ms elapsed between
`start()` and the first audio sample actually arriving** — every single
dictation silently lost its first ~1/8 second. People start speaking as they
press, so that is the leading edge of the first word, every time. Short
utterances ("yes", "done") lost so much that what remained fell under the
`min_record_seconds` floor and were dropped outright with no visible feedback.
That is the whole "it doesn't hear me / I have to repeat myself" complaint.

Now: ONE stream is opened at launch and stays open for the life of the app.
The audio callback continuously writes into a fixed ring buffer. "Start
recording" is just marking a read offset — measured at **~2 microseconds**,
and because the ring already holds the recent past we begin the capture
`preroll_ms` BEFORE the keypress. The first word cannot be clipped; the user
can even start talking slightly early and still be heard.

Threading: sounddevice owns the callback thread. That callback does the
minimum possible work — downmix, ring write, peak track — and emits NO Qt
signals (the old per-block `level_update.emit()` from the audio thread was a
contributor to the logged `audio input overflow x29`). All Qt signalling and
all policy (level metering, max-duration cap, mic-death detection) happens on
a GUI-thread QTimer that reads plain fields.
"""

import atexit
import weakref

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, QTimer, Signal

SAMPLE_RATE = 16000  # Whisper's fixed input rate

# Ring headroom over the longest allowed recording, so a capture can never
# lap its own start offset.
_RING_HEADROOM_S = 12.0

# How long after the hotkey release we keep reading the ring, so the final
# syllable (still in flight through PortAudio when the key came up) is
# included. The old code called stream.stop() immediately and threw it away.
_TAIL_MS = 160

# Poll rate for level metering, the duration cap, and mic-health checks.
_TICK_MS = 50

# No new audio for this long while the stream claims to be open means the
# device is gone (USB mic unplugged, driver reset, device switched away).
_STALL_SECONDS = 2.0

# Ticks to wait between reopen attempts while the device is unavailable. Without
# this, a machine with no mic retries the open every tick (20x/second), spinning
# PortAudio and writing an identical error line to debug.log 20x/second.
_REOPEN_COOLDOWN_TICKS = int(2.0 / (_TICK_MS / 1000.0))  # ~2s


def _close_stream_at_exit(ref):
    """atexit hook — stop PortAudio at interpreter exit without pinning the
    recorder (see the atexit.register call for why this takes a weakref)."""
    rec = ref()
    if rec is None:
        return
    try:
        rec._close_stream_only()
    except Exception:
        pass


class VoiceRecorder(QObject):
    """Continuous microphone capture; recordings are slices of a ring buffer."""

    recording_started = Signal()
    recording_stopped = Signal(np.ndarray)  # emits the audio array
    level_update = Signal(float)  # emits RMS level for a VU meter
    max_duration_reached = Signal()  # safety cap hit — GUI should stop us
    error = Signal(str)
    # Mic availability changed: (alive, human-readable reason). Lets the UI
    # show a dead mic INSTEAD of silently recording nothing.
    stream_state = Signal(bool, str)

    def __init__(self, sample_rate=16000, device=None, max_seconds=120.0,
                 preroll_ms=300):
        super().__init__()
        # Whisper requires 16 kHz mono. Capturing at any other rate would feed
        # mis-timed audio to Whisper AND desync every duration/gate calc that
        # divides sample counts by the rate — so pin it, and say so if a caller
        # asks for something else rather than silently honoring a broken rate.
        if sample_rate != SAMPLE_RATE:
            from . import applog
            applog.error(
                f"VoiceRecorder: ignoring unsupported rate {sample_rate}; "
                f"Whisper needs {SAMPLE_RATE}"
            )
        self.sample_rate = SAMPLE_RATE
        self.device = device  # None = system default, int = device index
        self.max_seconds = max_seconds  # 0/None = no cap
        self.preroll_ms = preroll_ms

        self._is_recording = False
        self._stream = None
        self._stream_channels = 1

        ring_seconds = (self.max_seconds or 30.0) + _RING_HEADROOM_S
        self._ring = np.zeros(int(ring_seconds * SAMPLE_RATE), dtype="float32")
        # Monotonic count of frames ever written. Written ONLY by the audio
        # callback, read by the GUI thread. Never wraps (Python int).
        self._frames_written = 0
        self._capture_start = 0  # absolute frame index this recording began at
        self._overflow_count = 0
        self._last_peak = 0.0
        self._max_hit = False
        self._alive = False
        self._stall_ticks = 0
        self._last_seen_frames = 0
        self._pending_stop = False
        self._open_failed = False   # have we already reported this outage?
        self._reopen_wait = 0       # ticks remaining before the next retry

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        # Owned (parented) one-shot for the tail drain. Deliberately NOT
        # QTimer.singleShot: a free-floating single-shot can outlive this
        # object and fire into a deleted QObject.
        self._tail_timer = QTimer(self)
        self._tail_timer.setSingleShot(True)
        self._tail_timer.setInterval(_TAIL_MS)
        self._tail_timer.timeout.connect(self._finish_capture)

        # The stream holds a reference to our bound callback and we hold the
        # stream, so recorder<->stream is a reference CYCLE. When the cycle
        # collector reclaims it, PortAudio's thread can still be inside
        # _audio_callback writing into self._ring — a hard segfault (confirmed
        # while running the suite). Guarantee the stream is stopped before the
        # buffer can be reclaimed, from both the GC path (__del__) and
        # interpreter exit (here).
        #
        # Registered through a WEAK reference on purpose: atexit holds its
        # arguments forever, so registering a bound method would pin every
        # recorder for the life of the process and __del__ would never run —
        # defeating the very guarantee this pairs with.
        atexit.register(_close_stream_at_exit, weakref.ref(self))

    # ------------------------------------------------------------------ #
    # Stream lifecycle (opened once at launch, kept open)
    # ------------------------------------------------------------------ #
    @property
    def is_recording(self):
        return self._is_recording

    @property
    def is_alive(self):
        """True when the mic stream is open and delivering audio."""
        return self._alive

    def open_stream(self):
        """Open the always-on capture stream. Safe to call repeatedly."""
        if self._stream is not None:
            return True
        from . import applog

        try:
            dev = self.device if self.device is not None and self.device >= 0 else None
            # Capture the device's own channel count (up to stereo) and downmix
            # ourselves. Asking MME for 1 channel on a stereo mic (the Yeti is
            # stereo) hands back a single capsule, which halves the signal and
            # pushed quiet speech under the silent-drop peak gate.
            channels = 1
            try:
                info = sd.query_devices(dev if dev is not None else sd.default.device[0])
                channels = max(1, min(2, int(info["max_input_channels"])))
            except Exception:
                channels = 1

            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=channels,
                dtype="float32",
                blocksize=1024,
                device=dev,
                callback=self._audio_callback,
            )
            self._stream.start()
            self._stream_channels = channels
            self._alive = True
            self._stall_ticks = 0
            self._open_failed = False
            self._reopen_wait = 0
            self._last_seen_frames = self._frames_written
            self._tick.start()
            applog.info(
                f"mic stream open (device={dev}, channels={channels}, "
                f"preroll={self.preroll_ms}ms)"
            )
            self.stream_state.emit(True, "Microphone ready")
            return True
        except Exception as e:
            self._stream = None
            self._alive = False
            # Report the failure ONCE per outage, not once per retry: with no
            # mic present the tick fires 20x/second, which would spin PortAudio
            # and flood debug.log with an identical error line 20x/second.
            if not self._open_failed:
                applog.error(f"mic stream open failed: {e}")
                self.stream_state.emit(False, f"Microphone unavailable: {e}")
            self._open_failed = True
            self._reopen_wait = _REOPEN_COOLDOWN_TICKS
            # Keep ticking so _on_tick can retry the open (throttled).
            self._tick.start()
            return False

    def __del__(self):
        # PEP 442 runs finalizers before reclaiming cycle members, so stopping
        # the stream here is what keeps the audio thread out of a freed ring.
        # Touch nothing Qt-related: this may run after the QObject is gone.
        try:
            self._close_stream_only()
        except Exception:
            pass

    def _close_stream_only(self):
        """Stop+close the PortAudio stream. No Qt, no signals — safe from
        __del__ and atexit."""
        stream, self._stream = getattr(self, "_stream", None), None
        self._alive = False
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def close_stream(self):
        """Tear the stream down (app exit / device change)."""
        self._tick.stop()
        self._tail_timer.stop()
        self._is_recording = False
        self._pending_stop = False
        stream, self._stream = self._stream, None
        self._alive = False
        if stream is None:
            return
        # Guarded teardown: PortAudio raises here if the device vanished
        # (USB mic unplugged). close() must run even when stop() raised.
        err = None
        try:
            stream.stop()
        except Exception as e:
            err = e
        try:
            stream.close()
        except Exception as e:
            err = err or e
        if err is not None:
            from . import applog
            applog.error(f"mic stream close error: {err}")

    def set_device(self, device):
        """Switch input device — reopens the always-on stream."""
        if device == self.device:
            return
        self.device = device
        was_recording = self._is_recording
        self.close_stream()
        self.open_stream()
        if was_recording:
            # A device swap mid-recording invalidates the capture; end it
            # cleanly rather than emitting a half-device Frankenstein buffer.
            self._is_recording = False
            self.recording_stopped.emit(np.array([], dtype="float32"))

    # ------------------------------------------------------------------ #
    # Recording = marking offsets into the always-running ring
    # ------------------------------------------------------------------ #
    def start(self):
        """Begin a capture. O(1) — no device open, no audio lost."""
        if self._is_recording:
            return
        if self._stream is None or not self._alive:
            # Last-chance open so a dead mic doesn't silently no-op the hotkey.
            if not self.open_stream():
                self.error.emit("Microphone unavailable — check it is plugged in and not in use")
                return

        preroll = int((self.preroll_ms / 1000.0) * SAMPLE_RATE)
        usable = len(self._ring) - SAMPLE_RATE  # keep a margin from the writer
        preroll = max(0, min(preroll, usable))
        self._capture_start = max(0, self._frames_written - preroll)
        self._overflow_count = 0
        self._max_hit = False
        self._pending_stop = False
        self._is_recording = True
        self.recording_started.emit()

    def stop(self):
        """End the capture, after a short drain so the last syllable survives.

        The drain is a timer, not a sleep — the GUI thread never blocks.
        """
        if not self._is_recording or self._pending_stop:
            return
        self._pending_stop = True
        self._tail_timer.start()

    def _finish_capture(self):
        if not self._is_recording:
            return
        self._is_recording = False
        self._pending_stop = False
        end = self._frames_written
        audio = self._read_ring(self._capture_start, end)

        if self._overflow_count:
            from . import applog
            applog.error(
                f"audio input overflow x{self._overflow_count} during recording "
                "(dropped samples — system under load?)"
            )
        self.recording_stopped.emit(audio)

    def _read_ring(self, start, end):
        """Copy absolute frame range [start, end) out of the ring."""
        n = end - start
        if n <= 0:
            return np.array([], dtype="float32")
        size = len(self._ring)
        if n > size:
            # Should be impossible (the duration cap fires first), but never
            # return interleaved garbage — keep the most recent `size` frames.
            start = end - size
            n = size
        a = start % size
        b = a + n
        if b <= size:
            return self._ring[a:b].copy()
        first = size - a
        out = np.empty(n, dtype="float32")
        out[:first] = self._ring[a:]
        out[first:] = self._ring[: n - first]
        return out

    # ------------------------------------------------------------------ #
    # Audio thread: do as little as possible, emit nothing
    # ------------------------------------------------------------------ #
    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            # Input overflow = dropped samples (gappy audio). Count here
            # (audio-rate callback — never log per-block), surface at stop.
            self._overflow_count += 1

        mono = indata[:, 0] if self._stream_channels == 1 else indata.mean(axis=1)

        size = len(self._ring)
        a = self._frames_written % size
        n = len(mono)
        b = a + n
        if b <= size:
            self._ring[a:b] = mono
        else:
            first = size - a
            self._ring[a:] = mono[:first]
            self._ring[: n - first] = mono[first:]
        self._frames_written += n

        # Cheap peak for the meter; the GUI timer reads this field.
        if n:
            self._last_peak = float(np.abs(mono).max())

    # ------------------------------------------------------------------ #
    # GUI thread: metering, duration cap, mic-health watchdog
    # ------------------------------------------------------------------ #
    def _on_tick(self):
        # --- mic health / auto-recovery (throttled: see _REOPEN_COOLDOWN_TICKS) ---
        if self._stream is None:
            if self._reopen_wait > 0:
                self._reopen_wait -= 1
                return
            self.open_stream()
            return
        advanced = self._frames_written != self._last_seen_frames
        self._last_seen_frames = self._frames_written
        if advanced:
            self._stall_ticks = 0
            if not self._alive:
                self._alive = True
                self.stream_state.emit(True, "Microphone ready")
        else:
            self._stall_ticks += 1
            if self._stall_ticks * (_TICK_MS / 1000.0) >= _STALL_SECONDS:
                # The device stopped delivering. Recording into a dead stream
                # is the worst failure mode there is — silence that looks fine.
                from . import applog
                applog.error("mic stream stalled — reopening")
                self._stall_ticks = 0
                if self._alive:
                    self._alive = False
                    self.stream_state.emit(False, "Microphone stopped responding — reconnecting…")
                was_recording = self._is_recording
                self.close_stream()
                self._tick.start()
                self.open_stream()
                if was_recording:
                    self._is_recording = False
                    self.error.emit("Microphone dropped out mid-recording")
                return

        # --- level meter (emitted from the GUI thread, not the audio thread) ---
        self.level_update.emit(self._last_peak if self._is_recording else 0.0)

        # --- safety cap on a forgotten/stuck hold ---
        if (self._is_recording and not self._max_hit and self.max_seconds
                and (self._frames_written - self._capture_start)
                >= self.max_seconds * SAMPLE_RATE):
            self._max_hit = True
            self.max_duration_reached.emit()
