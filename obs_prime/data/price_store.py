from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..models import PriceRecord
from .item_store import _read_fixture_payload
from .warframe_market import load_market_price_cache


class PriceStore:
    def __init__(self, prices: list[PriceRecord], max_age_hours: int = 24) -> None:
        self.prices = prices
        self.max_age_hours = max_age_hours
        self.by_item = {price.item_id: price for price in prices}

    @classmethod
    def from_fixture(cls, path: Path, max_age_hours: int, cache_path: str | Path | None = None) -> "PriceStore":
        payload = _read_fixture_payload(path)
        rows = payload.get("prices", [])
        if not isinstance(rows, list):
            rows = []
        prices: list[PriceRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                prices.append(PriceRecord(**row))
            except TypeError:
                continue
        live_prices = load_market_price_cache(cache_path)
        if live_prices:
            by_item = {price.item_id: price for price in prices}
            for price in live_prices:
                by_item[price.item_id] = price
            prices = list(by_item.values())
        return cls(prices, max_age_hours)

    def get(self, item_id: str) -> PriceRecord | None:
        return self.by_item.get(item_id)

    def is_stale(self, price: PriceRecord) -> bool:
        age = self.age_hours(price)
        return age is None or age > self.max_age_hours

    def age_hours(self, price: PriceRecord) -> float | None:
        try:
            updated = datetime.fromisoformat(price.last_updated.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - updated
            return age.total_seconds() / 3600
        except Exception:
            return None

    def oldest_age_hours(self) -> float | None:
        ages = [age for price in self.prices if (age := self.age_hours(price)) is not None]
        return max(ages) if ages else None
