from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import ItemRecord, PriceRecord
from ..paths import DATA_DIR, resolve_project_path


DEFAULT_MARKET_CACHE = DATA_DIR / "market_cache" / "warframe_market_prices.json"
WARFRAME_MARKET_V2_BASE = "https://api.warframe.market/v2"
DEFAULT_STATUSES = ("ingame",)
DEFAULT_RATE_LIMIT_PER_SECOND = 2
MAX_MARKET_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 1.5
ALLOWED_MARKET_HOSTS = {"api.warframe.market"}
MAX_MARKET_CACHE_BYTES = 8 * 1024 * 1024


def market_cache_path(raw_path: str | Path | None = None) -> Path:
    if raw_path:
        return resolve_project_path(raw_path)
    return DEFAULT_MARKET_CACHE


class WarframeMarketClient:
    def __init__(
        self,
        base_url: str = WARFRAME_MARKET_V2_BASE,
        platform: str = "pc",
        language: str = "ko",
        crossplay: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.platform = platform
        self.language = language
        self.crossplay = crossplay
        self.timeout = timeout

    def top_orders(self, slug: str) -> dict[str, Any]:
        safe_slug = urllib.parse.quote(slug.strip(), safe="")
        url = f"{self.base_url}/orders/item/{safe_slug}/top"
        _validate_market_url(url)
        for attempt in range(2):
            try:
                return self._top_orders_once(url)
            except MarketRateLimitError as exc:
                if attempt >= 1:
                    raise
                time.sleep(exc.retry_after_seconds)
        raise RuntimeError("warframe market request retry exhausted")

    def orders(self, slug: str) -> dict[str, Any]:
        safe_slug = urllib.parse.quote(slug.strip(), safe="")
        url = f"{self.base_url}/orders/item/{safe_slug}"
        _validate_market_url(url)
        for attempt in range(2):
            try:
                return self._top_orders_once(url)
            except MarketRateLimitError as exc:
                if attempt >= 1:
                    raise
                time.sleep(exc.retry_after_seconds)
        raise RuntimeError("warframe market request retry exhausted")

    def _top_orders_once(self, url: str) -> dict[str, Any]:
        if os.name == "nt":
            return self._top_orders_with_curl(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Platform": self.platform,
                "Language": self.language,
                "Crossplay": "true" if self.crossplay else "false",
                "User-Agent": "OBS-prime-local-tool/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(_read_response_limited(response, MAX_MARKET_RESPONSE_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise MarketRateLimitError(_retry_after_seconds(exc.headers.get("Retry-After"))) from exc
            raise

    def _top_orders_with_curl(self, url: str) -> dict[str, Any]:
        completed = subprocess.run(
            [
                windows_curl_executable(),
                "--ssl-no-revoke",
                "-L",
                "-sS",
                "--max-filesize",
                str(MAX_MARKET_RESPONSE_BYTES),
                "--max-time",
                str(max(1.0, float(self.timeout))),
                "-w",
                "\n%{http_code}",
                "-H",
                "Accept: application/json",
                "-H",
                f"Platform: {self.platform}",
                "-H",
                f"Language: {self.language}",
                "-H",
                f"Crossplay: {'true' if self.crossplay else 'false'}",
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
        try:
            status = int(status_text)
        except ValueError as exc:
            raise RuntimeError(f"curl response invalid HTTP status: {status_text}") from exc
        if status == 429:
            raise MarketRateLimitError()
        if status >= 400:
            raise RuntimeError(f"http_{status}")
        if len(body.encode("utf-8")) > MAX_MARKET_RESPONSE_BYTES:
            raise RuntimeError("market response too large")
        return json.loads(body)


class MarketRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: float | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds or DEFAULT_RATE_LIMIT_RETRY_SECONDS
        super().__init__(f"http_429 retry_after={self.retry_after_seconds:g}s")


class _RequestRateLimiter:
    def __init__(self, max_requests_per_second: int = DEFAULT_RATE_LIMIT_PER_SECOND) -> None:
        self.max_requests = max(1, int(max_requests_per_second))
        self.window_seconds = 1.0
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                sleep_for = self.window_seconds - (now - self._timestamps[0])
            time.sleep(max(0.001, sleep_for))


def update_market_price_cache(
    items: list[ItemRecord],
    cache_path: str | Path | None = None,
    platform: str = "pc",
    language: str = "ko",
    crossplay: bool = True,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    timeout: float = 10.0,
    rate_limit_per_second: int = DEFAULT_RATE_LIMIT_PER_SECOND,
) -> dict[str, Any]:
    client = WarframeMarketClient(platform=platform, language=language, crossplay=crossplay, timeout=timeout)
    rate_limiter = _RequestRateLimiter(rate_limit_per_second)
    updated_at = _utc_now()
    prices: list[PriceRecord] = []
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for item in items:
        if not item.tradable:
            skipped.append({"item_id": item.id, "reason": "not_tradable"})
            continue
        if not item.market_slug:
            skipped.append({"item_id": item.id, "reason": "missing_market_slug"})
            continue
        try:
            rate_limiter.wait()
            payload = client.top_orders(item.market_slug)
        except urllib.error.HTTPError as exc:
            failures.append({"item_id": item.id, "slug": item.market_slug, "error": f"http_{exc.code}"})
            continue
        except Exception as exc:
            failures.append({"item_id": item.id, "slug": item.market_slug, "error": str(exc)})
            continue
        price = _price_from_top_orders(item, payload, platform, statuses, updated_at)
        if price is None:
            failures.append({"item_id": item.id, "slug": item.market_slug, "error": "no_visible_sell_orders"})
            continue
        prices.append(price)
    target = write_market_price_cache(cache_path, prices, platform, language, crossplay, statuses, failures, skipped, merge=False)
    return {
        "stage": "market_price_update",
        "status": "PASS" if prices else "FAIL",
        "cache_path": str(target),
        "updated_count": len(prices),
        "failure_count": len(failures),
        "skipped_count": len(skipped),
        "rate_limit_per_second": rate_limit_per_second,
        "prices": [asdict(price) for price in prices],
        "failures": failures,
        "skipped": skipped,
    }


def fetch_market_prices_for_items(
    items: list[ItemRecord],
    cache_path: str | Path | None = None,
    platform: str = "pc",
    language: str = "ko",
    crossplay: bool = True,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    timeout: float = 1.5,
    max_workers: int = 4,
    use_today_cache: bool = True,
    rate_limit_per_second: int = DEFAULT_RATE_LIMIT_PER_SECOND,
) -> dict[str, Any]:
    unique_items = _unique_market_items(items)
    client = WarframeMarketClient(platform=platform, language=language, crossplay=crossplay, timeout=timeout)
    updated_at = _utc_now()
    prices: dict[str, PriceRecord] = {}
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    cache_hit_item_ids: list[str] = []
    for item in unique_items:
        if not item.tradable:
            skipped.append({"item_id": item.id, "reason": "not_tradable"})
        elif not item.market_slug:
            skipped.append({"item_id": item.id, "reason": "missing_market_slug"})

    candidates = [item for item in unique_items if item.tradable and item.market_slug]
    if use_today_cache:
        today_cache = {price.item_id: price for price in load_market_price_cache(cache_path, same_day_only=True, statuses=statuses)}
        for item in candidates:
            cached = today_cache.get(item.id)
            if cached is not None:
                prices[item.id] = cached
                cache_hit_item_ids.append(item.id)

    live_candidates = [item for item in candidates if item.id not in prices]
    if live_candidates:
        rate_limiter = _RequestRateLimiter(rate_limit_per_second)
        worker_count = max(1, min(max_workers, len(live_candidates), 4))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_fetch_single_price, client, item, platform, statuses, updated_at, rate_limiter): item
                for item in live_candidates
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    price = future.result()
                except Exception as exc:
                    failures.append({"item_id": item.id, "slug": item.market_slug, "error": str(exc)})
                    continue
                if price is None:
                    failures.append({"item_id": item.id, "slug": item.market_slug, "error": "no_visible_sell_orders"})
                    continue
                prices[item.id] = price

    live_prices = [price for item_id, price in prices.items() if item_id not in cache_hit_item_ids]
    cache_target = ""
    if live_prices:
        cache_target = str(
            write_market_price_cache(cache_path, live_prices, platform, language, crossplay, statuses, failures, skipped, merge=True)
        )
    return {
        "status": "PASS" if prices else "FAIL",
        "price_by_item": prices,
        "price_count": len(prices),
        "live_count": len(live_prices),
        "fallback_count": len(cache_hit_item_ids),
        "fallback_item_ids": cache_hit_item_ids,
        "cache_hit_count": len(cache_hit_item_ids),
        "cache_hit_item_ids": cache_hit_item_ids,
        "failure_count": len(failures),
        "skipped_count": len(skipped),
        "rate_limit_per_second": rate_limit_per_second,
        "failures": failures,
        "skipped": skipped,
        "cache_path": cache_target or str(market_cache_path(cache_path)),
        "updated_at": updated_at,
    }


def write_market_price_cache(
    cache_path: str | Path | None,
    prices: list[PriceRecord],
    platform: str,
    language: str,
    crossplay: bool,
    statuses: tuple[str, ...],
    failures: list[dict[str, str]] | None = None,
    skipped: list[dict[str, str]] | None = None,
    merge: bool = True,
) -> Path:
    target = market_cache_path(cache_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = {price.item_id: price for price in load_market_price_cache(target, same_day_only=False, statuses=statuses)} if merge else {}
    for price in prices:
        merged[price.item_id] = price
    cache_payload = {
        "schema": "obs_prime.warframe_market_price_cache.v1",
        "source": "warframe_market_v2_top",
        "updated_at": _utc_now(),
        "platform": platform,
        "language": language,
        "crossplay": crossplay,
        "statuses": list(statuses),
        "prices": [asdict(price) for price in merged.values()],
        "failures": failures or [],
        "skipped": skipped or [],
        "note": "orders_seen is top endpoint rows after status filtering, not total market volume.",
    }
    target.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_market_price_cache(
    cache_path: str | Path | None = None,
    same_day_only: bool = True,
    statuses: tuple[str, ...] | None = None,
) -> list[PriceRecord]:
    target = market_cache_path(cache_path)
    if not target.exists():
        return []
    if target.stat().st_size > MAX_MARKET_CACHE_BYTES:
        return []
    payload = json.loads(target.read_text(encoding="utf-8-sig"))
    if statuses is not None:
        stored_statuses = payload.get("statuses", [])
        if not isinstance(stored_statuses, list):
            return []
        expected = [status.lower() for status in statuses]
        actual = [str(status).lower() for status in stored_statuses]
        if actual != expected:
            return []
    rows = payload.get("prices", [])
    if not isinstance(rows, list):
        return []
    prices: list[PriceRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = PriceRecord(**row)
        except TypeError:
            continue
        if same_day_only and not _is_today(price.last_updated):
            continue
        prices.append(price)
    return prices


def _fetch_single_price(
    client: WarframeMarketClient,
    item: ItemRecord,
    platform: str,
    statuses: tuple[str, ...],
    updated_at: str,
    rate_limiter: _RequestRateLimiter,
) -> PriceRecord | None:
    rate_limiter.wait()
    payload = client.top_orders(item.market_slug)
    return _price_from_top_orders(item, payload, platform, statuses, updated_at)


def _unique_market_items(items: list[ItemRecord]) -> list[ItemRecord]:
    seen: set[str] = set()
    unique: list[ItemRecord] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique


def _price_from_top_orders(
    item: ItemRecord,
    payload: dict[str, Any],
    platform: str,
    statuses: tuple[str, ...],
    updated_at: str,
) -> PriceRecord | None:
    sells = payload.get("data", {}).get("sell", [])
    if not isinstance(sells, list):
        return None
    status_set = {status.lower() for status in statuses}
    values: list[float] = []
    for order in sells:
        if not isinstance(order, dict):
            continue
        user = order.get("user", {})
        user_status = str(user.get("status", "")).lower() if isinstance(user, dict) else ""
        if status_set and user_status not in status_set:
            continue
        if str(order.get("type", "")).lower() != "sell":
            continue
        if order.get("visible") is False:
            continue
        try:
            platinum = float(order["platinum"])
        except (KeyError, TypeError, ValueError):
            continue
        values.append(platinum)
    if not values:
        return None
    values.sort()
    return PriceRecord(
        item_id=item.id,
        platform=platform,
        currency="platinum",
        plat_price_min=values[0],
        plat_price_median=float(statistics.median(values)),
        plat_price_avg=sum(values) / len(values),
        volume_48h=0,
        orders_seen=len(values),
        last_updated=updated_at,
        source="warframe_market_v2_top",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_today(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return False
    return parsed.astimezone().date() == datetime.now().astimezone().date()


def _read_response_limited(response: Any, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError("market response too large")
    return body


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.5, min(seconds, 10.0))


def _validate_market_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_MARKET_HOSTS:
        raise RuntimeError(f"unsupported Warframe Market URL: {url}")


def windows_hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def windows_curl_executable() -> str:
    if os.name != "nt":
        return "curl"
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "curl.exe"
    if not candidate.exists():
        raise RuntimeError(f"trusted Windows curl.exe not found: {candidate}")
    return str(candidate)
