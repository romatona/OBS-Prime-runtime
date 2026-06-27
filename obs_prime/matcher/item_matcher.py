from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..models import ItemRecord, MatchResult
from .normalize import normalize_text


class ItemMatcher:
    def __init__(self, items: list[ItemRecord], corrections: dict[str, str] | None = None) -> None:
        self.items = items
        self.corrections = corrections or {}
        self.by_id = {item.id: item for item in items}
        self.index: list[tuple[str, ItemRecord, str]] = []
        for item in items:
            self.index.append((normalize_text(item.ko_name), item, "exact_ko"))
            self.index.append((normalize_text(item.en_name), item, "exact_en"))
            for alias in item.aliases:
                self.index.append((normalize_text(alias), item, "alias"))

    def match(self, raw_text: str) -> MatchResult:
        normalized_candidates = _normalized_candidates(raw_text)
        normalized = normalized_candidates[0] if normalized_candidates else ""
        if not normalized_candidates:
            return MatchResult(None, 0.0, "empty", normalized)
        for candidate in normalized_candidates:
            corrected_id = self.corrections.get(candidate)
            if corrected_id and corrected_id in self.by_id:
                return MatchResult(self.by_id[corrected_id], 1.0, "manual_correction", candidate)
        for candidate in normalized_candidates:
            for key, item, method in self.index:
                if candidate == key:
                    return MatchResult(item, 1.0, method, candidate)
        best: tuple[float, ItemRecord | None, str] = (0.0, None, "unmatched")
        candidates = []
        best_normalized = normalized
        for candidate in normalized_candidates:
            for key, item, method in self.index:
                score = SequenceMatcher(None, candidate, key).ratio()
                candidates.append({"item_id": item.id, "name": item.en_name, "score": round(score, 3), "method": method, "text": candidate})
                if score > best[0]:
                    best = (score, item, "fuzzy")
                    best_normalized = candidate
        candidates.sort(key=lambda c: c["score"], reverse=True)
        if best[1] is None or best[0] < 0.65:
            return MatchResult(None, best[0], "unmatched", best_normalized, candidates[:5])
        return MatchResult(best[1], best[0], best[2], best_normalized, candidates[:5])


def _normalized_candidates(raw_text: str) -> list[str]:
    values = [raw_text, raw_text.replace("\n", " ")]
    values.extend(line for line in raw_text.splitlines() if line.strip())
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        for variant in _strip_reward_quantity(value):
            normalized = normalize_text(variant)
            for candidate in (normalized, normalized.replace(" ", "_"), normalized.replace("_", " ")):
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
    return candidates


def _strip_reward_quantity(value: str) -> list[str]:
    stripped = value.strip()
    variants = [stripped]
    for pattern in (r"^\s*\d+\s*[xX×]\s*", r"^\s*\d+\s+", r"^[^0-9A-Za-z가-힣]+"):
        candidate = re.sub(pattern, "", stripped).strip()
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants
