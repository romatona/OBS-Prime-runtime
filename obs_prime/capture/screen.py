from __future__ import annotations
from ..models import CaptureFrame
from .providers import CaptureProvider


class ScreenCaptureProvider(CaptureProvider):
    def __init__(self, monitor_index: int = 0) -> None:
        self.monitor_index = monitor_index

    def capture(self) -> CaptureFrame:
        try:
            image = self._capture_with_mss()
            if image is None:
                from PIL import ImageGrab

                image = ImageGrab.grab()
            return CaptureFrame("screen", None, image.width, image.height, image)
        except Exception as exc:
            raise RuntimeError(f"screen capture unavailable: {exc}") from exc

    def _capture_with_mss(self):
        if self.monitor_index < 0:
            raise ValueError("monitor index must be non-negative")
        try:
            import mss  # type: ignore
        except Exception:
            return None
        with mss.mss() as sct:
            monitors = sct.monitors
            index = self.monitor_index + 1
            if index <= 0 or index >= len(monitors):
                raise ValueError(f"monitor index {self.monitor_index} is out of range")
            monitor = monitors[index]
            screenshot = sct.grab(monitor)
            from PIL import Image
            return Image.frombytes("RGB", screenshot.size, screenshot.rgb)
