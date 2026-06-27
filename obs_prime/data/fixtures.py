from __future__ import annotations

from pathlib import Path

from ..paths import DATA_DIR, resolve_project_path
from .item_store import ItemStore, UserStateStore
from .price_store import PriceStore


def fixture_path(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        return resolve_project_path(candidate)
    # Try explicit legacy paths first (for compatibility), then project fixture folder.
    explicit = resolve_project_path(candidate)
    if explicit.exists():
        return explicit
    if candidate.parts and candidate.parts[0] == "fixtures":
        candidate = candidate.relative_to("fixtures")
    candidate = DATA_DIR / "fixtures" / candidate
    return candidate


def load_fixture_stores(name: str, max_age_hours: int, price_cache_path: str | Path | None = None) -> tuple[ItemStore, PriceStore, UserStateStore]:
    path = fixture_path(name)
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    return ItemStore.from_fixture(path), PriceStore.from_fixture(path, max_age_hours, price_cache_path), UserStateStore.from_fixture(path)
