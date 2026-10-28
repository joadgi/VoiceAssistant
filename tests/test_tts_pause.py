"""Pause/resume for read-aloud.

Pinned here, all measured live on 2026-10-28 before the feature was built:

  * A read pauses only once its audio is playing, through the backend's OWN
    pause (VLC set_pause, SAPI SpVoice.Pause), and Stop still works from a
    paused read without leaving the shared SAPI voice paused.
  * LibVLC never reports Ended for a raw callback stream that was really
    paused (12 of 12 runs), so a paused read ends when its stream is
    exhausted and VLC's media time stops moving -- and ONLY then: not while
    paused, not before the last byte, and never for a read that was not paused.
  * Paused time is not playing time: it must not be reported as synthesis
    falling behind, nor count against the SAPI driver's time budget.

No audio, no real VLC, no real SAPI.
"""

import os
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voiceassistant.tts as tts
from voiceassistant.tts import TTSEngine, _UtteranceStop


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_vlc(monkeypatch):
    fake = types.ModuleType("vlc")
    fake.State = types.SimpleNamespace(Error="error", Ended="ended", Stopped="stopped")
    fake.cb = types.SimpleNamespace(MediaReadCb=lambda fn: fn)
    monkeypatch.setitem(sys.modules, "vlc", fake)
    return fake


class Player:
    """MediaPlayer stand-in: records pause calls, reports a scripted state and
    a media time that advances `step_ms` per poll until `end_ms`."""

    def __init__(self, end_ms=6000, step_ms=250, refuse=False):
        self.calls = []
        self.state = "playing"
        self.media_ms = 0
        self.end_ms = end_ms
        self.step_ms = step_ms
        self.refuse = refuse
        self.paused = False

    def set_pause(self, on):
        if self.refuse:
            raise RuntimeError("player refused")
        self.calls.append(("set_pause", on))
        self.paused = bool(on)

    def stop(self):
        self.calls.append(("stop",))
        self.state = "stopped"

    def get_state(self):
        return self.state

    def get_time(self):
        if not self.paused:
            self.media_ms = min(self.end_ms, self.media_ms + self.step_ms)
        return self.media_ms


class Voice:
    """SAPI SpVoice stand-in that shares a call log with the pyttsx3 engine."""

    def __init__(self, log):
        self.log = log

    def Pause(self):
        self.log.append("Pause")

    def Resume(self):
        self.log.append("Resume")


def _sapi_engine(log):
    voice = Voice(log)
    return types.SimpleNamespace(
        proxy=types.SimpleNamespace(_driver=types.SimpleNamespace(_tts=voice)),
        stop=lambda: log.append("purge"),
    )


def _engine(player=None):
    eng = TTSEngine()
    eng._vlc_player = player or Player()
    return eng


def _live_read(eng, backend="vlc"):
    """Put `eng` in the state of a read whose audio has started playing."""
    stop = _UtteranceStop()
    eng._gen += 1
    eng._active_stop = stop
    eng._speaking = True
    eng._go_live(stop, backend)
    return stop


class Stream:
    def __init__(self, eof=True):
        self.eof_delivered = eof
        self.error = None


