from __future__ import annotations

from abc import ABC, abstractmethod
import os
from pathlib import Path
import time
import uuid
from typing import Any

from ..models import CaptureFrame, OcrSlotResult, Rect
from ..paths import PROJECT_ROOT
from .paddleocr_runtime import paddle_cache_environment
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


class PaddleOcrV5Provider(OcrProvider):
    name = "paddleocr_v5"

    def __init__(self, language: str = "kor+eng", timeout_ms: int = 2500, preprocessing_preset: str = "default-korean-ui") -> None:
        self.language = language
        self.timeout_ms = timeout_ms
        self.preprocessing_preset = preprocessing_preset
        self._ocr = None

    def read_slot(self, frame: CaptureFrame, slot_index: int, rect: Rect) -> OcrSlotResult:
        if frame.image is None:
            raise RuntimeError("paddleocr_v5 provider requires an image frame")
        crop = frame.image.crop((rect.x, rect.y, rect.x + rect.w, rect.y + rect.h))
        result = self._predict_crop(crop)
        text, confidence = _extract_paddle_text(result)
        return OcrSlotResult(slot_index, text.strip(), confidence if text.strip() else 0.0, rect)

    def read_slots(self, frame: CaptureFrame, slot_rects: list[Rect]) -> list[OcrSlotResult]:
        if frame.image is None:
            raise RuntimeError("paddleocr_v5 provider requires an image frame")
        strip, offsets, slot_height = _build_vertical_slot_strip(frame.image, slot_rects)
        result = self._predict_image(strip)
        return _slot_results_from_paddle_result(result, slot_rects, offsets, slot_height)

    def _predict_crop(self, crop) -> Any:
        return self._predict_image(crop)

    def _predict_image(self, image) -> Any:
        ocr = self._pipeline()
        crop_path = _write_temp_crop(image)
        try:
            if hasattr(ocr, "predict"):
                return ocr.predict(str(crop_path))
            if hasattr(ocr, "ocr"):
                return ocr.ocr(str(crop_path), cls=False)
            raise RuntimeError("paddleocr object has no predict or ocr method")
        finally:
            try:
                crop_path.unlink()
            except OSError:
                pass

    def _pipeline(self):
        if self._ocr is not None:
            return self._ocr
        with paddle_cache_environment():
            try:
                from paddleocr import PaddleOCR
            except Exception as exc:
                raise RuntimeError("paddleocr_v5 provider requires paddleocr and paddlepaddle") from exc
            language = _paddle_language(self.language)
            try:
                self._ocr = PaddleOCR(
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    enable_mkldnn=False,
                )
            except TypeError:
                self._ocr = PaddleOCR(lang=language, use_angle_cls=False, show_log=False)
        return self._ocr


def build_ocr_provider(name: str, language: str, timeout_ms: int, preprocessing_preset: str = "default-korean-ui") -> OcrProvider:
    if name == "tesseract":
        return TesseractOcrProvider(language, timeout_ms, preprocessing_preset)
    if name == "paddleocr_v5":
        return PaddleOcrV5Provider(language, timeout_ms, preprocessing_preset)
    if name == "windows_ocr":
        return WindowsOcrProvider(language, timeout_ms)
    raise ValueError(f"unsupported OCR provider: {name}")


def _paddle_language(language: str) -> str:
    parts = {part.strip().lower() for part in str(language or "").replace(",", "+").split("+") if part.strip()}
    if "korean" in parts or "kor" in parts or "ko" in parts:
        return "korean"
    return "korean"


def _write_temp_crop(image) -> Path:
    tmp_dir = PROJECT_ROOT / "runtime" / "paddleocr_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"slot-{uuid.uuid4().hex}.png"
    image.save(path)
    return path


def _build_vertical_slot_strip(image, slot_rects: list[Rect]):
    from PIL import Image

    crops = [image.crop((rect.x, rect.y, rect.x + rect.w, rect.y + rect.h)) for rect in slot_rects]
    max_width = max(crop.width for crop in crops)
    slot_height = max(crop.height for crop in crops)
    gap = max(8, int(slot_height * 0.08))
    strip_height = sum(crop.height for crop in crops) + gap * max(0, len(crops) - 1)
    strip = Image.new("RGB", (max_width, strip_height), (14, 16, 18))
    offsets: list[int] = []
    y = 0
    for crop in crops:
        offsets.append(y)
        strip.paste(crop.convert("RGB"), (0, y))
        y += crop.height + gap
    return strip, offsets, slot_height


def _slot_results_from_paddle_result(
    result: Any,
    slot_rects: list[Rect],
    slot_offsets: list[int],
    slot_height: int | list[int],
) -> list[OcrSlotResult]:
    heights = [slot_height] * len(slot_rects) if isinstance(slot_height, int) else slot_height
    buckets: list[list[tuple[float, float, str, float]]] = [[] for _ in slot_rects]
    for text, score, x_center, y_center in _paddle_text_segments(result):
        if not text.strip():
            continue
        slot_index = min(
            range(len(slot_rects)),
            key=lambda index: abs(y_center - (slot_offsets[index] + heights[index] / 2)),
        )
        buckets[slot_index].append((y_center, x_center, text.strip(), score))
    results: list[OcrSlotResult] = []
    for index, rect in enumerate(slot_rects):
        rows = sorted(buckets[index], key=lambda row: row[0])
        text = _join_paddle_slot_rows(rows, max(8.0, heights[index] * 0.12))
        scores = [row[3] for row in rows if 0.0 <= row[3] <= 1.0]
        confidence = sum(scores) / len(scores) if scores else (0.70 if text else 0.0)
        results.append(OcrSlotResult(index + 1, text, float(max(0.0, min(1.0, confidence))), rect))
    return results


