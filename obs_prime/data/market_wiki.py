from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import DATA_DIR, resolve_project_path
from .item_wiki import (
    WARFRAME_MARKET_ITEMS_URL,
    _add_lookup,
    _fetch_json,
    _has_hangul,
    _localized_name,
    _write_text_with_backup,
)


DEFAULT_MARKET_WIKI_DIR = DATA_DIR / "market_wiki"


def market_wiki_path(raw_path: str | Path | None = None) -> Path:
    if raw_path:
        return resolve_project_path(raw_path)
    return DEFAULT_MARKET_WIKI_DIR


def refresh_market_wiki(
    output_dir: str | Path | None = None,
    market_items_url: str = WARFRAME_MARKET_ITEMS_URL,
    timeout: float = 45.0,
) -> dict[str, Any]:
    target_dir = market_wiki_path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    updated_at = _utc_now()
    version = datetime.now().strftime("%y-%m-%d")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    payload = _fetch_json(market_items_url, timeout)
    market_items = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(market_items, list):
        market_items = []

    entries = [_market_entry(item, market_items_url, updated_at, version) for item in market_items if isinstance(item, dict)]
    entries = [entry for entry in entries if entry is not None]
    entries.sort(key=lambda row: str(row["slug"]))

    written_count = 0
    unchanged_count = 0
    backup_count = 0
    files: dict[str, str] = {}
    lookup_ko: dict[str, str] = {}
    lookup_en: dict[str, str] = {}
    lookup_slug: dict[str, str] = {}
    tag_index: dict[str, list[str]] = {}
    collisions: list[dict[str, str]] = []

    for entry in entries:
        slug = str(entry["slug"])
        filename = f"{slug}.json"
        files[slug] = filename
        for tag in entry.get("tags", []):
            tag_key = str(tag).strip().lower()
            if tag_key:
                tag_index.setdefault(tag_key, []).append(filename)
        target = target_dir / filename
        outcome = _write_text_with_backup(target, json.dumps(entry, ensure_ascii=False, indent=2), timestamp)
        written_count += 1 if outcome == "written" else 0
        unchanged_count += 1 if outcome == "unchanged" else 0
        backup_count += 1 if outcome == "backup_written" else 0
        for alias in entry.get("aliases", []):
            _add_lookup(lookup_ko if _has_hangul(str(alias)) else lookup_en, str(alias), filename, collisions)
        _add_lookup(lookup_slug, slug, filename, collisions)

    index_payload = {
        "schema": "obs_prime.market_wiki_index.v1",
        "version": version,
        "updated_at": updated_at,
        "source": {
            "warframe_market_items": market_items_url,
        },
        "count": len(entries),
        "files": files,
        "by_ko": lookup_ko,
        "by_en": lookup_en,
        "by_slug": lookup_slug,
        "by_tag": {tag: sorted(set(files)) for tag, files in sorted(tag_index.items())},
        "collision_count": len(collisions),
        "collisions": collisions,
        "notes": [
            "Market wiki covers the full Warframe Market v2 item catalog, not only relic rewards.",
            "Item files are named by stable market slug.",
            "Korean lookup should use this index first, then the slug for Warframe Market order queries.",
        ],
    }
    index_outcome = _write_text_with_backup(target_dir / "_index.json", json.dumps(index_payload, ensure_ascii=False, indent=2), timestamp)
    written_count += 1 if index_outcome == "written" else 0
    unchanged_count += 1 if index_outcome == "unchanged" else 0
    backup_count += 1 if index_outcome == "backup_written" else 0

    return {
        "stage": "market_wiki_update",
        "status": "PASS" if entries else "FAIL",
        "version": version,
        "output_dir": str(target_dir),
        "index_path": str(target_dir / "_index.json"),
        "item_count": len(entries),
        "written_count": written_count,
        "unchanged_count": unchanged_count,
        "backup_count": backup_count,
        "collision_count": len(collisions),
        "source": index_payload["source"],
    }


def _market_entry(item: dict[str, Any], market_items_url: str, updated_at: str, version: str) -> dict[str, Any] | None:
    slug = str(item.get("slug", "")).strip()
    if not slug:
        return None
    name_en = _localized_name(item, "en") or slug.replace("_", " ").title()
    name_kr = _localized_name(item, "ko") or name_en
    i18n = item.get("i18n", {}) if isinstance(item.get("i18n", {}), dict) else {}
    i18n_en = i18n.get("en", {}) if isinstance(i18n.get("en", {}), dict) else {}
    i18n_ko = i18n.get("ko", {}) if isinstance(i18n.get("ko", {}), dict) else {}
    tags_raw = item.get("tags", [])
    tags = sorted({str(tag).strip().lower() for tag in tags_raw if str(tag).strip()}) if isinstance(tags_raw, list) else []
    return {
        "schema": "obs_prime.market_wiki_entry.v1",
        "market_id": str(item.get("id", "")),
        "slug": slug,
        "market_slug": slug,
        "name_en": name_en,
        "name_en_slug": slug,
        "name_kr": name_kr,
        "aliases": _market_aliases(name_en, name_kr, slug),
        "tags": tags,
        "game_ref": str(item.get("gameRef", "")),
        "max_rank": item.get("maxRank"),
        "tradable": True,
        "icon": str(i18n_ko.get("icon") or i18n_en.get("icon") or ""),
        "thumb": str(i18n_ko.get("thumb") or i18n_en.get("thumb") or ""),
        "source": {
            "warframe_market_items": market_items_url,
        },
        "updated_at": updated_at,
        "version": version,
    }


def _market_aliases(name_en: str, name_kr: str, slug: str) -> list[str]:
    aliases = {name_en, name_kr, slug, slug.replace("_", " ")}
    for suffix in (" 설계도", " Blueprint"):
        for value in (name_kr, name_en):
            if value.endswith(suffix):
                aliases.add(value[: -len(suffix)])
    return sorted({value.strip() for value in aliases if value and value.strip()})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