def _wait_in_thread(eng, stream, stop):
    t = threading.Thread(target=eng._wait_vlc_stream, args=(stream, stop), daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- #
# Pause / resume / stop
# --------------------------------------------------------------------------- #
def test_a_read_pauses_only_once_its_audio_is_playing(fake_vlc):
    eng = _engine()
    started, changes = [], []
    eng.playback_started.connect(lambda: started.append(1))
    eng.paused_changed.connect(changes.append)

    stop = _UtteranceStop()
    eng._active_stop, eng._speaking = stop, True
    assert eng.pause() is False, "paused during the lead-in, before any audio"
    assert eng._vlc_player.calls == []
    assert not eng.can_pause

    eng._go_live(stop, "vlc")
    assert started == [1]
    assert eng.can_pause
    assert eng.pause() is True
    assert eng.is_paused
    assert eng.pause() is False, "a second pause must not stack"
    assert eng.resume() is True
    assert not eng.is_paused
    assert eng.toggle_pause() is True and eng.is_paused
    assert eng.toggle_pause() is True and not eng.is_paused
    assert eng._vlc_player.calls == [("set_pause", 1), ("set_pause", 0)] * 2
    assert changes == [True, False, True, False]
    assert stop.pause.was_paused


def test_stop_from_a_paused_read_stops_and_releases_the_pause(fake_vlc):
    eng = _engine()
    changes = []
    eng.paused_changed.connect(changes.append)
    stop = _live_read(eng)
    eng.pause()
    eng.stop()
    assert stop.is_set()
    assert ("stop",) in eng._vlc_player.calls
    assert not eng.is_paused and not stop.pause.paused
    assert changes == [True, False]
    assert eng.pause() is False, "a stopped read cannot be paused again"


def test_stopping_a_paused_sapi_read_never_leaves_the_voice_paused(fake_vlc):
    """SAPI keeps a pause COUNT on the one shared voice: purge first (so the
    resume cannot let the rest of the text out), then resume."""
    log = []
    eng = _engine()
    eng._pyttsx_engine = _sapi_engine(log)
    _live_read(eng, "sapi")
    assert eng.pause() is True
    eng.stop()
    assert log == ["Pause", "purge", "Resume"]


def test_a_read_that_ends_while_paused_is_released_too(fake_vlc):
    """Any way a job ends (an error, a dead device) must not leave it paused."""
    log = []
    eng = _engine()
    eng._pyttsx_engine = _sapi_engine(log)
    stop = _live_read(eng, "sapi")
    eng.pause()
    eng._release_pause(stop)
    assert log == ["Pause", "Resume"]
    assert not stop.pause.paused


def test_a_backend_that_refuses_leaves_the_read_playing(fake_vlc):
    eng = _engine(Player(refuse=True))
    changes = []
    eng.paused_changed.connect(changes.append)
    stop = _live_read(eng)
    assert eng.pause() is False
    assert not eng.is_paused and not stop.pause.was_paused
    assert changes == []


def test_sapi_without_a_reachable_voice_does_not_pretend_to_pause(fake_vlc):
    eng = _engine()
    eng._pyttsx_engine = types.SimpleNamespace(stop=lambda: None)
    _live_read(eng, "sapi")
    assert eng.pause() is False
    assert not eng.is_paused


# --------------------------------------------------------------------------- #
# The end of a paused read (VLC never says Ended for it)
# --------------------------------------------------------------------------- #
@pytest.fixture
def quick_stall(monkeypatch):
    monkeypatch.setattr(TTSEngine, "PAUSED_END_STALL_S", 0.15)


def test_a_paused_read_ends_when_its_stream_runs_out(fake_vlc, quick_stall):
    """The live bug: without this the read stayed 'speaking' forever."""
    eng = _engine(Player(end_ms=1000))
    stop = _live_read(eng)
    stop.pause.pause()
    stop.pause.resume()
    t = _wait_in_thread(eng, Stream(eof=True), stop)
    t.join(3)
    assert not t.is_alive(), "a paused read never finished"
    assert eng._vlc_player.media_ms == 1000, "ended before the audio did"
    assert ("stop",) in eng._vlc_player.calls


def test_a_paused_read_never_ends_while_it_is_still_paused(fake_vlc, quick_stall):
    eng = _engine(Player(end_ms=1000))
    stop = _live_read(eng)
    eng.pause()
    t = _wait_in_thread(eng, Stream(eof=True), stop)
    t.join(0.6)
    assert t.is_alive(), "a paused read was declared finished"
    stop.set()
    t.join(2)
    assert not t.is_alive()


def test_still_media_time_before_the_last_byte_is_not_the_end(fake_vlc, quick_stall):
    """After a resume, synthesis can still be behind: VLC waits, time stands
    still, and that is a stall to sit out, not the end of the read."""
    eng = _engine(Player(end_ms=1000))
    stop = _live_read(eng)
    stop.pause.pause()
    stop.pause.resume()
    t = _wait_in_thread(eng, Stream(eof=False), stop)
    t.join(0.6)
    assert t.is_alive(), "ended before the stream had delivered its last byte"
    stop.set()
    t.join(2)


def test_a_read_that_was_never_paused_still_waits_for_ended(fake_vlc, quick_stall):
    eng = _engine(Player(end_ms=1000))
    stop = _live_read(eng)
    t = _wait_in_thread(eng, Stream(eof=True), stop)
    t.join(0.6)
    assert t.is_alive(), "the paused-read rule leaked into a normal read"
    eng._vlc_player.state = "ended"
    t.join(2)
    assert not t.is_alive()


# --------------------------------------------------------------------------- #
# Paused time is not playing time
# --------------------------------------------------------------------------- #
def _local_read_with_a_hold(monkeypatch, hold_s, paused):
    """Three 1 s blocks generated in 0.3 s each, with `hold_s` passing while
    the second one is generated -- held paused, or (control) not."""
    import numpy as np

    now = [0.0]
    monkeypatch.setattr(
        tts, "time", types.SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: None))
    eng = _engine()
    eng._use_local = True
    stop = _UtteranceStop()
    samples = tts.LOCAL_PCM_SAMPLE_RATE

    class Kokoro:
        def _prepare(self, *args):
            return "style", "phonemes", [(f"p{i}", 0.1) for i in range(3)]

        def _create_batch(self, phonemes, *args):
            now[0] += 0.3
            if phonemes == "p1":
                if paused:
                    stop.pause.pause()
                now[0] += hold_s
                if paused:
                    stop.pause.resume()
            return np.zeros(samples, dtype=np.float32), None

    def start(stream, stop_event, progress):
        progress.append("started")
        eng._go_live(stop_event, "vlc")

    eng._kokoro = Kokoro()
    eng._time_stretch_local_audio = lambda s, factor: s
    eng._start_vlc_pcm_stream = start
    eng._wait_vlc_stream = lambda stream, stop_event: None
    warnings, lines = [], []
    eng.falling_behind.connect(warnings.append)
    monkeypatch.setattr(tts.applog, "dbg", lines.append)
    eng._speak_local("Three phrases.", stop, [])
    return warnings, lines


