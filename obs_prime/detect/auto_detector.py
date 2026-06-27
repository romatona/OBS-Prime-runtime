from __future__ import annotations

from pathlib import Path
import time

from ..models import CaptureFrame, DetectorResult, Rect
from .geometry import default_reward_geometry
from .templates import DEFAULT_DETECTOR_PRESET, DetectorPreset


class AutoDetector:
    def __init__(self, preset: DetectorPreset = DEFAULT_DETECTOR_PRESET) -> None:
        self.preset = preset

    def detect(self, frame: CaptureFrame, threshold: float = 0.86) -> DetectorResult:
        start = time.perf_counter()
        panel, slots = self._preset_geometry(frame.width, frame.height)
        reasons: list[str] = []
        is_virtual = frame.source in {"virtual_sample", "virtual"}
        is_live_screen = frame.source == "screen"
        base_confidence = 0.90 if is_virtual else 0.0
        if base_confidence:
            reasons.append("virtual sample geometry accepted")

        geometry_confidence = 0.0
        panel_visible = _is_rect_visible(panel, frame.width, frame.height)
        if panel_visible:
            geometry_confidence = 0.62 if frame.source == "sample_image" else 0.45
            reasons.append("geometry anchor fit")
            if panel.w > 0:
                aspect = panel.w / max(1, panel.h)
                aspect_diff = abs(aspect - (1497 / 367))
                geometry_confidence = min(0.70, geometry_confidence + max(0.0, 0.05 - aspect_diff))
                if aspect_diff > 0.08:
                    reasons.append(f"panel aspect drift={aspect_diff:.2f}")
        else:
            reasons.append("panel out of bounds")

        template_score = self._template_match_score(frame)
        if template_score is not None:
            reasons.append(f"template match score={template_score:.2f}")
        if not self.preset.template_paths:
            reasons.append("no template path configured for preset")
        if not is_virtual and self.preset.template_paths and template_score is None:
            reasons.append("template files not readable for this input")

        confidence = max(base_confidence, geometry_confidence, template_score or 0.0)
        blocked_live_geometry_only = is_live_screen and template_score is None
        if not is_virtual and template_score is None:
            reasons.append("template evidence required before geometry-only confidence can pass")
            confidence = min(confidence, 0.49)
        if blocked_live_geometry_only:
            reasons.append("blocked live screen geometry-only detection")
        detected = confidence >= threshold and not blocked_live_geometry_only
        reason = "; ".join(reasons) if reasons else "no detection signal"
        return DetectorResult(
            detected=detected,
            confidence=confidence,
            preset_id=self.preset.preset_id,
            screen_rect=Rect(0, 0, frame.width, frame.height),
            reward_panel_rect=panel,
            slot_rects=slots,
            template_confidence=template_score,
            reason=reason,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    def _preset_geometry(self, width: int, height: int) -> tuple[Rect, list[Rect]]:
        scaled_panel = self._scale_rect(self.preset.reward_panel_rect, width, height)
        scaled_slots = [self._scale_rect(rect, width, height) for rect in self.preset.expected_slot_rects]
        if scaled_slots and len(scaled_slots) == 4 and _all_rects_valid(scaled_slots, width, height):
            return scaled_panel, scaled_slots
        if scaled_panel.w > 0 and scaled_panel.h > 0:
            return scaled_panel, _derive_slots_from_panel(scaled_panel)
        return default_reward_geometry(width, height)

    def _scale_rect(self, rect: Rect, width: int, height: int) -> Rect:
        ref_w, ref_h = _parse_resolution(self.preset.capture_resolution)
        if ref_w <= 0 or ref_h <= 0 or not _is_positive_rect(rect):
            return rect
        scale_x = width / ref_w
        scale_y = height / ref_h
        return Rect(
            int(rect.x * scale_x),
            int(rect.y * scale_y),
            int(rect.w * scale_x),
            int(rect.h * scale_y),
        )

    def _template_match_score(self, frame: CaptureFrame) -> float | None:
        if frame.image is None or not self.preset.template_paths:
            return None
        try:
            from PIL import Image
            from PIL import ImageChops, ImageOps, ImageStat
        except Exception:
            return None
        if frame.width <= 0 or frame.height <= 0:
            return None

        base = frame.image.convert("L")
        panel_crop: Image.Image | None = None
        _, slots = self._preset_geometry(frame.width, frame.height)
        panel = _derive_panel_from_slots(slots)
        if panel.w > 0 and panel.h > 0 and _is_rect_visible(panel, frame.width, frame.height):
            panel_crop = base.crop((panel.x, panel.y, panel.x + panel.w, panel.y + panel.h))
        crop = panel_crop or base
        if crop.width <= 1 or crop.height <= 1:
            return None

        panel_score = None
        for template_path in self.preset.template_paths:
            template_img = _read_template_image(Path(template_path))
            if template_img is None:
                continue
            template = ImageOps.grayscale(template_img)
            if template.width <= 1 or template.height <= 1 or crop.width < template.width or crop.height < template.height:
                continue
            scaled = crop.resize((template.width, template.height))
            if scaled.width <= 0 or scaled.height <= 0:
                continue
            diff = ImageStat.Stat(ImageChops.difference(scaled, template)).mean[0]
            score = 1.0 - (diff / 255.0)
            panel_score = max(panel_score or 0.0, score)
        return panel_score


def _is_positive_rect(rect: Rect) -> bool:
    return rect.w > 0 and rect.h > 0


def _derive_panel_from_slots(slots: list[Rect]) -> Rect:
    if not slots:
        return Rect(0, 0, 0, 0)
    min_x = min(slot.x for slot in slots)
    min_y = min(slot.y for slot in slots)
    max_x = max(slot.x + slot.w for slot in slots)
    max_y = max(slot.y + slot.h for slot in slots)
    return Rect(min_x, min_y - max(0, int((max_y - min_y) * 0.35)), max_x - min_x, max_y - min_y)


def _derive_slots_from_panel(panel: Rect) -> list[Rect]:
    slot_w = int(panel.w / 4)
    slot_h = int(panel.h * 0.42)
    slot_y = panel.y + int(panel.h * 0.25)
    return [
        Rect(panel.x + i * slot_w + int(slot_w * 0.10), slot_y, int(slot_w * 0.80), slot_h)
        for i in range(4)
    ]


def _is_rect_visible(rect: Rect, width: int, height: int) -> bool:
    return (
        rect.x >= 0
        and rect.y >= 0
        and rect.w > 0
        and rect.h > 0
        and rect.x + rect.w <= width
        and rect.y + rect.h <= height
    )


def _all_rects_valid(rects: list[Rect], width: int, height: int) -> bool:
    return all(_is_rect_visible(rect, width, height) for rect in rects)


def _parse_resolution(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().strip().split("x", 1)
        return int(width), int(height)
    except Exception:
        return 1920, 1080


def _read_template_image(path: Path):
    try:
        from PIL import Image
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        return Image.open(path).convert("L")
    except Exception:
        return None
