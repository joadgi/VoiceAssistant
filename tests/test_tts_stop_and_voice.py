"""TTS regression tests for the two 2026-08-26 bugs: Stop that didn't stop,
and the wrong (robotic) voice.

Both traced to the SAPI fallback path:

  * `pyttsx3.runAndWait()` BLOCKS inside the driver loop until the whole text
    has been spoken, so `stop()` was a no-op there — the audio ran to the end
    while the UI already claimed it had stopped.
  * That same path pushed `self._voice_id` into SAPI, which on a neural
    selection is an edge-tts name like "en-US-AndrewNeural". SAPI selects
    nothing for it, so you got whatever Windows defaulted to.

No audio, no network, no real SAPI: a fake engine reproduces pyttsx3's loop
semantics (say queues, startLoop(False)/iterate pump, endLoop stops+purges)
and a fake VLC player reproduces the asynchronous play() warm-up.

Runnable standalone (python tests/test_tts_stop_and_voice.py) or via pytest.
"""

import asyncio
import math
import os
import sys
import time
import types
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeVoice:
    def __init__(self, vid, name, gender, languages):
        self.id = vid
        self.name = name
        self.gender = gender
        self.languages = languages


DAVID = FakeVoice(
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_DAVID_11.0",
    "Microsoft David Desktop - English (United States)", "Male", ["en-US"],
)
ZIRA = FakeVoice(
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0",
    "Microsoft Zira Desktop - English (United States)", "Female", ["en-US"],
)


class FakeSapiEngine:
    """pyttsx3.Engine stand-in with the real loop semantics and guards.

    `iters_to_finish` is how many iterate() calls one utterance takes, which is
    how a test makes speech "long" without making a test slow.
    """

    def __init__(self, iters_to_finish=2, external_loop=True):
        self.iters_to_finish = iters_to_finish
        self.external_loop = external_loop
        self.props = {}
        self.voices = [DAVID, ZIRA]
        self.spoken = []
        self.ran_and_waited = 0
        self.stop_calls = 0
        self.end_loop_calls = 0
        self._inLoop = False           # pyttsx3's own attribute name
        self._connects = {}
        self._pending = None
        self._remaining = 0

    # --- properties -------------------------------------------------- #
    def setProperty(self, key, value):
        self.props[key] = value

    def getProperty(self, key):
        if key == "voices":
            return self.voices
        return self.props.get(key)

    # --- notifications ----------------------------------------------- #
    def connect(self, topic, cb):
        self._connects.setdefault(topic, []).append(cb)
        return {"topic": topic, "cb": cb}

    def disconnect(self, token):
        try:
            self._connects[token["topic"]].remove(token["cb"])
        except (KeyError, ValueError):
            pass

    # --- speaking ------------------------------------------------------ #
    def say(self, text, name=None):
        self._pending = text

    def startLoop(self, useDriverLoop=True):
        if useDriverLoop or not self.external_loop:
            raise RuntimeError("external loop unsupported")
        if self._inLoop:
            raise RuntimeError("run loop already started")
        self._inLoop = True
        self._remaining = self.iters_to_finish

    def iterate(self):
        if not self._inLoop:
            raise RuntimeError("run loop not started")
        self._remaining -= 1
        if self._remaining <= 0:
            self.spoken.append(self._pending)
            for cb in list(self._connects.get("finished-utterance", [])):
                cb(name=None, completed=True)

    def endLoop(self):
        self.end_loop_calls += 1
        if not self._inLoop:
            raise RuntimeError("run loop not started")
        self._inLoop = False

    def stop(self):
        self.stop_calls += 1

    def runAndWait(self):
        self.ran_and_waited += 1
        self.spoken.append(self._pending)


class FakeMedia:
    def __init__(self):
        self.options = []

    def add_option(self, option):
        self.options.append(option)

    def release(self):
        pass


class FakeVlcInstance:
    def media_new(self, path):
        return FakeMedia()

    def media_new_callbacks(self, *args):
        self.callback_args = args
        return FakeMedia()