def _join_paddle_slot_rows(rows: list[tuple[float, float, str, float]], line_threshold: float) -> str:
    lines: list[str] = []
    current: list[tuple[float, float, str, float]] = []
    current_y: float | None = None
    for row in rows:
        y_center = row[0]
        if current_y is None or abs(y_center - current_y) <= line_threshold:
            current.append(row)
            current_y = y_center if current_y is None else (current_y + y_center) / 2
            continue
        lines.append(" ".join(part[2] for part in sorted(current, key=lambda part: part[1])))
        current = [row]
        current_y = y_center
    if current:
        lines.append(" ".join(part[2] for part in sorted(current, key=lambda part: part[1])))
    return "\n".join(line for line in lines if line)


def _paddle_text_segments(value: Any) -> list[tuple[str, float, float, float]]:
    segments: list[tuple[str, float, float, float]] = []
    _collect_paddle_segments(value, segments)
    return segments


def _collect_paddle_segments(value: Any, segments: list[tuple[str, float, float, float]]) -> None:
    if value is None or isinstance(value, str):
        return
    if hasattr(value, "json"):
        try:
            _collect_paddle_segments(value.json, segments)
            return
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            _collect_paddle_segments(value.to_dict(), segments)
            return
        except Exception:
            pass
    if isinstance(value, dict):
        texts = value.get("rec_texts") or value.get("texts")
        boxes = value.get("rec_boxes") or value.get("rec_polys") or value.get("dt_polys")
        scores = value.get("rec_scores") or value.get("scores") or []
        if isinstance(texts, list) and boxes is not None:
            box_rows = _as_list(boxes)
            score_rows = _as_list(scores)
            for index, text in enumerate(texts):
                if not isinstance(text, str) or index >= len(box_rows):
                    continue
                center = _box_center(box_rows[index])
                if center is None:
                    continue
                x_center, y_center = center
                score = _score_at(score_rows, index)
                segments.append((text, score, x_center, y_center))
        for key in ("res", "result", "data"):
            if key in value:
                _collect_paddle_segments(value.get(key), segments)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_paddle_segments(item, segments)


def _as_list(value: Any) -> list[Any]:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _score_at(scores: list[Any], index: int) -> float:
    if index >= len(scores):
        return 0.70
    try:
        score = float(scores[index])
    except (TypeError, ValueError):
        return 0.70
    return score if 0.0 <= score <= 1.0 else 0.70


def _box_center(box: Any) -> tuple[float, float] | None:
    values = _as_list(box)
    if len(values) == 4 and all(isinstance(value, (int, float)) for value in values):
        return (float(values[0]) + float(values[2])) / 2, (float(values[1]) + float(values[3])) / 2
    x_values: list[float] = []
    y_values: list[float] = []
    for point in values:
        point_values = _as_list(point)
        if len(point_values) >= 2 and isinstance(point_values[1], (int, float)):
            if isinstance(point_values[0], (int, float)):
                x_values.append(float(point_values[0]))
            y_values.append(float(point_values[1]))
    if not x_values or not y_values:
        return None
    return sum(x_values) / len(x_values), sum(y_values) / len(y_values)


def _extract_paddle_text(result: Any) -> tuple[str, float]:
    texts: list[str] = []
    scores: list[float] = []
    _collect_paddle_text(result, texts, scores)
    text = "\n".join(part for part in texts if part)
    confidence = sum(scores) / len(scores) if scores else (0.70 if text else 0.0)
    return text, float(max(0.0, min(1.0, confidence)))


def _collect_paddle_text(value: Any, texts: list[str], scores: list[float]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        return
    if hasattr(value, "json"):
        try:
            _collect_paddle_text(value.json, texts, scores)
            return
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            _collect_paddle_text(value.to_dict(), texts, scores)
            return
        except Exception:
            pass
    if isinstance(value, dict):
        for key in ("rec_text", "text"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        rec_texts = value.get("rec_texts") or value.get("texts")
        if isinstance(rec_texts, list):
            for text in rec_texts:
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        for key in ("rec_score", "score", "confidence"):
            _collect_score(value.get(key), scores)
        rec_scores = value.get("rec_scores") or value.get("scores")
        if isinstance(rec_scores, list):
            for score in rec_scores:
                _collect_score(score, scores)
        for key in ("res", "result", "data"):
            if key in value:
                _collect_paddle_text(value.get(key), texts, scores)
        return
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and isinstance(value[1], tuple) and len(value[1]) >= 2 and isinstance(value[1][0], str):
            if value[1][0].strip():
                texts.append(value[1][0].strip())
            _collect_score(value[1][1], scores)
            return
        for item in value:
            _collect_paddle_text(item, texts, scores)


def _collect_score(value: Any, scores: list[float]) -> None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return
    if 0.0 <= score <= 1.0:
        scores.append(score)
