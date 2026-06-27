from __future__ import annotations

import json
from dataclasses import dataclass

from ..paths import resolve_project_path
from pathlib import Path

MAX_PRESET_JSON_BYTES = 256 * 1024


@dataclass(frozen=True)
class OcrPreset:
    preset_id: str
    display_name: str
    provider: str
    language: str
    preprocessing_preset: str
    timeout_ms: int
    min_confidence: float


def load_ocr_preset(preset_id: str) -> OcrPreset:
    path = _preset_path("ocr", preset_id)
    if not path.exists():
        return OcrPreset(preset_id, f"Missing OCR preset: {preset_id}", "tesseract", "kor+eng", preset_id, 2500, 0.70)
    if path.stat().st_size > MAX_PRESET_JSON_BYTES:
        raise RuntimeError(f"OCR preset is too large: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return OcrPreset(
        preset_id=str(payload.get("preset_id", preset_id)),
        display_name=str(payload.get("display_name", preset_id)),
        provider=str(payload.get("provider", "tesseract")),
        language=str(payload.get("language", "kor+eng")),
        preprocessing_preset=str(payload.get("preprocessing_preset", "default-korean-ui")),
        timeout_ms=int(payload.get("timeout_ms", 2500)),
        min_confidence=float(payload.get("min_confidence", 0.70)),
    )


def _preset_path(kind: str, preset_id: str) -> Path:
    if not _safe_preset_id(preset_id):
        return resolve_project_path(Path("presets") / kind / "__invalid__.json")
    return resolve_project_path(Path("presets") / kind / f"{preset_id}.json")


def _safe_preset_id(preset_id: str) -> bool:
    return bool(preset_id) and all(char.isalnum() or char in {"-", "_"} for char in preset_id)