class FakeVlcPlayer:
    """play() is asynchronous like the real one: is_playing() stays False until
    `becomes_playing_after` warm-up polls (never, by default)."""

    def __init__(self, becomes_playing_after=None):
        self.plays = 0
        self.stops = 0
        self.rate = None
        self._polls = 0
        self._becomes = becomes_playing_after

    def set_media(self, media):
        self.media = media

    def audio_set_volume(self, v):
        pass

    def play(self):
        self.plays += 1

    def is_playing(self):
        self._polls += 1
        return self._becomes is not None and self._polls >= self._becomes

    def stop(self):
        self.stops += 1

    def set_rate(self, rate):
        self.rate = rate

    def get_state(self):
        return "playing"


def _install_fake_vlc():
    fake = types.ModuleType("vlc")

    class State:
        Error = "error"
        Ended = "ended"
        Stopped = "stopped"

    fake.State = State
    fake.cb = types.SimpleNamespace(MediaReadCb=lambda fn: fn)
    sys.modules["vlc"] = fake


def _engine(**kw):
    """A TTSEngine with a fake SAPI engine bolted on — never touches real audio."""
    from voiceassistant.tts import TTSEngine

    eng = TTSEngine()
    eng._pyttsx_engine = FakeSapiEngine(**kw)
    return eng


def _run_job(eng, text, stop_event=None):
    """Run one utterance synchronously on THIS thread.

    The generation must be advanced first — _speak_job drops any job whose gen
    is not the engine's current one (that is the superseded-while-queued
    guard), so passing a made-up gen silently no-ops the whole test.
    """
    eng._gen += 1
    eng._speak_job(text, eng._gen, stop_event or threading.Event())


def _stop_after(eng, delay):
    t = threading.Timer(delay, eng.stop)
    t.daemon = True
    t.start()
    return t


# --------------------------------------------------------------------------- #
# Bug 1 — Stop must actually stop
# --------------------------------------------------------------------------- #
def test_offline_speech_is_interruptible():
    """THE headline regression: stop() during SAPI speech must cut it fast.

    With runAndWait() this returned only after the whole text had been spoken.
    """
    eng = _engine(iters_to_finish=100_000)  # "long" utterance
    stop_event = threading.Event()
    _stop_after(eng, 0.15)

    eng._active_stop = stop_event
    started = time.monotonic()
    eng._speak_offline("One. Two. Three. Four. Five.", stop_event)
    elapsed = time.monotonic() - started

    assert stop_event.is_set(), "engine.stop() did not set the active stop event"
    assert elapsed < 2.0, (
        f"offline speech ignored stop for {elapsed:.2f}s — "
        "a blocking speak-until-finished call is back"
    )
    assert eng._pyttsx_engine.ran_and_waited == 0, (
        "runAndWait() is back on the happy path — Stop cannot interrupt it"
    )
    print(f"PASS: offline speech stopped in {elapsed * 1000:.0f}ms")


def test_stop_purges_the_sapi_queue():
    """stop() must tell SAPI itself to shut up, not just flip a flag —
    Engine.stop() is what issues the purging Speak() call."""
    eng = _engine()
    eng.stop()
    assert eng._pyttsx_engine.stop_calls >= 1, "stop() never reached the SAPI engine"
    assert eng.is_speaking is False
    print("PASS: stop() purges the SAPI queue")


def test_offline_continuous_utterance_ends_its_loop():
    """The normal SAPI path must queue the selection once for gapless speech,
    then endLoop() so the next utterance is not poisoned by a started loop."""
    eng = _engine(iters_to_finish=2)
    eng._speak_offline("Hello there. Second sentence.", threading.Event())
    fake = eng._pyttsx_engine
    assert fake.spoken == ["Hello there. Second sentence."], fake.spoken
    assert fake.end_loop_calls == 1, fake.end_loop_calls
    assert fake._inLoop is False, "loop left running — next utterance would raise"
    print("PASS: offline speech stays continuous and closes its loop")


def test_stuck_in_loop_flag_is_cleared():
    """A previous run that died inside the loop leaves _inLoop True; pyttsx3
    then raises from BOTH runAndWait() and startLoop() and the fallback is
    silently mute for the rest of the session."""
    eng = _engine(iters_to_finish=2)
    eng._pyttsx_engine._inLoop = True
    eng._speak_offline("Recover from a stuck loop.", threading.Event())
    assert eng._pyttsx_engine.spoken == ["Recover from a stuck loop."]
    print("PASS: a stuck _inLoop flag is cleared, not fatal")


