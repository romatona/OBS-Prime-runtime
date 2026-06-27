from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .warframe_market import WarframeMarketClient, load_market_price_cache, windows_curl_executable, windows_hidden_subprocess_kwargs
from .item_wiki_store import MAX_ITEM_WIKI_INDEX_BYTES
from ..paths import DATA_DIR, resolve_project_path


ITEM_WIKI_DIR = DATA_DIR / "item_wiki"
WFCD_RELICS_URL = "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Relics.json"
WARFRAME_MARKET_ITEMS_URL = "https://api.warframe.market/v2/items"
MARKET_PROBE_SLUG = "voruna_prime_chassis_blueprint"
MAX_ITEM_WIKI_RESPONSE_BYTES = 24 * 1024 * 1024
ALLOWED_ITEM_WIKI_HOSTS = {"raw.githubusercontent.com", "api.warframe.market"}
MAX_EXISTING_ITEM_WIKI_COMPARE_BYTES = 2 * 1024 * 1024
RARITY_DUCATS = {
    "common": 15,
    "uncommon": 45,
    "rare": 100,
}


@dataclass
class RelicRewardMeta:
    slug: str
    name_en: str
    rarities: set[str]
    ducats_from_rarity: set[int]
    relic_count: int = 0


def item_wiki_path(raw_path: str | Path | None = None) -> Path:
    if raw_path:
        return resolve_project_path(raw_path)
    return ITEM_WIKI_DIR


def item_wiki_version(raw_path: str | Path | None = None) -> dict[str, Any]:
    index_path = item_wiki_path(raw_path) / "_index.json"
    if not index_path.exists():
        return {
            "status": "missing",
            "version": "",
            "count": 0,
            "index_path": str(index_path),
            "text": "현재 데이터 베이스는 미구축 상태입니다.",
        }
    if index_path.stat().st_size > MAX_ITEM_WIKI_INDEX_BYTES:
        return {
            "status": "invalid",
            "version": "",
            "count": 0,
            "index_path": str(index_path),
            "text": "현재 데이터 베이스는 인덱스 과대 상태입니다.",
        }
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "status": "invalid",
            "version": "",
            "count": 0,
            "index_path": str(index_path),
            "error": str(exc),
            "text": "현재 데이터 베이스는 읽기 실패 상태입니다.",
        }
    version = str(payload.get("version", ""))
    count = int(payload.get("count", 0) or 0)
    if not version:
        return {
            "status": "invalid",
            "version": "",
            "count": count,
            "index_path": str(index_path),
            "text": "현재 데이터 베이스는 버전 없음 상태입니다.",
        }
    return {
        "status": "ready",
        "version": version,
        "count": count,
        "index_path": str(index_path),
        "text": f"현재 데이터 베이스는 {version} 버전입니다. ({count}개)",
    }


def probe_market_api(
    slug: str = MARKET_PROBE_SLUG,
    platform: str = "pc",
    language: str = "ko",
    crossplay: bool = True,
    timeout: float = 10.0,
    statuses: tuple[str, ...] = ("ingame",),
) -> dict[str, Any]:
    client = WarframeMarketClient(platform=platform, language=language, crossplay=crossplay, timeout=timeout)
    payload = client.top_orders(slug)
    rows = payload.get("data", {}).get("sell", [])
    status_set = {value.lower() for value in statuses}
    prices: list[float] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        user = row.get("user", {})
        user_status = str(user.get("status", "")).lower() if isinstance(user, dict) else ""
        if status_set and user_status not in status_set:
            continue
        if row.get("visible") is False or str(row.get("type", "")).lower() != "sell":
            continue
        try:
            prices.append(float(row["platinum"]))
        except (KeyError, TypeError, ValueError):
            continue
    prices.sort()
    status = "PASS" if prices else "FAIL"
    return {
        "stage": "market_api_probe",
        "status": status,
        "item_kr": "보루나 프라임 섀시 설계도",
        "item_query": "보루나 프라임 섀시",
        "slug": slug,
        "platform": platform,
        "language": language,
        "crossplay": crossplay,
        "statuses": list(statuses),
        "lowest_plat": prices[0] if prices else None,
        "lowest_plat_display": f"{_format_number(prices[0])} p" if prices else "-",
        "orders_seen": len(prices),
        "source": "warframe_market_v2_top",
    }


