from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from ..paths import resolve_project_path

MAX_CORRECTION_STORE_BYTES = 1024 * 1024


class CorrectionStore:
    def __init__(self, path: Path) -> None:
        self.path = resolve_project_path(path)
        self.corrections: dict[str, str] = {}
        if self.path.exists():
            if self.path.stat().st_size > MAX_CORRECTION_STORE_BYTES:
                raise RuntimeError(f"correction store is too large: {self.path}")
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"correction store root must be an object: {self.path}")
            self.corrections = {str(key): str(value) for key, value in payload.items()}

    def lookup(self, normalized_text: str) -> str | None:
        return self.corrections.get(normalized_text)

    def set(self, normalized_text: str, item_id: str, overwrite: bool = False) -> None:
        if normalized_text in self.corrections and not overwrite:
            raise ValueError("correction already exists")
        self.corrections[normalized_text] = item_id

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + f".{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bak")
            shutil.copy2(self.path, backup)
        self.path.write_text(json.dumps(self.corrections, ensure_ascii=False, indent=2), encoding="utf-8")