def test_offline_falls_back_to_blocking_loop_per_chunk():
    """If external-loop mode is unavailable we must still chunk, so Stop costs
    one sentence rather than the whole document."""
    eng = _engine(external_loop=False)
    eng._speak_offline("One. Two. Three.", threading.Event())
    fake = eng._pyttsx_engine
    assert fake.ran_and_waited == 3, fake.ran_and_waited
    assert fake.spoken == ["One.", "Two.", "Three."], fake.spoken
    print("PASS: degraded offline path still chunks")


def test_vlc_warmup_honors_stop():
    """VLC's play() is asynchronous; the warm-up poll used to sleep up to a
    full second with no stop check, so Stop pressed there kept playing."""
    _install_fake_vlc()
    eng = _engine()
    player = FakeVlcPlayer(becomes_playing_after=None)  # never reports playing
    eng._vlc_player = player
    eng._vlc_instance = FakeVlcInstance()

    stop_event = threading.Event()
    threading.Timer(0.05, stop_event.set).start()
    started = time.monotonic()
    eng._play_vlc("chunk.mp3", stop_event)
    elapsed = time.monotonic() - started

    assert elapsed < 0.6, f"warm-up ignored stop for {elapsed:.2f}s"
    assert player.stops >= 1, "the player was never stopped"
    assert player.rate is None, "set_rate ran after stop — the warm-up fell through"
    print(f"PASS: VLC warm-up honored stop in {elapsed * 1000:.0f}ms")


def test_vlc_never_starts_after_stop():
    """stop() on the GUI thread landing before play() on the worker must not
    leave the next chunk playing on."""
    _install_fake_vlc()
    eng = _engine()
    player = FakeVlcPlayer()
    eng._vlc_player = player
    eng._vlc_instance = FakeVlcInstance()

    stop_event = threading.Event()
    stop_event.set()
    eng._play_vlc("chunk.mp3", stop_event)
    assert player.plays == 0, "a stopped utterance still started playback"
    print("PASS: no playback starts after stop")


# --------------------------------------------------------------------------- #
# Local neural path — speed must be real and the stream must stay gapless
# --------------------------------------------------------------------------- #
def test_spoken_text_removes_markdown_source_but_keeps_visible_words():
    from voiceassistant.tts import prepare_text_for_speech

    source = (
        "1\\. Pick the outcomes AI should own\n"
        "For each workflow, specify:\n"
        "\\- Trigger\n"
        "\\- Required inputs\n"
        "[Source transcript (line 22)](</C:/Users/example/private-source.md:22>)"
    )

    assert prepare_text_for_speech(source) == (
        "1. Pick the outcomes AI should own.\n"
        "For each workflow, specify:\n"
        "Trigger.\n"
        "Required inputs.\n"
        "Source transcript (line 22)"
    )


def test_spoken_text_preserves_normal_hyphens_acronyms_and_identifiers():
    from voiceassistant.tts import prepare_text_for_speech

    source = "AI owns a measurable outcome for AMAZON_NA and high-speed work."
    assert prepare_text_for_speech(source) == source


def test_speak_submits_normalized_text_without_logging_payload():
    eng = _engine()
    submitted = []
    eng._worker.submit = lambda fn, *args: submitted.append(args)

    eng.speak("\\- Trigger\n[Visible label](<C:/private/source.md>)")

    assert submitted
    assert submitted[0][0] == "Trigger.\nVisible label"


