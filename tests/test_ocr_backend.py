"""Native-OCR backend integration test — real engine, rendered image, ~10ms.

Calls the backend synchronously (no Qt event loop in pytest, so cross-thread
queued signals can't deliver here; the worker/signal plumbing is covered by
the fault-injection suite). Skips cleanly when winsdk or the OCR language
pack is absent.
"""

import os
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _win_ocr_available():
    try:
        from winsdk.windows.media.ocr import OcrEngine

        return OcrEngine.try_create_from_user_profile_languages() is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _win_ocr_available(), reason="Windows OCR engine unavailable"
)


def _render(text):
    img = Image.new("RGB", (700, 120), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("segoeui.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    d.text((16, 40), text, fill="black", font=font)
    return img


def _loaded_engine():
    from voiceassistant.ocr import OCREngine

    eng = OCREngine(backend="windows")
    eng._load_job()  # synchronous: runs the real load path on this thread
    assert eng.active_backend == "windows", "native backend did not initialize"
    assert eng.describe() == "Windows native"
    return eng


def test_windows_backend_reads_screen_text():
    eng = _loaded_engine()
    text = eng._read_windows(_render("The quarterly report is ready for review"))
    assert "quarterly report" in text.lower(), f"bad OCR output: {text!r}"


def test_empty_image_reads_empty():
    eng = _loaded_engine()
    text = eng._read_windows(Image.new("RGB", (300, 100), "white"))
    assert text == "", f"expected empty, got {text!r}"


def test_oversized_image_downscaled_not_crashed():
    eng = _loaded_engine()
    big = Image.new("RGB", (6000, 300), "white")
    d = ImageDraw.Draw(big)
    try:
        font = ImageFont.truetype("segoeui.ttf", 80)
    except OSError:
        font = ImageFont.load_default()
    d.text((40, 80), "HELLO WORLD", fill="black", font=font)
    text = eng._read_windows(big)  # must downscale, not raise
    assert "hello" in text.lower()
