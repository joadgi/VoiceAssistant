"""TTS regression tests: a network stall must NEVER wedge the engine.

The failure this guards: edge-tts connects but the stream never yields and
never raises. The consumer must time out without changing the selected neural
voice into SAPI, stop() must retire the request, and the engine's single worker
must remain alive and drainable afterwards (threading law).

Runnable standalone (python tests/test_tts_stall.py) or via pytest.
"""

import os
import sys
import time
import types
import asyncio
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_fake_edge_tts(behavior):
    """Inject a fake edge_tts module. behavior: 'stall' | 'fail_fast'."""

    class FakeCommunicate:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            if behavior == "fail_fast":
                raise ConnectionError("simulated: DNS/offline failure")
            # 'stall': connected, but no audio ever arrives and no exception is
            # raised — the exact wedge case.
            await asyncio.sleep(3600)
            yield  # pragma: no cover — never reached

    fake = types.ModuleType("edge_tts")
    fake.Communicate = FakeCommunicate
    sys.modules["edge_tts"] = fake


def _make_engine():
    from voiceassistant.tts import TTSEngine

    eng = TTSEngine()
    return eng


def _assert_worker_drains(eng, timeout, context):
    """The engine's ONE worker must be able to run a probe job — if a previous
    job is wedged (unbounded wait), the probe never runs."""
    probe = threading.Event()
    eng._worker.submit(lambda: probe.set())
    assert probe.wait(timeout), f"tts worker is wedged ({context})"


def test_stall_fails_without_changing_voice():
    """No playable lead within FIRST_AUDIO_TIMEOUT -> honest error, never SAPI."""
    _install_fake_edge_tts("stall")
    eng = _make_engine()

    fallback_called = threading.Event()
    error_called = threading.Event()
    eng._speak_offline = lambda text, stop_event=None: fallback_called.set()
    from PySide6.QtCore import Qt
    eng.error.connect(
        lambda _message: error_called.set(), Qt.ConnectionType.DirectConnection
    )

    eng.speak("This request will stall on the network.")
    # 6s first-audio timeout + margin
    assert error_called.wait(timeout=10), "network stall did not surface or worker wedged"
    assert not fallback_called.is_set(), "stall changed neural voice into robotic SAPI"
    _assert_worker_drains(eng, 5, "after stalled neural request")
    assert eng._speaking is False
    print("PASS: stall -> honest error, chosen voice preserved, worker drains")


def test_stop_unwedges_stalled_worker():
    """stop() during a stall must unwind the utterance within ~1s (bounded get)."""
    _install_fake_edge_tts("stall")
    eng = _make_engine()

    fallback_called = threading.Event()
    eng._speak_offline = lambda text, stop_event=None: fallback_called.set()

    eng.speak("Stall, then the user presses stop.")
    time.sleep(1.0)  # let the worker enter the consume loop
    eng.stop()

    _assert_worker_drains(eng, 3, "after stop() during stall")
    # User-initiated stop must NOT trigger the offline fallback.
    time.sleep(0.5)
    assert not fallback_called.is_set(), "stop() wrongly triggered offline fallback"
    assert eng._speaking is False
    print("PASS: stop() unwedged a stalled utterance, no spurious fallback")


def test_fast_failure_retries_then_preserves_voice():
    """A fast DNS failure gets one neural retry, then an error—not SAPI."""
    _install_fake_edge_tts("fail_fast")
    eng = _make_engine()

    fallback_called = threading.Event()
    error_called = threading.Event()
    eng._speak_offline = lambda text, stop_event=None: fallback_called.set()
    from PySide6.QtCore import Qt
    eng.error.connect(
        lambda _message: error_called.set(), Qt.ConnectionType.DirectConnection
    )

    eng.speak("This request fails immediately.")
    assert error_called.wait(timeout=8), "fast neural failure did not surface"
    assert not fallback_called.is_set(), "fast failure changed voice into SAPI"
    _assert_worker_drains(eng, 5, "after fast failure")
    print("PASS: fast failure retries once, then preserves the chosen voice")


def test_speak_interrupts_previous_utterance():
    """speak() during speech stops the old utterance and plays the new one —
    the old embedded toggle silently DROPPED the new text."""
    _install_fake_edge_tts("stall")
    eng = _make_engine()
    eng._speak_offline = lambda text, stop_event=None: None

    eng.speak("first utterance (stalls)")
    time.sleep(0.5)
    first_stop = eng._active_stop
    eng.speak("second utterance")
    assert first_stop is not None and first_stop.is_set(), (
        "new speak() did not stop the previous utterance"
    )
    assert eng._gen == 2, "generation counter did not advance"
    eng.stop()
    _assert_worker_drains(eng, 8, "after interrupt sequence")
    print("PASS: speak() interrupts the previous utterance (generation advance)")


if __name__ == "__main__":
    test_stall_fails_without_changing_voice()
    test_stop_unwedges_stalled_worker()
    test_fast_failure_retries_then_preserves_voice()
    test_speak_interrupts_previous_utterance()
    print("\nALL TTS TESTS PASSED")