def test_local_high_speed_uses_quality_model_then_tempo_filter(monkeypatch):
    """High speed must not be generated by making Kokoro swallow words."""
    import numpy as np
    from voiceassistant.tts import (
        LOCAL_PCM_SAMPLE_RATE,
        LOCAL_QUALITY_EFFECTIVE_SPEED,
        LOCAL_QUALITY_MODEL_SPEED,
        TTSEngine,
    )

    calls = []
    tempo_calls = []

    class FakeKokoro:
        def _prepare(self, text, voice, speed, lang, is_phonemes,
                     sentence_pause, clause_pause):
            calls.append(("prepare", text, voice, speed, lang))
            return "voice-style", "phonemes", [("phonemes", 0.0)]

        def _create_batches(self, batches, voice_style, speed, trim):
            calls.append(("infer", batches, voice_style, speed, trim))
            return np.zeros(2400, dtype=np.float32), []

    monkeypatch.setattr(
        TTSEngine,
        "_time_stretch_local_audio",
        staticmethod(
            lambda samples, factor: tempo_calls.append(factor) or samples
        ),
    )

    audio, sample_rate = TTSEngine._create_local_audio(
        FakeKokoro(), "Read this quickly.", "am_michael", 2.1
    )

    assert calls[0][3] == LOCAL_QUALITY_MODEL_SPEED
    assert calls[1][3] == LOCAL_QUALITY_MODEL_SPEED
    assert len(tempo_calls) == 1
    assert math.isclose(
        tempo_calls[0], 2.1 / LOCAL_QUALITY_EFFECTIVE_SPEED, rel_tol=1e-9
    )
    assert sample_rate == LOCAL_PCM_SAMPLE_RATE
    assert len(audio) == 2400


def test_local_speech_prepares_full_text_once_and_freezes_speed():
    """Native pauses and one rate must survive across every streamed batch."""
    import numpy as np

    eng = _engine()
    eng._use_local = True
    eng.set_speed(2.6)
    prepared = []
    generated = []

    class FakeKokoro:
        def _prepare(self, text, voice, speed, lang, is_phonemes,
                     sentence_pause, clause_pause):
            prepared.append((text, voice, speed, sentence_pause, clause_pause))
            return "voice-style", "all-phonemes", [
                ("first native phrase.", 0.25),
                ("second native phrase.", 0.0),
            ]

        def _create_batch(self, phonemes, voice_style, speed, trim, pause):
            generated.append((phonemes, voice_style, speed, trim, pause))
            # A slider move during inference must not alter later queued audio.
            if len(generated) == 1:
                eng.set_speed(1.0)
            return np.zeros(2400, dtype=np.float32), None

    eng._kokoro = FakeKokoro()
    tempo_calls = []
    eng._time_stretch_local_audio = (
        lambda samples, factor: tempo_calls.append(factor) or samples
    )

    starts = []
    eng._start_vlc_pcm_stream = (
        lambda stream, stop_event, progress: (starts.append(stream), progress.append("started"))
    )
    eng._wait_vlc_stream = lambda stream, stop_event: None
    progress = []
    full_text = "First native phrase. Second native phrase."
    eng._speak_local(full_text, threading.Event(), progress)

    assert prepared == [(full_text, "am_michael", 1.2, 0.25, 0.1)]
    assert [item[2] for item in generated] == [1.2, 1.2]
    assert [item[4] for item in generated] == [0.25, 0.0]
    assert len(tempo_calls) == 2
    assert all(math.isclose(value, 2.6 / 1.15, rel_tol=1e-9) for value in tempo_calls)
    assert len(starts) == 1, "local blocks opened more than one VLC stream"
    assert progress == ["started"]


def test_local_pcm_stream_never_delegates_speed_to_vlc():
    """Regression: callback-backed raw PCM ignored VLC set_rate(2.1)."""
    _install_fake_vlc()
    eng = _engine()
    eng._use_local = True
    eng.set_speed(2.1)
    player = FakeVlcPlayer(becomes_playing_after=1)
    eng._vlc_player = player
    eng._vlc_instance = FakeVlcInstance()

    class FakeStream:
        error = None

        def read(self, size):
            return b""

    progress = []
    eng._start_vlc_pcm_stream(FakeStream(), threading.Event(), progress)

    assert player.rate == 1.0, (
        "local PCM was handed a fake VLC speed instead of being generated at speed"
    )
    assert progress == ["local-neural-stream-started"]