def refresh_item_wiki(
    output_dir: str | Path | None = None,
    price_cache_path: str | Path | None = None,
    wfcd_relics_url: str = WFCD_RELICS_URL,
    market_items_url: str = WARFRAME_MARKET_ITEMS_URL,
    timeout: float = 45.0,
) -> dict[str, Any]:
    target_dir = item_wiki_path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    updated_at = _utc_now()
    version = datetime.now().strftime("%y-%m-%d")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    relic_payload = _fetch_json(wfcd_relics_url, timeout)
    relics = relic_payload if isinstance(relic_payload, list) else []
    reward_meta = _collect_relic_reward_meta(relics)

    market_payload = _fetch_json(market_items_url, timeout)
    market_items = market_payload.get("data", []) if isinstance(market_payload, dict) else []
    if not isinstance(market_items, list):
        market_items = []
    catalog_by_slug = {str(item.get("slug", "")): item for item in market_items if isinstance(item, dict) and item.get("slug")}
    price_by_slug = {price.item_id: price for price in load_market_price_cache(price_cache_path, same_day_only=False)}

    entries: list[dict[str, Any]] = []
    exact_count = 0
    fallback_count = 0
    for slug, meta in sorted(reward_meta.items()):
        catalog_item = catalog_by_slug.get(slug, {})
        ducats = _coerce_int(catalog_item.get("ducats")) if catalog_item else None
        ducats_source = "warframe_market_v2_items"
        if ducats is not None:
            exact_count += 1
        else:
            ducats = _choose_fallback_ducats(meta)
            ducats_source = "wfcd_relic_rarity_fallback"
            fallback_count += 1
        if ducats is None:
            continue
        name_en = _localized_name(catalog_item, "en") or meta.name_en or slug
        name_kr = _localized_name(catalog_item, "ko") or name_en
        price = price_by_slug.get(slug)
        plat = _coerce_float(price.plat_price_min) if price else None
        plat_date = _format_date_mmddyy(price.last_updated) if price and price.last_updated else ""
        if slug == "forma_blueprint":
            name_en = "Forma Blueprint"
            name_kr = "포르마 설계도"
            ducats = 0
            ducats_source = "fixed_forma_reward"
            plat = 0
            plat_date = datetime.now().strftime("%m-%d-%y")
        entry = {
            "schema": "obs_prime.item_wiki_entry.v1",
            "slug": slug,
            "market_slug": slug,
            "name_en": name_en,
            "name_en_slug": slug,
            "name_kr": name_kr,
            "aliases": _entry_aliases(name_en, name_kr, slug),
            "ducats": int(ducats),
            "ducats_display": f"{int(ducats)} d",
            "ducats_source": ducats_source,
            "rarities": sorted(meta.rarities),
            "plat": plat,
            "plat_display": f"{_format_number(plat)} p" if plat is not None else "-",
            "plat_date": plat_date,
            "source": {
                "wfcd_relics": wfcd_relics_url,
                "warframe_market_items": market_items_url,
                "price_cache": str(resolve_project_path(price_cache_path)) if price_cache_path else "",
            },
            "updated_at": updated_at,
            "version": version,
            "relic_reward_count": meta.relic_count,
        }
        entries.append(entry)

    written_count = 0
    unchanged_count = 0
    backup_count = 0
    files: dict[str, str] = {}
    lookup_ko: dict[str, str] = {}
    lookup_en: dict[str, str] = {}
    lookup_slug: dict[str, str] = {}
    collisions: list[dict[str, str]] = []
    for entry in entries:
        filename = f"{entry['slug']}.json"
        files[str(entry["slug"])] = filename
        target = target_dir / filename
        payload_text = json.dumps(entry, ensure_ascii=False, indent=2)
        outcome = _write_text_with_backup(target, payload_text, timestamp)
        written_count += 1 if outcome == "written" else 0
        unchanged_count += 1 if outcome == "unchanged" else 0
        backup_count += 1 if outcome == "backup_written" else 0
        for alias in entry.get("aliases", []):
            _add_lookup(lookup_ko if _has_hangul(alias) else lookup_en, alias, filename, collisions)
        _add_lookup(lookup_slug, str(entry["slug"]), filename, collisions)

    index_payload = {
        "schema": "obs_prime.item_wiki_index.v1",
        "version": version,
        "updated_at": updated_at,
        "source": {
            "wfcd_relics": wfcd_relics_url,
            "warframe_market_items": market_items_url,
            "price_cache": str(resolve_project_path(price_cache_path)) if price_cache_path else "",
        },
        "count": len(entries),
        "exact_ducat_count": exact_count,
        "fallback_ducat_count": fallback_count,
        "files": files,
        "by_ko": lookup_ko,
        "by_en": lookup_en,
        "by_slug": lookup_slug,
        "collision_count": len(collisions),
        "collisions": collisions,
        "notes": [
            "Item files are named by stable market slug.",
            "Korean OCR lookup uses this index, not Korean filesystem filenames.",
            "WFCD Relics.json is used to define relic reward slugs; Warframe Market v2 catalog supplies exact ducats and Korean names when available.",
        ],
    }
    index_text = json.dumps(index_payload, ensure_ascii=False, indent=2)
    index_outcome = _write_text_with_backup(target_dir / "_index.json", index_text, timestamp)
    written_count += 1 if index_outcome == "written" else 0
    unchanged_count += 1 if index_outcome == "unchanged" else 0
    backup_count += 1 if index_outcome == "backup_written" else 0

    status = "PASS" if entries else "FAIL"
    return {
        "stage": "ducat_db_update",
        "status": status,
        "version": version,
        "output_dir": str(target_dir),
        "index_path": str(target_dir / "_index.json"),
        "item_count": len(entries),
        "exact_ducat_count": exact_count,
        "fallback_ducat_count": fallback_count,
        "written_count": written_count,
        "unchanged_count": unchanged_count,
        "backup_count": backup_count,
        "collision_count": len(collisions),
        "sources": index_payload["source"],
    }


