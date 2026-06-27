from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..paths import resolve_project_path
from ..models import Rect


@dataclass(frozen=True)
class DetectorPreset:
    preset_id: str
    display_name: str
    threshold: float
    reason: str
    capture_resolution: str
    template_paths: list[str]
    reward_panel_rect: Rect
    expected_slot_rects: list[Rect]


DEFAULT_DETECTOR_PRESET = DetectorPreset(
    preset_id="default-virtual-1080p",
    display_name="Default virtual 1080p reward panel",
    threshold=0.86,
    reason="virtual/sample geometry preset",
    capture_resolution="1920x1080",
    template_paths=[],
    reward_panel_rect=Rect(211, 367, 1497, 367),
    expected_slot_rects=[
        Rect(248, 458, 299, 154),
        Rect(622, 458, 299, 154),
        Rect(996, 458, 299, 154),
        Rect(1370, 458, 299, 154),
    ],
)

MAX_PRESET_JSON_BYTES = 256 * 1024


def load_detector_preset(preset_id: str) -> DetectorPreset:
    path = _preset_path("detector", preset_id)
    if not path.exists():
        return DetectorPreset(
            preset_id=preset_id,
            display_name=f"Missing preset: {preset_id}",
            threshold=0.0,
            reason="preset missing",
            capture_resolution="1920x1080",
            template_paths=[],
            reward_panel_rect=DEFAULT_DETECTOR_PRESET.reward_panel_rect,
            expected_slot_rects=DEFAULT_DETECTOR_PRESET.expected_slot_rects,
        )
    if path.stat().st_size > MAX_PRESET_JSON_BYTES:
        raise RuntimeError(f"detector preset is too large: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    threshold = float(payload.get("thresholds", {}).get("confidence", DEFAULT_DETECTOR_PRESET.threshold))
    template_paths = _normalize_template_paths([str(p) for p in payload.get("template_paths", [])])
    panel_rect = _rect(payload.get("expected_reward_panel_rect", {}))
    slot_rects = [_rect(row) for row in payload.get("expected_slot_rects", [])]
    capture_resolution = str(payload.get("supported_resolution", "1920x1080"))
    return DetectorPreset(
        preset_id=str(payload.get("preset_id", preset_id)),
        display_name=str(payload.get("display_name", preset_id)),
        threshold=threshold,
        reason=", ".join(payload.get("anchor_rules", [])) or "preset loaded",
        capture_resolution=capture_resolution,
        template_paths=template_paths,
        reward_panel_rect=panel_rect,
        expected_slot_rects=slot_rects,
    )


def _rect(payload: dict[str, int | float]) -> Rect:
    return Rect(int(payload.get("x", 0)), int(payload.get("y", 0)), int(payload.get("w", 0)), int(payload.get("h", 0)))


def _normalize_template_paths(paths: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_path in paths:
        try:
            candidate = resolve_project_path(Path(raw_path))
        except ValueError:
            continue
        if candidate.exists():
            normalized.append(str(candidate))
    return normalized


def _preset_path(kind: str, preset_id: str) -> Path:
    if not _safe_preset_id(preset_id):
        return resolve_project_path(Path("presets") / kind / "__invalid__.json")
    return resolve_project_path(Path("presets") / kind / f"{preset_id}.json")


def _safe_preset_id(preset_id: str) -> bool:
    return bool(preset_id) and all(char.isalnum() or char in {"-", "_"} for char in preset_id)