def test_local_pcm_is_exposed_on_its_real_sample_clock():
    """VLC must not read 45 seconds of PCM in ~20 seconds again."""
    from voiceassistant.tts import _PacedAudioStream

    class Source:
        error = None

        def __init__(self, data):
            self.data = bytearray(data)

        def read(self, size):
            if not self.data:
                return b""
            part = bytes(self.data[:size])
            del self.data[:size]
            return part

    # 0.2 seconds of mono s16 PCM at 1 kHz = 400 bytes.
    source = Source(b"x" * 400)
    now = [0.0]

    def wait_without_sleep(delay):
        now[0] += delay
        return False

    paced = _PacedAudioStream(
        source,
        threading.Event(),
        sample_rate=1000,
        lead_seconds=0.02,
        frame_seconds=0.05,
        clock=lambda: now[0],
        waiter=wait_without_sleep,
    )
    received = bytearray()
    while True:
        data = paced.read(256)
        if not data:
            break
        received.extend(data)

    assert received == b"x" * 400
    assert 0.17 <= now[0] <= 0.25, (
        f"0.2 seconds of PCM was exposed over {now[0]:.3f}s"
    )


def test_stop_interrupts_pcm_pacing_immediately():
    from voiceassistant.tts import _PacedAudioStream

    class Source:
        error = None

        def read(self, size):
            raise AssertionError("stopped pacer reached the source")

    stop_event = threading.Event()
    stop_event.set()
    paced = _PacedAudioStream(Source(), stop_event)
    assert paced.read(4096) is None


def test_local_speed_is_honestly_clamped_to_measured_ceiling():
    from voiceassistant.tts import LOCAL_MAX_SPEED

    eng = _engine()
    eng._use_local = True
    eng.set_speed(3.0)
    assert eng._speed == LOCAL_MAX_SPEED == 2.6


def test_local_speed_plan_preserves_quality_at_saved_2_6x():
    from voiceassistant.tts import TTSEngine

    assert TTSEngine._local_speed_plan(1.0) == (1.0, 1.0)
    assert TTSEngine._local_speed_plan(1.2) == (1.2, 1.0)
    model_speed, tempo_factor = TTSEngine._local_speed_plan(2.6)
    assert model_speed == 1.2
    assert math.isclose(tempo_factor, 2.6 / 1.15, rel_tol=1e-9)


def test_local_tempo_filter_halves_duration_without_raising_pitch():
    import numpy as np
    from voiceassistant.tts import LOCAL_PCM_SAMPLE_RATE, TTSEngine

    seconds = 1.0
    frequency = 440.0
    times = np.arange(int(LOCAL_PCM_SAMPLE_RATE * seconds)) / LOCAL_PCM_SAMPLE_RATE
    tone = np.sin(2 * np.pi * frequency * times).astype(np.float32)

    stretched = TTSEngine._time_stretch_local_audio(tone, 2.0)

    assert 0.45 <= len(stretched) / LOCAL_PCM_SAMPLE_RATE <= 0.55
    crossings = np.flatnonzero(np.diff(np.signbit(stretched)))
    measured_frequency = len(crossings) / (2 * len(stretched) / LOCAL_PCM_SAMPLE_RATE)
    assert 425 <= measured_frequency <= 455


def test_neural_uses_one_protected_stream(monkeypatch):
    """Gapless-reading regression: even a long selection must use one edge-tts
    Communicate and one VLC byte stream. Separate mini-MP3s produced audible
    encoder/prosody seams and left canceled requests draining in the background."""
    calls = []
    played = []

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            calls.append((text, voice, kwargs))

        async def stream(self):
            yield {"type": "audio", "data": b"first-half"}
            await asyncio.sleep(0.02)
            yield {"type": "audio", "data": b"second-half"}

    fake_edge = types.ModuleType("edge_tts")
    fake_edge.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    eng = _engine()

    def start_stream(stream, stop_event, progress):
        played.append(stream)
        progress.append("started")

    def consume_stream(stream, stop_event):
        audio = bytearray()
        while True:
            data = stream.read(4096)
            assert data is not None, stream.error
            if not data:
                break
            audio.extend(data)
        played[0] = bytes(audio)

    eng._start_vlc_stream = start_stream
    eng._wait_vlc_stream = consume_stream
    progress = []
    text = " ".join(["One continuous neural selection."] * 180)
    eng._synthesize_and_play(text, 7, threading.Event(), progress)

    assert len(text) > 5_000
    assert len(calls) == 1, f"opened {len(calls)} neural connections"
    assert calls[0][0] == text
    assert played == [b"first-halfsecond-half"]
    assert len(progress) == 1, progress
    print("PASS: neural speech uses one protected stream for the selection")


