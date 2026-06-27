from __future__ import annotations

from abc import ABC, abstractmethod
import os
import time

from ..models import CaptureFrame, OcrSlotResult, Rect
from .preprocess import preprocess_slot_image
from .tesseract_runtime import configure_pytesseract


class OcrProvider(ABC):
    name = "base"

    @abstractmethod
    def read_slot(self, frame: CaptureFrame, slot_index: int, rect: Rect) -> OcrSlotResult:
        raise NotImplementedError

    @abstractmethod
    def read_slots(self, frame: CaptureFrame, slot_rects: list[Rect]) -> list[OcrSlotResult]:
        raise NotImplementedError


class TesseractOcrProvider(OcrProvider):
    name = "tesseract"

    def __init__(self, language: str = "kor+eng", timeout_ms: int = 2500, preprocessing_preset: str = "default-korean-ui") -> None:
        self.language = language
        self.timeout_ms = timeout_ms
        self.preprocessing_preset = preprocessing_preset
        self._runtime_checked = False
        self._runtime_error = ""

    def read_slot(self, frame: CaptureFrame, slot_index: int, rect: Rect) -> OcrSlotResult:
        try:
            import pytesseract
        except Exception as exc:
            raise RuntimeError("tesseract provider requires pytesseract") from exc
        if not self._runtime_checked:
            runtime = configure_pytesseract(pytesseract, self.language, self.timeout_ms)
            self._runtime_checked = True
            self._runtime_error = runtime.error if runtime.status != "ready" else ""
        if self._runtime_error:
            raise RuntimeError(f"tesseract runtime unavailable: {self._runtime_error}")
        if frame.image is None:
            raise RuntimeError("tesseract provider requires an image frame")
        crop = frame.image.crop((rect.x, rect.y, rect.x + rect.w, rect.y + rect.h))
        crop = preprocess_slot_image(crop, self.preprocessing_preset)
        text = pytesseract.image_to_string(
            crop,
            lang=self.language,
            config="--oem 1 --psm 6",
            timeout=self.timeout_ms / 1000,
        ).strip()
        return OcrSlotResult(slot_index, text, 0.70 if text else 0.0, rect)

    def read_slots(self, frame: CaptureFrame, slot_rects: list[Rect]) -> list[OcrSlotResult]:
        results: list[OcrSlotResult] = []
        deadline = time.monotonic() + max(1, self.timeout_ms) / 1000
        for index, rect in enumerate(slot_rects, start=1):
            if time.monotonic() >= deadline:
                results.append(OcrSlotResult(index, "", 0.0, rect, error="OCR 전체 제한 시간 초과"))
                continue
            original_timeout = self.timeout_ms
            self.timeout_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                results.append(self.read_slot(frame, index, rect))
            except Exception as exc:
                results.append(OcrSlotResult(index, "", 0.0, rect, error=str(exc)))
            finally:
                self.timeout_ms = original_timeout
        return results


class WindowsOcrProvider(OcrProvider):
    name = "windows_ocr"

    def __init__(self, language: str = "kor+eng", timeout_ms: int = 2500) -> None:
        self.language = language
        self.timeout_ms = timeout_ms

    def read_slot(self, frame: CaptureFrame, slot_index: int, rect: Rect) -> OcrSlotResult:
        if os.name != "nt":
            raise RuntimeError("windows_ocr provider is Windows-only")
        try:
            import winrt.windows.globalization  # noqa: F401
            import winrt.windows.media.ocr  # noqa: F401
        except Exception as exc:
            raise RuntimeError("windows_ocr provider is not available in this environment") from exc
        try:
            from PIL import Image  # noqa: F401
        except Exception as exc:
            raise RuntimeError("windows_ocr provider requires pillow to rasterize crop images") from exc
        raise RuntimeError("windows_ocr provider is intentionally unavailable in this MVP environment")

    def read_slots(self, frame: CaptureFrame, slot_rects: list[Rect]) -> list[OcrSlotResult]:
        return [self.read_slot(frame, index, rect) for index, rect in enumerate(slot_rects, start=1)]


def build_ocr_provider(name: str, language: str, timeout_ms: int, preprocessing_preset: str = "default-korean-ui") -> OcrProvider:
    if name == "tesseract":
        return TesseractOcrProvider(language, timeout_ms, preprocessing_preset)
    if name == "windows_ocr":
        return WindowsOcrProvider(language, timeout_ms)
    raise ValueError(f"unsupported OCR provider: {name}")
