"""Live integration tests for the two SECONDARY features: read-aloud (TTS) and OCR.

These are REAL end-to-end tests — no network/audio mocking. They prove the
features actually produce audio / read real pixels, and they do it
OBJECTIVELY (you cannot judge audio by ear in automation):

  * TTS  — non-empty, on-disk MP3 chunk files appear in the engine's temp dir;
           `speaking_started` fires before `speaking_finished`; no `error`
           signal; measured time-to-first-audio and total wall time; chunk
           count scales with sentence count; offline SAPI path produces NO
           neural MP3s and touches no network; shutdown removes the temp dir.
  * OCR  — word recall on images rendered in several fonts/sizes, a multi-line
           code block, and a mock dialog; per-read latency well under 200ms
           (native engine is ~10ms); and ONE real screen capture proving the
           capture -> OCR path runs on live pixels.

OUT OF SCOPE (not automatable): perceptual audio *quality* / naturalness of the
neural voice. We assert the machinery emits valid, non-empty, completed audio;
a human still judges how it sounds. Likewise we assert OCR *recall* of known
words, not layout fidelity.

Gating / side effects:
  * TTS  — gated behind RUN_TTS_LIVE=1. PLAYS AUDIO on the live machine and
           needs network (edge-tts) + VLC installed. NOT run by the default
           suite. If the neural path yields no audio (e.g. offline) the neural
           tests SKIP rather than fail — network is an environment dependency.
  * OCR  — gated behind RUN_OCR_LIVE=1. Renders in-memory images and takes ONE
           real screenshot of the top-left 400x120 px. No audio, no focus
           steal. Skips cleanly if the Windows OCR engine (winsdk / language
           pack) is unavailable, exactly like tests/test_ocr_backend.py.

Run (PowerShell):
    # OCR only (safe — no audio, no focus steal):
    $env:RUN_OCR_LIVE=1; venv\\Scripts\\python -m pytest tests/integration/test_tts_ocr_live.py -v -s -k OCR
    # TTS only (PLAYS AUDIO, needs network — run serially, not alongside dictation):
    $env:RUN_TTS_LIVE=1; venv\\Scripts\\python -m pytest tests/integration/test_tts_ocr_live.py -v -s -k TTS

The engines are QObjects, but we do NOT run a Qt event loop: signals are
connected with DirectConnection so they fire synchronously in the worker
thread at emit time, and the main thread drives everything by polling
threading primitives / the on-disk temp dir (a tiny watcher thread) with
bounded waits — the same synchronous style as the existing test suites.
"""

import os
import sys
import time
import statistics
import threading

import pytest
from PIL import Image, ImageDraw, ImageFont

# repo root = three levels up from tests/integration/<thisfile>
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# If a QApplication ends up being created (TTS), keep it windowless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _flag(name):
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


RUN_TTS_LIVE = _flag("RUN_TTS_LIVE")
RUN_OCR_LIVE = _flag("RUN_OCR_LIVE")


# --------------------------------------------------------------------------- #
# Shared synchronous-driving helpers (no Qt event loop needed)
# --------------------------------------------------------------------------- #
def _process_events():
    """Best-effort event pump so QUEUED connections (if any) also deliver.
    DirectConnection already fires synchronously; this is belt-and-suspenders."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()
    except Exception:
        pass


def _wait(pred, timeout, interval=0.05):
    """Poll pred() until true or timeout (seconds). Returns pred()'s final value."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        _process_events()
        time.sleep(interval)
    return bool(pred())


# --------------------------------------------------------------------------- #
# TTS live tests
# --------------------------------------------------------------------------- #
class _SignalLog:
    """Records TTSEngine signals with timestamps, via DirectConnection so they
    fire in the worker thread without any running Qt event loop."""

    def __init__(self, engine):
        from PySide6.QtCore import Qt

        dc = Qt.ConnectionType.DirectConnection
        self.started = []
        self.finished = []
        self.status = []   # list[(t, str)]
        self.errors = []   # list[(t, str)]
        engine.speaking_started.connect(lambda: self.started.append(time.monotonic()), dc)
        engine.speaking_finished.connect(lambda: self.finished.append(time.monotonic()), dc)
        engine.status.connect(lambda s: self.status.append((time.monotonic(), s)), dc)
        engine.error.connect(lambda e: self.errors.append((time.monotonic(), e)), dc)

    def first_playing_offset(self, t0):
        """Wall-time (s) from t0 to the first neural 'Playing ...' status, or None.
        The engine only emits 'Playing' AFTER a non-empty MP3 was handed to VLC,
        so this doubles as proof that real audio was produced."""
        for ts, s in self.status:
            if s.startswith("Playing"):
                return ts - t0
        return None