def test_neural_starts_after_lead_not_full_synthesis(monkeypatch):
    """Startup latency regression: a long selection starts after its protected
    lead is ready; it must not wait for Microsoft to finish the entire MP3."""
    allow_finish = threading.Event()
    producer_finished = threading.Event()
    playback_saw_live_producer = []
    calls = []

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            calls.append(text)

        async def stream(self):
            # 30 KB exceeds the 24 KB lead at the test engine's 1.0x speed.
            yield {"type": "audio", "data": b"a" * 30_000}
            while not allow_finish.is_set():
                await asyncio.sleep(0.01)
            yield {"type": "audio", "data": b"b" * 10_000}
            producer_finished.set()

    fake_edge = types.ModuleType("edge_tts")
    fake_edge.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    eng = _engine()

    def start_stream(stream, stop_event, progress):
        playback_saw_live_producer.append(not producer_finished.is_set())
        progress.append("started")
        allow_finish.set()

    def consume_stream(stream, stop_event):
        while True:
            data = stream.read(4096)
            assert data is not None, stream.error
            if not data:
                return

    eng._start_vlc_stream = start_stream
    eng._wait_vlc_stream = consume_stream
    long_text = " ".join(["prefetch"] * 100)
    eng._synthesize_and_play(
        long_text,
        9,
        threading.Event(),
        [],
    )

    assert calls == [long_text], f"selection was split into {len(calls)} requests"
    assert playback_saw_live_producer == [True]
    assert producer_finished.is_set()
    print("PASS: neural playback starts before full synthesis finishes")


def test_neural_startup_deadline_covers_a_trickling_partial_lead():
    """The 3 second speed contract is time-to-play, not merely time to the
    first byte. A service that dribbles one packet and then hangs must not leave
    the user staring at Buffering indefinitely."""
    import pytest
    from voiceassistant.tts import _StreamingAudioBuffer

    stream = _StreamingAudioBuffer(threading.Event(), threading.Event())
    stream.write(b"too-small")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="not ready to play"):
        stream.wait_until_ready(
            min_bytes=10_000,
            first_timeout=0.08,
            stall_timeout=10.0,
        )
    assert time.monotonic() - started < 0.3
    print("PASS: partial neural lead cannot evade the startup deadline")


def test_short_neural_text_uses_a_smaller_starting_reservoir():
    """A three-second high-speed reservoir can take longer to synthesize than
    a simple sentence takes to read. Short text gets a one-second lead; long
    text keeps the larger anti-jitter reservoir."""
    eng = _engine()
    assert eng.SHORT_START_BUFFER_PLAY_SECONDS == 1.0
    assert eng.START_BUFFER_PLAY_SECONDS == 3.0
    assert len("short sentence") <= eng.SHORT_TEXT_MAX_CHARS
    assert len("x" * 5_553) > eng.SHORT_TEXT_MAX_CHARS


def test_vlc_readahead_wait_is_not_a_false_underrun():
    """VLC can drain the Python queue into its own cache while audio is still
    coming out of the speakers. A producer gap longer than the old 0.75 second
    cutoff must resume normally instead of killing an otherwise healthy read."""
    from voiceassistant.tts import _StreamingAudioBuffer

    stop_event = threading.Event()
    cancel_event = threading.Event()
    stream = _StreamingAudioBuffer(stop_event, cancel_event)

    def delayed_packet():
        time.sleep(0.9)
        stream.write(b"continued-audio")
        stream.finish()

    producer = threading.Thread(target=delayed_packet, daemon=True)
    producer.start()
    started = time.monotonic()
    data = stream.read(4096)
    elapsed = time.monotonic() - started
    producer.join(0.5)

    assert data == b"continued-audio"
    assert elapsed >= 0.75, f"test did not cross the old cutoff: {elapsed:.2f}s"
    assert stream.error is None
    assert stream.read(4096) == b""
    print(f"PASS: VLC read-ahead gap resumed after {elapsed:.2f}s")


