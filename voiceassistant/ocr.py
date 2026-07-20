"""Screen capture + OCR + region-selection overlay.

OCR backends (config "ocr_backend"):
  * "auto"/"windows" — Windows.Media.Ocr, the OS-native engine (what PowerToys
    Text Extractor uses). Measured on this app's screen-text corpus: equal or
    better recall than EasyOCR on every case at 8-30x the speed (≈10ms/read),
    with ZERO model download and no PyTorch. This is the default.
  * "easyocr" — the former engine, kept as an automatic fallback when the
    native engine is unavailable (no language pack / winsdk missing) or when
    explicitly configured. Requires the ~3GB torch stack.

Phase 3 changes from the old screen_reader.py:
  * one owned SerialWorker for model load + reads (threading law);
  * the process-global sys.stdout/stderr swap is GONE — it raced the Whisper
    loader on another thread and silently no-op'd under pythonw. EasyOCR's
    progress output is suppressed with its own verbose=False instead.
"""

import asyncio

import numpy as np
from PIL import Image
import mss
import mss.tools
from PySide6.QtCore import QObject, Signal, Qt, QRect
from PySide6.QtGui import QPainter, QColor, QPen, QGuiApplication
from PySide6.QtWidgets import QWidget

from . import winapi
from .workers import SerialWorker