def _watch_mp3s(engine, stop_evt, seen):
    """Poll the engine's temp dir and record every non-empty *.mp3 seen (name ->
    max size). Chunks are deleted right after playback, so we must observe them
    live; `seen` retains the evidence after deletion."""
    d = engine._temp_dir
    while not stop_evt.is_set():
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        for name in names:
            if name.endswith(".mp3"):
                try:
                    sz = os.path.getsize(os.path.join(d, name))
                except OSError:
                    continue
                if sz > 0:
                    seen[name] = max(seen.get(name, 0), sz)
        time.sleep(0.01)


def _drive_speak(engine, text, timeout):
    """Speak `text` and block (polling) until speaking_finished fires or timeout.
    Returns (log, seen_mp3s, time_to_first_audio, total_seconds, finished_ok)."""
    log = _SignalLog(engine)
    seen = {}
    stop_evt = threading.Event()
    watcher = threading.Thread(
        target=_watch_mp3s, args=(engine, stop_evt, seen), daemon=True
    )
    watcher.start()
    t0 = time.monotonic()
    engine.speak(text)
    ok = _wait(lambda: len(log.finished) >= 1, timeout)
    total = time.monotonic() - t0
    stop_evt.set()
    watcher.join(timeout=2)
    return log, seen, log.first_playing_offset(t0), total, ok