def test_stop_interrupts_a_blocked_neural_stream_read():
    """Removing the false underrun cutoff must not weaken Stop. Even without
    another network packet, a blocked VLC callback must unwind promptly."""
    from voiceassistant.tts import _StreamingAudioBuffer

    stop_event = threading.Event()
    stream = _StreamingAudioBuffer(stop_event, threading.Event())
    result = []
    reader = threading.Thread(target=lambda: result.append(stream.read(4096)))
    reader.start()
    time.sleep(0.05)
    started = time.monotonic()
    stop_event.set()
    reader.join(0.3)
    elapsed = time.monotonic() - started

    assert not reader.is_alive(), "Stop left VLC's stream read blocked"
    assert result == [None]
    assert elapsed < 0.15, f"blocked stream took {elapsed:.2f}s to stop"
    print(f"PASS: blocked neural stream stopped in {elapsed * 1000:.0f}ms")


def test_stop_interrupts_neural_buffering_before_playback(monkeypatch):
    """Buffering cannot weaken the Stop contract: a stop while the network is
    still producing must return promptly and must never start partial audio."""
    first_packet = threading.Event()
    played = []

    class SlowCommunicate:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            first_packet.set()
            yield {"type": "audio", "data": b"partial"}
            await asyncio.sleep(0.20)
            yield {"type": "audio", "data": b"too-late"}

    fake_edge = types.ModuleType("edge_tts")
    fake_edge.Communicate = SlowCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    eng = _engine()
    eng._start_vlc_stream = lambda stream, stop_event, progress: played.append(stream)
    stop_event = threading.Event()
    worker = threading.Thread(
        target=eng._synthesize_and_play,
        args=("Do not play a partial buffer.", 8, stop_event, []),
    )
    worker.start()
    assert first_packet.wait(1.0), "neural producer never started"
    stop_event.set()
    worker.join(0.5)

    assert not worker.is_alive(), "Stop left the buffer consumer wedged"
    assert played == [], "partial neural audio started after Stop"
    print("PASS: Stop interrupts neural buffering without playing partial audio")


def test_stop_closes_the_active_neural_request(monkeypatch):
    """Stop must retire the cloud request itself. Merely ignoring its writes
    leaves a canceled websocket competing with the next selected-text job."""
    stream_closed = threading.Event()

    class HangingCommunicate:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            try:
                yield {"type": "audio", "data": b"a" * 30_000}
                while True:
                    await asyncio.sleep(1)
            finally:
                stream_closed.set()

    fake_edge = types.ModuleType("edge_tts")
    fake_edge.Communicate = HangingCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)

    eng = _engine()
    stop_event = threading.Event()

    def stop_during_playback(stream, event, progress):
        progress.append("started")
        event.set()

    eng._start_vlc_stream = stop_during_playback
    eng._synthesize_and_play(
        "Stop this single neural request.", 11, stop_event, []
    )

    assert stream_closed.wait(0.2), "canceled edge-tts generator stayed open"
    assert not any(
        t.is_alive() and t.name == "tts-producer-11"
        for t in threading.enumerate()
    ), "canceled neural producer survived behind the next job"
    print("PASS: Stop closes and retires the active neural request")


# --------------------------------------------------------------------------- #
# Bug 2 — the robotic voice
# --------------------------------------------------------------------------- #
def test_fallback_never_selects_a_neural_voice_id():
    """The wrong-voice bug: 'en-US-AndrewNeural' is meaningless to SAPI, so
    setting it left whatever Windows happened to default to."""
    eng = _engine()
    eng.set_voice("en-US-AndrewNeural")
    eng._speak_offline("Neural failed, so this is the fallback.", threading.Event())
    chosen = eng._pyttsx_engine.props.get("voice")
    assert chosen != "en-US-AndrewNeural", "a neural id was pushed into SAPI"
    assert chosen == DAVID.id, chosen
    print("PASS: fallback picks a real SAPI voice, never the neural id")


