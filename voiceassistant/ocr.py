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
from PySide6.QtCore import QObject, Signal, Qt, QRectF
from PySide6.QtGui import QCursor, QPainter, QColor, QPen, QGuiApplication, QScreen
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
    # Loaded, but slower/worse than requested (EasyOCR instead of the
    # ~10ms native engine, or CPU instead of GPU). Surfaced, not just logged.
    degraded = Signal(str)
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
                self.degraded.emit(
                    "Screen reader fell back to EasyOCR: no Windows OCR language "
                    "pack installed. OCR will be slower and needs the torch stack."
                )
            except Exception as e:
                applog.error(f"OCR: Windows engine unavailable ({e}); falling back")
                self.degraded.emit(
                    "Screen reader fell back to EasyOCR: the fast Windows OCR "
                    "engine is unavailable. See debug.log."
                )
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
                self.degraded.emit(
                    "Screen reader OCR is running on the CPU, not the GPU - "
                    "captures will be noticeably slower."
                )
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


class RegionSelector(QObject):
    """Click-drag region picker that spans EVERY monitor.

    One translucent overlay per screen, never one window stretched over the
    virtual desktop. The old single overlay called showFullScreen(), which
    makes a window full-screen on the ONE monitor it lands on and discards the
    all-monitor geometry set just before it. Measured 2026-11-10 on a 2x2 grid
    of 3440x1440 monitors: it covered 1 of 4, so a drag on any other monitor
    went straight through and selected text in the app underneath. A single
    window also cannot span monitors with different scale factors.

    The selection lives in PHYSICAL pixels (winapi.get_cursor_pos), the space
    mss captures in, so the emitted rectangle is right on scaled and mixed-DPI
    setups. Each overlay maps it into its own logical coordinates to draw.
    A drag may cross monitors: Qt's implicit grab keeps delivering to the
    overlay the press landed on, and every overlay draws its share.
    """

    region_selected = Signal(int, int, int, int)  # x, y, width, height (physical)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self._overlays = []
        self._reset()

    def _reset(self):
        self._start_phys = None
        self._current_phys = None
        self._is_selecting = False

    @staticmethod
    def _screens():
        return QGuiApplication.screens()

    @property
    def is_active(self):
        """True while the overlays are up, waiting for (or in) a drag."""
        return bool(self._overlays)

    def activate(self):
        """Cover every screen with its own overlay and wait for a drag."""
        self._close_overlays()
        self._reset()
        for screen in self._screens():
            overlay = _ScreenOverlay(self, screen)
            overlay.show()
            overlay.raise_()
            self._overlays.append(overlay)
        # Keyboard focus (for Esc) goes to the overlay under the pointer.
        under = QGuiApplication.screenAt(QCursor.pos())
        focus = next((o for o in self._overlays if o.screen() is under), None)
        focus = focus or (self._overlays[0] if self._overlays else None)
        if focus is not None:
            focus.activateWindow()
            focus.setFocus()

    def selection(self):
        """The current drag as a normalized PHYSICAL (x0, y0, x1, y1), or None."""
        if not (self._is_selecting and self._start_phys and self._current_phys):
            return None
        (sx, sy), (ex, ey) = self._start_phys, self._current_phys
        return min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey)

    def _begin(self, phys):
        self._start_phys = phys
        self._current_phys = phys
        self._is_selecting = True
        self._repaint()

    def _drag(self, phys):
        if self._is_selecting:
            self._current_phys = phys
            self._repaint()

    def _finish(self, phys):
        if not self._is_selecting:
            return  # cancelled mid-drag: the implicit-grab button-up still arrives
        self._current_phys = phys
        x0, y0, x1, y1 = self.selection()
        self._close_overlays()   # off the screen BEFORE the slot captures it
        self._reset()
        w, h = x1 - x0, y1 - y0
        if w > 10 and h > 10:
            self.region_selected.emit(x0, y0, w, h)
        else:
            self.cancelled.emit()

    def cancel(self):
        """Esc or a right-click on any monitor. Fully resets the state machine:
        clearing only the positions left a drag "live", and the implicit-grab
        button-up then fired a SECOND signal (an OCR of stale coords) after the
        user had already cancelled."""
        self._close_overlays()
        self._reset()
        self.cancelled.emit()

    def _repaint(self):
        for overlay in self._overlays:
            overlay.update()

    def _close_overlays(self):
        for overlay in self._overlays:
            overlay.hide()
            overlay.deleteLater()
        self._overlays = []


class _ScreenOverlay(QWidget):
    """One monitor's share of the region selector: dims it and draws the
    part of the (physical) selection that falls on it."""

    def __init__(self, owner, screen):
        super().__init__()
        self._owner = owner
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        if isinstance(screen, QScreen):
            self.setScreen(screen)   # scale factor first, then the geometry
        self.setGeometry(screen.geometry())

    def physical_rect(self):
        """This overlay's (left, top, right, bottom) in physical pixels."""
        if QGuiApplication.platformName() == "windows" and self.isVisible():
            rect = winapi.get_window_rect(int(self.winId()))
            if rect:
                return rect
        g, ratio = self.geometry(), self.devicePixelRatioF()
        return (g.x(), g.y(),
                g.x() + round(g.width() * ratio), g.y() + round(g.height() * ratio))

    def to_local(self, phys):
        """Map a physical (x0, y0, x1, y1) into this overlay's logical coords."""
        left, top, right, bottom = self.physical_rect()
        sx = self.width() / max(1, right - left)
        sy = self.height() / max(1, bottom - top)
        x0, y0, x1, y1 = phys
        return QRectF((x0 - left) * sx, (y0 - top) * sy, (x1 - x0) * sx, (y1 - y0) * sy)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))
        sel = self._owner.selection()
        if sel is not None:
            rect = self.to_local(sel)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, QColor(0, 0, 0, 0))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(0, 170, 255), 2))
            painter.drawRect(rect)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._begin(winapi.get_cursor_pos())
        elif event.button() == Qt.MouseButton.RightButton:
            self._owner.cancel()

    def mouseMoveEvent(self, event):
        self._owner._drag(winapi.get_cursor_pos())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._finish(winapi.get_cursor_pos())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._owner.cancel()
