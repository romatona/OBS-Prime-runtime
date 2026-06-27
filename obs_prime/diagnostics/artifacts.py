from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import DEBUG_DIR, resolve_project_path


class ArtifactWriter:
    def __init__(self, root: Path | str = DEBUG_DIR, enabled: bool = True) -> None:
        self.enabled = enabled
        self.root = resolve_project_path(root)
        self.run_dir = self.root / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        if self.enabled:
            self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, payload: dict[str, Any] | list[Any]) -> str:
        if not self.enabled:
            return ""
        path = self._artifact_path(name)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def write_text(self, name: str, payload: str) -> str:
        if not self.enabled:
            return ""
        path = self._artifact_path(name)
        path.write_text(payload, encoding="utf-8")
        return str(path)

    def write_binary(self, name: str, payload: bytes) -> str:
        if not self.enabled:
            return ""
        path = self._artifact_path(name)
        path.write_bytes(payload)
        return str(path)

    def write_crop(self, name: str, image, rect) -> str | None:
        if not self.enabled:
            return None
        if image is None or not hasattr(image, "crop"):
            return None
        path = self._artifact_path(name)
        crop = image.crop((rect.x, rect.y, rect.x + rect.w, rect.y + rect.h))
        crop.save(path)
        return str(path)

    def _artifact_path(self, name: str) -> Path:
        candidate = Path(name)
        if candidate.is_absolute():
            raise ValueError(f"artifact name must be relative: {name}")
        path = (self.run_dir / candidate).resolve()
        try:
            path.relative_to(self.run_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"artifact path escapes run directory: {name}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