def test_fallback_matches_the_chosen_gender():
    eng = _engine()
    eng.set_voice("en-US-EmmaNeural")  # female
    eng._speak_offline("Fallback for a female neural voice.", threading.Event())
    assert eng._pyttsx_engine.props.get("voice") == ZIRA.id
    print("PASS: fallback keeps the chosen gender")


def test_user_selected_sapi_voice_is_honored():
    eng = _engine()
    eng.set_voice(f"sapi:{ZIRA.id}")
    assert eng._use_offline is True
    eng._speak_offline("Deliberately offline.", threading.Event())
    assert eng._pyttsx_engine.props.get("voice") == ZIRA.id
    print("PASS: an explicitly chosen SAPI voice is used as-is")


def test_no_sapi_match_leaves_the_system_default():
    """An en-GB neural voice on a box with only en-US voices: gender still
    matches, and a locale with no match at all must not crash."""
    eng = _engine()
    eng._pyttsx_engine.voices = []
    eng.set_voice("en-GB-SoniaNeural")
    eng._speak_offline("No SAPI voices installed.", threading.Event())
    assert "voice" not in eng._pyttsx_engine.props
    print("PASS: no match leaves the system default alone")


def test_midway_failure_does_not_reread_the_whole_text():
    """A failure after some audio played used to re-read the ENTIRE text with
    SAPI — you heard the opening twice, the second time robotic."""
    eng = _engine()
    calls = []
    eng._speak_offline = lambda text, stop_event=None: calls.append(text)

    def boom(text, gen, stop_event, progress):
        progress.append("chunk-that-played.mp3")
        raise RuntimeError("VLC died three sentences in")

    eng._speak_neural = boom
    _run_job(eng, "Alpha. Bravo. Charlie.")
    assert calls == [], f"re-read the whole text through SAPI: {calls}"
    print("PASS: a mid-utterance failure truncates instead of re-reading")


def test_fast_failure_retries_before_going_robotic():
    """One transient edge-tts error used to cost the utterance its voice."""
    eng = _engine()
    attempts = []
    fell_back = []
    eng._speak_offline = lambda text, stop_event=None: fell_back.append(text)

    def flaky(text, gen, stop_event, progress=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("simulated: transient websocket close")
        progress.append("ok.mp3")

    eng._synthesize_and_play = flaky
    _run_job(eng, "Retry me.")
    assert len(attempts) == 2, attempts
    assert fell_back == [], "fell back to SAPI despite the retry succeeding"
    print("PASS: a fast failure is retried on the neural path")


def test_stall_is_not_retried():
    """A stall already burned FIRST_AUDIO_TIMEOUT; retrying it doubles the dead
    air. It must also never change a selected neural voice into SAPI."""
    eng = _engine()
    attempts = []
    fell_back = []
    errors = []
    eng.error.connect(errors.append)
    eng._speak_offline = lambda text, stop_event=None: fell_back.append(text)

    def stalls(text, gen, stop_event, progress=None):
        attempts.append(1)
        raise TimeoutError("Neural TTS was not ready within 6s (network stall)")

    eng._synthesize_and_play = stalls
    _run_job(eng, "Stall me.")
    assert len(attempts) == 1, f"a stall was retried: {len(attempts)} attempts"
    assert fell_back == [], "selected neural voice changed into robotic SAPI"
    assert errors and "chosen neural voice unavailable" in errors[-1]
    print("PASS: a stall fails honestly without retrying or changing voice")


def test_neural_failure_never_enters_robotic_fallback():
    """Andrew/Emma/etc. are hard voice choices. A connection failure may emit
    an honest error, but it must never start the Windows voice automatically."""
    eng = _engine()
    offline = []
    errors = []
    eng.error.connect(errors.append)
    eng._speak_offline = lambda text, stop_event=None: offline.append(text)

    def fails(text, gen, stop_event, progress=None):
        raise ConnectionError("simulated: offline")

    eng._synthesize_and_play = fails
    _run_job(eng, "First.")
    assert offline == [], f"robotic fallback spoke: {offline}"
    assert errors and "chosen neural voice unavailable" in errors[-1]
    print("PASS: a neural failure never changes into the robotic voice")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL TTS STOP/VOICE TESTS PASSED")