class ScreenCapture:
    """Captures screen regions using mss."""

    def __init__(self):
        self._sct = mss.mss()

    def _virtual_bounds(self):
        monitors = self._sct.monitors[1:] or [self._sct.monitors[0]]
        left = min(m["left"] for m in monitors)
        top = min(m["top"] for m in monitors)
        right = max(m["left"] + m["width"] for m in monitors)
        bottom = max(m["top"] + m["height"] for m in monitors)
        return left, top, right, bottom

    def capture_around_cursor(self, width=600, height=300):
        """Capture a region centered on the current mouse cursor.

        Uses the PHYSICAL cursor position (winapi.get_cursor_pos) — mss
        captures in physical pixels, and Qt's logical coordinates were off by
        the scale factor on 125%/150% DPI displays (wrong region OCR'd).
        """
        cx, cy = winapi.get_cursor_pos()
        left, top, right, bottom = self._virtual_bounds()
        width = min(width, right - left)
        height = min(height, bottom - top)
        x = max(left, min(cx - width // 2, right - width))
        y = max(top, min(cy - height // 2, bottom - height))

        monitor = {"left": x, "top": y, "width": width, "height": height}
        screenshot = self._sct.grab(monitor)
        return Image.frombytes("RGB", screenshot.size, screenshot.rgb)

    def capture_region(self, x, y, w, h):
        """Capture a specific screen region."""
        monitor = {"left": x, "top": y, "width": w, "height": h}
        screenshot = self._sct.grab(monitor)
        return Image.frombytes("RGB", screenshot.size, screenshot.rgb)


class OCREngine(QObject):
    """Screen-text recognition: Windows-native engine, EasyOCR fallback."""

    model_loading = Signal(str)
    model_ready = Signal()
    text_ready = Signal(str)
    error = Signal(str)

    def __init__(self, languages=None, gpu=True, backend="auto"):
        super().__init__()
        self.languages = languages or ["en"]
        self.gpu = gpu
        self.backend = backend  # "auto" | "windows" | "easyocr"
        self.active_backend = None  # set once loaded
        self._win_engine = None
        self._reader = None  # easyocr
        self._worker = SerialWorker("ocr")

    @property
    def is_loaded(self):
        return self._win_engine is not None or self._reader is not None

    def describe(self):
        if self.active_backend == "windows":
            return "Windows native"
        if self.active_backend == "easyocr":
            return f"EasyOCR {'GPU' if self.gpu else 'CPU'}"
        return "not loaded"

    def load_model(self):
        """Initialize the OCR backend on the OCR worker."""
        self._worker.submit(self._load_job)

    def _load_job(self):
        from . import applog

        if self.backend in ("auto", "windows"):
            try:
                self.model_loading.emit("Loading OCR engine...")
                # WinRT needs a COM apartment on THIS thread. All native-OCR
                # calls (load + reads) run on this one SerialWorker thread,
                # so a single init here covers the engine's whole lifetime.
                try:
                    import winsdk._winrt as _winrt

                    _winrt.init_apartment()
                except Exception:
                    pass
                from winsdk.windows.media.ocr import OcrEngine as _WinOcr

                engine = _WinOcr.try_create_from_user_profile_languages()
                if engine is not None:
                    self._win_engine = engine
                    self.active_backend = "windows"
                    applog.info("OCR: Windows native engine ready")
                    self.model_ready.emit()
                    return
                applog.error("OCR: no Windows OCR language pack; falling back")
            except Exception as e:
                applog.error(f"OCR: Windows engine unavailable ({e}); falling back")
            if self.backend == "windows":
                self.error.emit("Windows OCR unavailable (install a language pack)")
                return

        # EasyOCR path (explicit backend, or fallback from auto)
        try:
            self.model_loading.emit("Loading OCR engine (EasyOCR)...")
            import easyocr

            # verbose=False suppresses the Unicode progress bars that crashed
            # on cp1252 consoles — no more swapping process-global stdio.
            self._reader = easyocr.Reader(self.languages, gpu=self.gpu, verbose=False)
            self.active_backend = "easyocr"
            self.model_ready.emit()
        except Exception as e:
            applog.error(f"GPU OCR load failed ({e}); trying CPU")
            try:
                self.model_loading.emit("GPU OCR failed, trying CPU...")
                import easyocr

                self._reader = easyocr.Reader(self.languages, gpu=False, verbose=False)
                self.gpu = False
                self.active_backend = "easyocr"
                self.model_ready.emit()
            except Exception as e2:
                self.error.emit(f"OCR load failed: {e2}")

    def read_image(self, pil_image):
        """Run OCR on a PIL image on the OCR worker."""
        if not self.is_loaded:
            self.error.emit("OCR engine not loaded yet")
            return
        self._worker.submit(self._read_job, pil_image)

    def _read_job(self, pil_image):
        try:
            if self.active_backend == "windows":
                text = self._read_windows(pil_image)
            else:
                text = self._read_easyocr(pil_image)
            self.text_ready.emit(text if text else "[No text detected in region]")
        except Exception as e:
            self.error.emit(f"OCR error: {e}")

    def _read_windows(self, pil_image):
        from winsdk.windows.graphics.imaging import (
            BitmapPixelFormat, SoftwareBitmap,
        )
        from winsdk.windows.storage.streams import Buffer

        # The native engine caps image dimensions — downscale oversized grabs.
        # (max_image_dimension is a STATIC property on the OcrEngine class.)
        from winsdk.windows.media.ocr import OcrEngine as _WinOcr

        max_dim = int(getattr(_WinOcr, "max_image_dimension", 0) or 0) or 4096
        img = pil_image
        if img.width > max_dim or img.height > max_dim:
            img = img.copy()
            img.thumbnail((max_dim, max_dim))

        rgba = img.convert("RGBA").tobytes()
        buf = Buffer(len(rgba))
        buf.length = len(rgba)
        with memoryview(buf) as mv:
            mv[:] = rgba
        bmp = SoftwareBitmap.create_copy_from_buffer(
            buf, BitmapPixelFormat.RGBA8, img.width, img.height
        )

        async def _recognize():
            return await self._win_engine.recognize_async(bmp)

        result = asyncio.run(_recognize())
        lines = [line.text for line in result.lines]
        return "\n".join(lines).strip()

    def _read_easyocr(self, pil_image):
        img_array = np.array(pil_image)
        results = self._reader.readtext(img_array)
        lines = [text for (_, text, conf) in results if conf > 0.3]
        return "\n".join(lines).strip()

    def shutdown(self):
        """Stop the OCR worker (bounded). Called on app exit."""
        self._worker.shutdown()


class RegionSelector(QWidget):
    """Transparent fullscreen overlay for selecting a screen region by click-drag."""

    region_selected = Signal(int, int, int, int)  # x, y, width, height
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._start_pos = None
        self._current_pos = None
        self._is_selecting = False

    def activate(self):
        """Show the selector covering all screens."""
        screen_geo = QGuiApplication.primaryScreen().geometry()
        for screen in QGuiApplication.screens():
            screen_geo = screen_geo.united(screen.geometry())
        self.setGeometry(screen_geo)
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))

        if self._start_pos and self._current_pos:
            rect = QRect(self._start_pos, self._current_pos).normalized()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, QColor(0, 0, 0, 0))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QPen(QColor(0, 170, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Qt logical coords drive the on-screen drawing; the PHYSICAL
            # cursor position (same space as mss) drives the emitted region —
            # this is what makes selection correct on scaled/mixed-DPI setups.
            self._start_pos = event.globalPosition().toPoint()
            self._current_pos = self._start_pos
            self._start_phys = winapi.get_cursor_pos()
            self._is_selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._is_selecting:
            self._current_pos = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._is_selecting:
            self._is_selecting = False
            end_phys = winapi.get_cursor_pos()
            sx, sy = getattr(self, "_start_phys", end_phys)
            ex, ey = end_phys
            x, y = min(sx, ex), min(sy, ey)
            w, h = abs(ex - sx), abs(ey - sy)
            self.hide()
            if w > 10 and h > 10:
                self.region_selected.emit(x, y, w, h)
            else:
                self.cancelled.emit()
            self._start_pos = None
            self._current_pos = None
            self._start_phys = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            # Fully reset the selection state machine. Clearing only the
            # positions (not _is_selecting / _start_phys) left an in-progress
            # drag "live": the implicit mouse grab still delivers the eventual
            # button-up to this hidden widget, so mouseReleaseEvent would fire
            # a SECOND signal (region_selected from stale coords) after we
            # already emitted cancelled — an unwanted OCR read of the wrong
            # region right after the user pressed Esc to cancel.
            self._is_selecting = False
            self.hide()
            self.cancelled.emit()
            self._start_pos = None
            self._current_pos = None
            self._start_phys = None
