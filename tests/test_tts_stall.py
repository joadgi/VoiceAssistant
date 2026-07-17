"""Phase 0 regression tests: TTS network-stall must NEVER wedge the engine.

The failure this guards: edge-tts connects but the stream never yields and never
raises. Before the fix, the consumer blocked forever on audio_q.get() holding
TTSEngine._lock — read-aloud dead until app restart, offline fallback never fired.

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
    from tts_engine import TTSEngine

    eng = TTSEngine()
    # Ensure the fallback path is reachable even if SAPI init failed on this box;
    # _speak_offline is patched in every test so it is never actually invoked.
    if not eng._pyttsx_engine:
        eng._pyttsx_engine = object()
    return eng


def test_stall_falls_back_to_offline():
    """No first audio within FIRST_AUDIO_TIMEOUT -> TimeoutError -> offline fallback."""
    _install_fake_edge_tts("stall")
    eng = _make_engine()

    fallback_called = threading.Event()
    eng._speak_offline = lambda text: fallback_called.set()

    eng.speak("This request will stall on the network.")
    # 6s first-audio timeout + margin
    assert fallback_called.wait(timeout=12), (
        "offline fallback did not fire after a network stall — worker is wedged"
    )
    # Worker must fully unwind and release the lock.
    assert eng._lock.acquire(timeout=5), "worker never released _lock after stall"
    eng._lock.release()
    assert eng._speaking is False
    print("PASS: stall -> offline fallback fired, worker unwound cleanly")


def test_stop_unwedges_stalled_worker():
    """stop() during a stall must unwind the worker within ~1s (bounded get)."""
    _install_fake_edge_tts("stall")
    eng = _make_engine()

    fallback_called = threading.Event()
    eng._speak_offline = lambda text: fallback_called.set()

    eng.speak("Stall, then the user presses stop.")
    time.sleep(1.0)  # let the worker enter the consume loop
    eng.stop()

    assert eng._lock.acquire(timeout=3), (
        "worker did not unwind within 3s of stop() — get() is still unbounded"
    )
    eng._lock.release()
    # User-initiated stop must NOT trigger the offline fallback.
    time.sleep(0.5)
    assert not fallback_called.is_set(), "stop() wrongly triggered offline fallback"
    assert eng._speaking is False
    print("PASS: stop() unwedged a stalled worker, no spurious fallback")


def test_fast_failure_still_falls_back():
    """Fast failures (offline/DNS) must keep the pre-existing fallback path."""
    _install_fake_edge_tts("fail_fast")
    eng = _make_engine()

    fallback_called = threading.Event()
    eng._speak_offline = lambda text: fallback_called.set()

    eng.speak("This request fails immediately.")
    assert fallback_called.wait(timeout=8), "fast-failure fallback regressed"
    assert eng._lock.acquire(timeout=5)
    eng._lock.release()
    print("PASS: fast failure -> offline fallback (no regression)")


if __name__ == "__main__":
    test_stall_falls_back_to_offline()
    test_stop_unwedges_stalled_worker()
    test_fast_failure_still_falls_back()
    print("\nALL PHASE 0 TTS TESTS PASSED")