def _collect_relic_reward_meta(relics: list[Any]) -> dict[str, RelicRewardMeta]:
    rewards: dict[str, RelicRewardMeta] = {}
    for relic in relics:
        if not isinstance(relic, dict):
            continue
        for reward in relic.get("rewards", []) or []:
            if not isinstance(reward, dict):
                continue
            item = reward.get("item", {})
            if not isinstance(item, dict):
                continue
            name_en = str(item.get("name", "")).strip()
            market = item.get("warframeMarket", {})
            slug = str(market.get("urlName", "")).strip() if isinstance(market, dict) else ""
            if not slug and name_en.lower() == "forma blueprint":
                slug = "forma_blueprint"
            if not slug:
                continue
            rarity = str(reward.get("rarity", "")).strip()
            ducats = 0 if slug == "forma_blueprint" else RARITY_DUCATS.get(rarity.lower())
            meta = rewards.setdefault(slug, RelicRewardMeta(slug=slug, name_en=name_en, rarities=set(), ducats_from_rarity=set()))
            if name_en and not meta.name_en:
                meta.name_en = name_en
            if rarity:
                meta.rarities.add(rarity)
            if ducats is not None:
                meta.ducats_from_rarity.add(int(ducats))
            meta.relic_count += 1
    return rewards


def _choose_fallback_ducats(meta: RelicRewardMeta) -> int | None:
    if meta.slug == "forma_blueprint":
        return 0
    if not meta.ducats_from_rarity:
        return None
    if len(meta.ducats_from_rarity) == 1:
        return next(iter(meta.ducats_from_rarity))
    return max(meta.ducats_from_rarity)


def _fetch_json(url: str, timeout: float) -> Any:
    _validate_fetch_url(url)
    if os.name == "nt":
        completed = subprocess.run(
            [
                windows_curl_executable(),
                "--ssl-no-revoke",
                "-L",
                "-sS",
                "--max-filesize",
                str(MAX_ITEM_WIKI_RESPONSE_BYTES),
                "--max-time",
                str(max(1, int(timeout))),
                "-w",
                "\n%{http_code}",
                "-H",
                "Accept: application/json",
                "-H",
                "Platform: pc",
                "-H",
                "Language: ko",
                "-H",
                "User-Agent: OBS-prime-local-tool/0.1",
                url,
            ],
            capture_output=True,
            text=True,
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
        output = completed.stdout.strip()
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or output or f"curl exit {completed.returncode}").strip())
        if "\n" not in output:
            raise RuntimeError("curl response missing HTTP status")
        body, status_text = output.rsplit("\n", 1)
        status = int(status_text)
        if status >= 400:
            raise RuntimeError(f"http_{status}")
        if len(body.encode("utf-8")) > MAX_ITEM_WIKI_RESPONSE_BYTES:
            raise RuntimeError("item wiki response too large")
        return json.loads(body)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Platform": "pc",
            "Language": "ko",
            "User-Agent": "OBS-prime-local-tool/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(_read_response_limited(response, MAX_ITEM_WIKI_RESPONSE_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"http_{exc.code}") from exc


def _validate_fetch_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_ITEM_WIKI_HOSTS:
        raise RuntimeError(f"unsupported item wiki source URL: {url}")


def _read_response_limited(response: Any, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError("item wiki response too large")
    return body


def _localized_name(item: dict[str, Any], language: str) -> str:
    i18n = item.get("i18n", {}) if isinstance(item, dict) else {}
    row = i18n.get(language, {}) if isinstance(i18n, dict) else {}
    return str(row.get("name", "")).strip() if isinstance(row, dict) else ""


def _entry_aliases(name_en: str, name_kr: str, slug: str) -> list[str]:
    aliases = [name_kr, name_en, slug]
    for suffix in (" 설계도", " Blueprint"):
        for value in (name_kr, name_en):
            if value.endswith(suffix):
                aliases.append(value[: -len(suffix)])
    return sorted({value.strip() for value in aliases if value and value.strip()})


def _add_lookup(target: dict[str, str], key: str, filename: str, collisions: list[dict[str, str]]) -> None:
    normalized = _normalize_lookup_key(key)
    if not normalized:
        return
    existing = target.get(normalized)
    if existing and existing != filename:
        collisions.append({"key": normalized, "kept": existing, "ignored": filename})
        return
    target[normalized] = filename


def _normalize_lookup_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _has_hangul(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)


def _write_text_with_backup(target: Path, text: str, timestamp: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(target.suffix + f".{timestamp}.bak")
        if target.stat().st_size <= MAX_EXISTING_ITEM_WIKI_COMPARE_BYTES:
            current = target.read_text(encoding="utf-8-sig")
            if current == text:
                return "unchanged"
        shutil.copy2(target, backup)
        target.write_text(text, encoding="utf-8")
        return "backup_written"
    target.write_text(text, encoding="utf-8")
    return "written"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_date_mmddyy(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return ""
    return parsed.astimezone().strftime("%m-%d-%y")


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: float | int | None) -> str:
    if value is None:
        return "-"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")
