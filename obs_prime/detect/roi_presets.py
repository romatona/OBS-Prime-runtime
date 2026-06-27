from __future__ import annotations

import json
from dataclasses import dataclass

from ..models import Rect
from ..paths import resolve_project_path
from pathlib import Path

MAX_PRESET_JSON_BYTES = 256 * 1024


@dataclass(frozen=True)
class RoiPreset:
    preset_id: str
    display_name: str
    capture_resolution: str
    reward_panel_rect: Rect
    slot_name_rects: list[Rect]
    notes: str = ""


def load_roi_preset(preset_id: str) -> RoiPreset:
    path = _preset_path("roi", preset_id)
    if not path.exists():
        return RoiPreset(
            preset_id=preset_id,
            display_name=f"Missing ROI preset: {preset_id}",
            capture_resolution="unknown",
            reward_panel_rect=Rect(0, 0, 0, 0),
            slot_name_rects=[],
            notes="preset missing",
        )
    if path.stat().st_size > MAX_PRESET_JSON_BYTES:
        raise RuntimeError(f"ROI preset is too large: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return RoiPreset(
        preset_id=str(payload.get("preset_id", preset_id)),
        display_name=str(payload.get("display_name", preset_id)),
        capture_resolution=str(payload.get("capture_resolution", "unknown")),
        reward_panel_rect=_rect(payload.get("reward_panel_rect", {})),
        slot_name_rects=[_rect(row) for row in payload.get("slot_name_rects", [])],
        notes=str(payload.get("notes", "")),
    )


def _rect(payload: dict) -> Rect:
    return Rect(int(payload.get("x", 0)), int(payload.get("y", 0)), int(payload.get("w", 0)), int(payload.get("h", 0)))


def _preset_path(kind: str, preset_id: str) -> Path:
    if not _safe_preset_id(preset_id):
        return resolve_project_path(Path("presets") / kind / "__invalid__.json")
    return resolve_project_path(Path("presets") / kind / f"{preset_id}.json")


def _safe_preset_id(preset_id: str) -> bool:
    return bool(preset_id) and all(char.isalnum() or char in {"-", "_"} for char in preset_id)