@pytest.mark.skipif(
    not RUN_TTS_LIVE,
    reason="set RUN_TTS_LIVE=1 (plays audio on the live machine; needs network + VLC)",
)
class TestTTSLive:
    @pytest.fixture(scope="class")
    def qapp(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        yield app

    @pytest.fixture
    def eng(self, qapp):
        from voiceassistant.tts import TTSEngine

        e = TTSEngine()
        try:
            yield e
        finally:
            try:
                e.shutdown()
            except Exception:
                pass

    # 1) Neural speak of a SHORT phrase -----------------------------------
    def test_neural_short_phrase(self, eng):
        log, seen, t_first, total, ok = _drive_speak(
            eng, "Integration test, phrase one.", timeout=30
        )
        assert ok, f"neural speech never finished; status={log.status} errors={log.errors}"
        if t_first is None and not seen:
            pytest.skip(
                "neural edge-tts produced no audio (network down?); "
                f"status={log.status} errors={log.errors}"
            )
        assert len(log.started) >= 1 and len(log.finished) >= 1
        assert log.started[0] <= log.finished[0], "finished fired before started"
        assert not log.errors, f"unexpected error(s): {log.errors}"
        assert seen, "no non-empty MP3 appeared in the temp dir"
        assert all(sz > 0 for sz in seen.values()), f"empty MP3(s): {seen}"
        # time-to-first-audio quality proxy (typ. 1-3s); lenient bound vs network jitter.
        assert t_first is not None and t_first < 15.0, f"time-to-first-audio too high: {t_first}"
        print(
            f"\n[TTS-1] time_to_first_audio={t_first:.2f}s total={total:.2f}s "
            f"mp3s={ {k: v for k, v in seen.items()} }"
        )

    # 2) Multi-sentence -> multiple chunk files, playback completes -------
    def test_neural_multi_sentence(self, eng):
        text = (
            "This is the first sentence of the integration test. "
            "Here is a second sentence with several more words in it. "
            "And finally a third sentence to finish the passage cleanly."
        )
        words = len(text.split())
        log, seen, t_first, total, ok = _drive_speak(eng, text, timeout=60)
        assert ok, f"multi-sentence speech never finished; status={log.status}"
        if t_first is None and not seen:
            pytest.skip(f"neural edge-tts produced no audio (network?); status={log.status}")
        assert not log.errors, f"unexpected error(s): {log.errors}"
        # Objective proxy for "duration proportional to content": the streamer
        # emits one MP3 per sentence-ish chunk, so >1 sentence => multiple files.
        assert len(seen) >= 2, f"expected multiple chunk MP3s, saw {sorted(seen)}"
        assert all(sz > 0 for sz in seen.values())
        assert total > 0.5, "multi-sentence playback finished implausibly fast"
        print(
            f"\n[TTS-2] words={words} chunks={len(seen)} "
            f"first_audio={t_first:.2f}s total={total:.2f}s "
            f"(~{words / max(total, 0.01):.1f} words/s incl. synthesis)"
        )

    # 3) Live speed control -----------------------------------------------
    def test_speed_control_live(self, eng):
        # Objective part: the clamp/call path (no ears required).
        eng.set_speed(5.0)
        assert eng._speed == 3.0, "speed not clamped to max 3.0"
        eng.set_speed(0.1)
        assert eng._speed == 0.5, "speed not clamped to min 0.5"
        eng.set_speed(2.0)
        assert eng._speed == 2.0

        # Now exercise the LIVE set_rate path during real playback.
        log = _SignalLog(eng)
        seen = {}
        stop_evt = threading.Event()
        watcher = threading.Thread(
            target=_watch_mp3s, args=(eng, stop_evt, seen), daemon=True
        )
        watcher.start()
        t0 = time.monotonic()
        eng.speak("Changing the playback speed live while this sentence is spoken aloud.")
        # Wait until audio is actually playing, then poke the speed (hits VLC set_rate).
        _wait(lambda: eng.is_speaking and log.first_playing_offset(t0) is not None, timeout=20)
        eng.set_speed(1.5)
        eng.set_speed(0.75)
        ok = _wait(lambda: len(log.finished) >= 1, timeout=40)
        stop_evt.set()
        watcher.join(timeout=2)

        assert ok, f"speech did not complete after live speed changes; status={log.status}"
        if log.first_playing_offset(t0) is None and not seen:
            pytest.skip("neural audio unavailable (network?); live-speed path not reached")
        assert not log.errors, f"live speed changes caused error(s): {log.errors}"
        print(f"\n[TTS-3] live speed changes OK; completed; chunks={len(seen)}")

    # 4) Offline path (pyttsx3 SAPI) — completes, no network, no MP3s -----
    def test_offline_sapi_no_network(self, eng):
        sapi = [vid for vid, _name in eng.get_voices() if vid.startswith("sapi:")]
        if not sapi:
            pytest.skip("no SAPI (offline) voice installed on this machine")
        eng.set_voice(sapi[0])
        assert eng._use_offline is True, "set_voice(sapi:...) did not select offline path"

        log, seen, t_first, total, ok = _drive_speak(
            eng, "This sentence is spoken by the offline system voice.", timeout=30
        )
        assert ok, f"offline speech never finished; status={log.status} errors={log.errors}"
        assert not log.errors, f"offline path error(s): {log.errors}"
        # Offline uses pyttsx3 — it must NOT create neural MP3s and must NOT emit 'Playing'.
        assert not seen, f"offline path unexpectedly produced neural MP3s: {seen}"
        assert t_first is None, "offline path unexpectedly used the neural VLC 'Playing' path"
        assert any("offline" in s.lower() for _t, s in log.status), (
            f"offline status not observed: {log.status}"
        )
        print(
            f"\n[TTS-4] offline SAPI completed in {total:.2f}s "
            f"(no network, zero MP3s); voice={sapi[0]}"
        )

    # 5) shutdown() removes the temp dir and is idempotent ----------------
    def test_shutdown_removes_temp_dir(self, qapp):
        from voiceassistant.tts import TTSEngine

        e = TTSEngine()
        tmp = e._temp_dir
        assert os.path.isdir(tmp), f"temp dir was not created: {tmp}"
        e.shutdown()
        assert not os.path.exists(tmp), f"temp dir not removed by shutdown(): {tmp}"
        e.shutdown()  # idempotent — a second teardown must not raise
        print(f"\n[TTS-5] shutdown() removed temp dir and was idempotent")


# --------------------------------------------------------------------------- #
# OCR live tests
# --------------------------------------------------------------------------- #
def _win_ocr_available():
    try:
        from winsdk.windows.media.ocr import OcrEngine

        return OcrEngine.try_create_from_user_profile_languages() is not None
    except Exception:
        return False


def _ocr_skip_reason():
    if not RUN_OCR_LIVE:
        return "set RUN_OCR_LIVE=1 to run the live OCR integration tests"
    if not _win_ocr_available():
        return "Windows OCR engine unavailable (winsdk / language pack)"
    return None


_OCR_SKIP = _ocr_skip_reason()


def _font(name, size):
    for candidate in (name, "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render(lines, font_name="segoeui.ttf", size=28, pad=24):
    """Render one or more lines of black text on white, sizing the canvas to fit
    (mirrors the style of tests/test_ocr_backend.py::_render)."""
    if isinstance(lines, str):
        lines = [lines]
    font = _font(font_name, size)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10), "white"))
    widths, heights = [], []
    for ln in lines:
        box = probe.textbbox((0, 0), ln or " ", font=font)
        widths.append(box[2] - box[0])
        heights.append(box[3] - box[1])
    line_h = max(heights) + 12
    width = max(widths) + pad * 2
    height = line_h * len(lines) + pad * 2
    img = Image.new("RGB", (max(width, 80), max(height, 48)), "white")
    draw = ImageDraw.Draw(img)
    y = pad
    for ln in lines:
        draw.text((pad, y), ln, fill="black", font=font)
        y += line_h
    return img


