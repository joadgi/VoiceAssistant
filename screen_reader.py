"""Screen capture and OCR engine for reading on-screen text."""

import threading
import numpy as np
from PIL import Image
import mss
import mss.tools
from PySide6.QtCore import QObject, Signal, Qt, QPoint, QRect
from PySide6.QtGui import QPainter, QColor, QPen, QCursor, QGuiApplication
from PySide6.QtWidgets import QWidget


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
        """Capture a region centered on the current mouse cursor."""
        cursor_pos = QCursor.pos()
        left, top, right, bottom = self._virtual_bounds()
        width = min(width, right - left)
        height = min(height, bottom - top)
        x = max(left, min(cursor_pos.x() - width // 2, right - width))
        y = max(top, min(cursor_pos.y() - height // 2, bottom - height))

        monitor = {
            "left": x,
            "top": y,
            "width": width,
            "height": height,
        }

        screenshot = self._sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        return img

    def capture_region(self, x, y, w, h):
        """Capture a specific screen region."""
        monitor = {"left": x, "top": y, "width": w, "height": h}
        screenshot = self._sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        return img

class OCREngine(QObject):
    """EasyOCR-based text recognition."""

    model_loading = Signal(str)
    model_ready = Signal()
    text_ready = Signal(str)
    error = Signal(str)

    def __init__(self, languages=None, gpu=True):
        super().__init__()
        self.languages = languages or ["en"]
        self.gpu = gpu
        self._reader = None
        self._lock = threading.Lock()

    @property
    def is_loaded(self):
        return self._reader is not None

    def load_model(self):
        """Load the EasyOCR model in a background thread."""
        thread = threading.Thread(target=self._load_worker, daemon=True)
        thread.start()

    def _load_worker(self):
        import sys, io

        # EasyOCR prints Unicode progress bars that crash on Windows cp1252
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
        except Exception:
            pass

        try:
            self.model_loading.emit("Loading OCR engine...")
            import easyocr

            self._reader = easyocr.Reader(self.languages, gpu=self.gpu)
            self.model_ready.emit()
        except Exception as e:
            try:
                self.model_loading.emit("GPU OCR failed, trying CPU...")
                import easyocr

                self._reader = easyocr.Reader(self.languages, gpu=False)
                self.gpu = False
                self.model_ready.emit()
            except Exception as e2:
                self.error.emit(f"OCR load failed: {e2}")
        finally:
            try:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            except Exception:
                pass

    def read_image(self, pil_image):
        """Run OCR on a PIL image in a background thread."""
        if not self.is_loaded:
            self.error.emit("OCR engine not loaded yet")
            return
        thread = threading.Thread(
            target=self._read_worker, args=(pil_image,), daemon=True
        )
        thread.start()

    def _read_worker(self, pil_image):
        try:
            with self._lock:
                img_array = np.array(pil_image)
                results = self._reader.readtext(img_array)
                lines = [text for (_, text, conf) in results if conf > 0.3]
                if lines:
                    self.text_ready.emit("\n".join(lines))
                else:
                    self.text_ready.emit("[No text detected in region]")
        except Exception as e:
            self.error.emit(f"OCR error: {e}")


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
        # Semi-transparent dark overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))

        if self._start_pos and self._current_pos:
            rect = QRect(self._start_pos, self._current_pos).normalized()
            # Clear the selected region (make it transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, QColor(0, 0, 0, 0))
            # Draw border around selection
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QPen(QColor(0, 170, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_pos = event.globalPosition().toPoint()
            self._current_pos = self._start_pos
            self._is_selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._is_selecting:
            self._current_pos = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._is_selecting:
            self._is_selecting = False
            end_pos = event.globalPosition().toPoint()
            rect = QRect(self._start_pos, end_pos).normalized()
            self.hide()
            if rect.width() > 10 and rect.height() > 10:
                self.region_selected.emit(rect.x(), rect.y(), rect.width(), rect.height())
            else:
                self.cancelled.emit()
            self._start_pos = None
            self._current_pos = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            self.cancelled.emit()
            self._start_pos = None
            self._current_pos = None
