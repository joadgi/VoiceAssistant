"""TTS regression tests: a network stall must NEVER wedge the engine.

The failure this guards: edge-tts connects but the stream never yields and
never raises. The consumer must time out into the offline fallback, stop()
must unwedge a stalled utterance, and the engine's single worker must remain
alive and drainable afterwards (threading law).

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
    # Ensure the fallback path is reachable even if SAPI init failed on this box;
    # _speak_offline is patched in every test so it is never actually invoked.
    if not eng._pyttsx_engine:
        eng._pyttsx_engine = object()
    return eng


def _assert_worker_drains(eng, timeout, context):
    """The engine's ONE worker must be able to run a probe job — if a previous
    job is wedged (unbounded wait), the probe never runs."""
    probe = threading.Event()
    eng._worker.submit(lambda: probe.set())
    assert probe.wait(timeout), f"tts worker is wedged ({context})"


def test_stall_falls_back_to_offline():
    """No first audio within FIRST_AUDIO_TIMEOUT -> TimeoutError -> offline fallback."""
    _install_fake_edge_tts("stall")
    eng = _make_engine()

    fallback_called = threading.Event()
    eng._speak_offline = lambda text, stop_event=None: fallback_called.set()

    eng.speak("This request will stall on the network.")
    # 6s first-audio timeout + margin
    assert fallback_called.wait(timeout=12), (
        "offline fallback did not fire after a network stall — worker is wedged"
    )
    _assert_worker_drains(eng, 5, "after stall fallback")
    assert eng._speaking is False
    print("PASS: stall -> offline fallback fired, worker drains cleanly")


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


def test_fast_failure_still_falls_back():
    """Fast failures (offline/DNS) must keep the pre-existing fallback path."""
    _install_fake_edge_tts("fail_fast")
    eng = _make_engine()

    fallback_called = threading.Event()
    eng._speak_offline = lambda text, stop_event=None: fallback_called.set()

    eng.speak("This request fails immediately.")
    assert fallback_called.wait(timeout=8), "fast-failure fallback regressed"
    _assert_worker_drains(eng, 5, "after fast failure")
    print("PASS: fast failure -> offline fallback (no regression)")


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
    test_stall_falls_back_to_offline()
    test_stop_unwedges_stalled_worker()
    test_fast_failure_still_falls_back()
    test_speak_interrupts_previous_utterance()
    print("\nALL TTS TESTS PASSED")