def _loaded_ocr_engine():
    from voiceassistant.ocr import OCREngine

    eng = OCREngine(backend="windows")
    eng._load_job()  # synchronous: runs the real native-load path on this thread
    assert eng.active_backend == "windows", "native OCR backend did not initialize"
    assert eng.describe() == "Windows native"
    return eng


@pytest.mark.skipif(_OCR_SKIP is not None, reason=_OCR_SKIP or "")
class TestOCRLive:
    # 6) Recall across fonts/sizes + multi-line + mock dialog, with latency
    def test_native_recall_and_latency(self):
        eng = _loaded_ocr_engine()
        cases = [
            # (label, font, size, lines, expected substrings [lowercase])
            ("segoe/prose", "segoeui.ttf", 30,
             ["The quarterly report is ready for review"],
             ["quarterly", "report", "review"]),
            ("arial/prose", "arial.ttf", 34,
             ["Meeting moved to Thursday afternoon"],
             ["meeting", "thursday"]),
            ("calibri/numbers", "calibri.ttf", 30,
             ["Total revenue increased by twelve percent"],
             ["revenue", "percent"]),
            ("consolas/code-multiline", "consola.ttf", 24,
             ["def compute_total(items):", "    return sum(items)"],
             ["compute", "return"]),
            ("segoe/mock-dialog", "segoeui.ttf", 28,
             ["Warning", "The document could not be saved.", "Retry        Cancel"],
             ["warning", "document", "saved", "cancel"]),
        ]

        # Warm up once so the cold-start cost is excluded from the latency metric.
        eng._read_windows(_render(["warm up"]))

        latencies = []
        for label, font_name, size, lines, expected in cases:
            img = _render(lines, font_name=font_name, size=size)
            t0 = time.perf_counter()
            text = eng._read_windows(img)
            dt = (time.perf_counter() - t0) * 1000.0
            latencies.append(dt)
            low = text.lower()
            missing = [w for w in expected if w not in low]
            assert not missing, f"[{label}] missing {missing} in OCR output {text!r}"
            assert dt < 200.0, f"[{label}] OCR read too slow: {dt:.1f}ms (native ~10ms)"

        print(
            f"\n[OCR-6] {len(latencies)} reads recalled; latency "
            f"min={min(latencies):.1f}ms median={statistics.median(latencies):.1f}ms "
            f"max={max(latencies):.1f}ms"
        )

    # 7) One REAL screen capture -> native OCR (prove capture->OCR on live pixels)
    def test_real_screen_capture_path(self):
        from voiceassistant.ocr import ScreenCapture

        eng = _loaded_ocr_engine()
        img = ScreenCapture().capture_region(0, 0, 400, 120)
        # Real pixels of the requested physical size came back.
        assert img.size == (400, 120), f"capture returned wrong dims: {img.size}"
        assert img.mode == "RGB", f"unexpected image mode: {img.mode}"

        t0 = time.perf_counter()
        text = eng._read_windows(img)  # content may be empty; must not raise
        dt = (time.perf_counter() - t0) * 1000.0
        assert isinstance(text, str)
        print(
            f"\n[OCR-7] real 400x120 capture -> {len(text)} chars in {dt:.1f}ms; "
            f"sample={text[:60]!r}"
        )
