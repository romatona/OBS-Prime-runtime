from __future__ import annotations

import json
from pathlib import Path

from ..models import ItemRecord

MAX_ITEM_WIKI_INDEX_BYTES = 2 * 1024 * 1024
MAX_ITEM_WIKI_ENTRY_BYTES = 256 * 1024


def load_item_wiki_records(item_wiki_dir: Path) -> list[ItemRecord]:
    index_path = item_wiki_dir / "_index.json"
    if not index_path.exists():
        return []
    if index_path.stat().st_size > MAX_ITEM_WIKI_INDEX_BYTES:
        return []
    payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
    files = payload.get("files", {}) if isinstance(payload, dict) else {}
    if not isinstance(files, dict):
        return []
    records: list[ItemRecord] = []
    seen: set[str] = set()
    for slug, filename in files.items():
        if not isinstance(filename, str):
            continue
        entry_path = _safe_item_wiki_entry_path(item_wiki_dir, filename)
        if entry_path is None:
            continue
        if not entry_path.exists():
            continue
        if entry_path.stat().st_size > MAX_ITEM_WIKI_ENTRY_BYTES:
            continue
        entry = json.loads(entry_path.read_text(encoding="utf-8-sig"))
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("slug") or slug)
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        aliases_raw = entry.get("aliases", [])
        aliases = [str(value) for value in aliases_raw] if isinstance(aliases_raw, list) else []
        try:
            ducats = int(entry.get("ducats", 0) or 0)
        except (TypeError, ValueError):
            continue
        records.append(
            ItemRecord(
                id=item_id,
                ko_name=str(entry.get("name_kr", "")),
                en_name=str(entry.get("name_en", "")),
                aliases=aliases,
                item_type="part",
                rarity=", ".join(str(value) for value in entry.get("rarities", [])) if isinstance(entry.get("rarities", []), list) else "",
                ducats=ducats,
                market_slug=str(entry.get("market_slug") or item_id),
                vaulted=False,
                tradable=item_id != "forma_blueprint",
            )
        )
    return records


def _safe_item_wiki_entry_path(item_wiki_dir: Path, filename: str) -> Path | None:
    root = item_wiki_dir.resolve()
    path = (root / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.name != filename:
        return None
    return path


def merge_item_records(primary: list[ItemRecord], fallback: list[ItemRecord]) -> list[ItemRecord]:
    records = list(primary)
    seen = {item.id for item in records}
    for item in fallback:
        if item.id not in seen:
            records.append(item)
            seen.add(item.id)
    return records
