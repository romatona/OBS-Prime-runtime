from __future__ import annotations

from typing import Any

from ..models import Rect


DEFAULT_TOP_RATIO = 0.58
DEFAULT_HEIGHT_RATIO = 0.38
DEFAULT_MIN_HEIGHT = 24


def name_band_config(ocr_cfg: dict[str, Any]) -> tuple[bool, float, float]:
    enabled = bool(ocr_cfg.get("obs_name_band_enabled", True))
    try:
        top_ratio = float(ocr_cfg.get("obs_name_band_top_ratio", DEFAULT_TOP_RATIO))
    except (TypeError, ValueError):
        top_ratio = DEFAULT_TOP_RATIO
    try:
        height_ratio = float(ocr_cfg.get("obs_name_band_height_ratio", DEFAULT_HEIGHT_RATIO))
    except (TypeError, ValueError):
        height_ratio = DEFAULT_HEIGHT_RATIO
    return enabled, _clamp_ratio(top_ratio, 0.0, 0.95), _clamp_ratio(height_ratio, 0.05, 1.0)


def apply_name_band(rects: list[Rect], ocr_cfg: dict[str, Any]) -> list[Rect]:
    enabled, top_ratio, height_ratio = name_band_config(ocr_cfg)
    if not enabled:
        return rects
    return [rect_to_name_band(rect, top_ratio, height_ratio) for rect in rects]


def apply_name_band_to_dicts(rects: list[dict[str, int]], ocr_cfg: dict[str, Any]) -> list[dict[str, int]]:
    enabled, top_ratio, height_ratio = name_band_config(ocr_cfg)
    if not enabled:
        return rects
    adjusted: list[dict[str, int]] = []
    for row in rects:
        rect = Rect(int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"]))
        adjusted.append(rect_to_name_band(rect, top_ratio, height_ratio).to_dict())
    return adjusted


def rect_to_name_band(rect: Rect, top_ratio: float = DEFAULT_TOP_RATIO, height_ratio: float = DEFAULT_HEIGHT_RATIO) -> Rect:
    if rect.h <= 1:
        return rect
    y_offset = int(round(rect.h * top_ratio))
    if y_offset >= rect.h:
        y_offset = max(0, rect.h - 1)
    available = max(1, rect.h - y_offset)
    target_h = max(1, int(round(rect.h * height_ratio)))
    if rect.h >= DEFAULT_MIN_HEIGHT:
        target_h = max(DEFAULT_MIN_HEIGHT, target_h)
    band_h = min(available, target_h)
    return Rect(rect.x, rect.y + y_offset, rect.w, band_h)


def _clamp_ratio(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))