def test_a_long_pause_is_not_reported_as_the_cpu_falling_behind(monkeypatch):
    warnings, lines = _local_read_with_a_hold(monkeypatch, 30.0, paused=True)
    assert warnings == [], "a pause was blamed on the CPU"
    outcome = [line for line in lines if line.startswith("local neural playback complete")]
    assert len(outcome) == 1 and "paused=30.000s" in outcome[0], lines


def test_the_same_hold_without_a_pause_still_warns(monkeypatch):
    """Control: proves the harness can see a dry spell at all."""
    warnings, _ = _local_read_with_a_hold(monkeypatch, 30.0, paused=False)
    assert len(warnings) == 1


class SlowSapi:
    """pyttsx3 stand-in whose utterance finishes at fake time `finish_at`."""

    def __init__(self, now, finish_at):
        self.now = now
        self.finish_at = finish_at
        self._done = []
        self.spoken = []

    def connect(self, topic, cb):
        self._done.append(cb)
        return cb

    def disconnect(self, token):
        pass

    def say(self, text):
        self._text = text

    def startLoop(self, use_driver_loop):
        pass

    def iterate(self):
        if self.now[0] >= self.finish_at and not self.spoken:
            self.spoken.append(self._text)
            for cb in self._done:
                cb(name=None, completed=True)

    def endLoop(self):
        pass


def _sapi_read_paused_for(monkeypatch, pause_s):
    now = [0.0]

    def sleep(s):
        now[0] += s

    monkeypatch.setattr(
        tts, "time", types.SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    errors = []
    monkeypatch.setattr(tts.applog, "error", errors.append)
    eng = _engine()
    stop = _UtteranceStop()
    stop.pause.backend = "sapi"
    text = "Short."
    budget = eng._offline_chunk_budget(text)
    sapi = SlowSapi(now, finish_at=budget + pause_s - 1.0)
    if pause_s:
        stop.pause.pause()

        def resume_later(original=sapi.iterate):
            if now[0] >= pause_s and stop.pause.paused:
                stop.pause.resume()
            original()

        sapi.iterate = resume_later
    eng._speak_offline_chunk(sapi, text, stop, allow_blocking_fallback=False)
    return sapi, errors


def test_a_paused_sapi_read_is_not_cut_off_by_the_driver_budget(monkeypatch):
    """The budget exists for a wedged driver; a minute held paused is not one."""
    sapi, errors = _sapi_read_paused_for(monkeypatch, 60.0)
    assert sapi.spoken == ["Short."], errors
    assert errors == []
