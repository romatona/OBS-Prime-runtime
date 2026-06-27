from __future__ import annotations

from ..models import Recommendation, RewardResult


class RecommendationEngine:
    def __init__(self, usable_threshold: float = 0.80) -> None:
        self.usable_threshold = usable_threshold

    def score(self, rewards: list[RewardResult]) -> Recommendation:
        warnings: list[str] = []
        matched = [r for r in rewards if r.matched_item_id]
        for reward in rewards:
            if reward.match_score < self.usable_threshold:
                if "LOW_CONFIDENCE" not in reward.recommendation_flags:
                    reward.recommendation_flags.append("LOW_CONFIDENCE")
                reward.warning = reward.warning or "매칭 신뢰도 낮음"
                warnings.append(f"{reward.slot_index}번 칸 신뢰도 낮음")
            if reward.matched_item_id is None:
                if "UNMATCHED" not in reward.recommendation_flags:
                    reward.recommendation_flags.append("UNMATCHED")
                reward.warning = reward.warning or "OCR 미매칭"
        best_plat = _max_slot(matched, lambda r: r.plat_price if r.match_score >= self.usable_threshold else None)
        best_ducat = _max_slot(matched, lambda r: r.ducats if r.match_score >= self.usable_threshold else None)
        best_ratio = _max_slot(
            matched,
            lambda r: (r.plat_price / r.ducats) if r.plat_price is not None and r.ducats and r.match_score >= self.usable_threshold else None,
        )
        _flag(rewards, best_plat, "BEST_PLAT")
        _flag(rewards, best_ducat, "BEST_DUCAT")
        _flag(rewards, best_ratio, "BEST_RATIO")
        return Recommendation(best_plat, best_ducat, best_ratio, warnings)


def _max_slot(rewards: list[RewardResult], getter) -> int | None:
    best_slot: int | None = None
    best_value: float | int | None = None
    for reward in rewards:
        value = getter(reward)
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_value = value
            best_slot = reward.slot_index
    return best_slot


def _flag(rewards: list[RewardResult], slot: int | None, flag: str) -> None:
    if slot is None:
        return
    for reward in rewards:
        if reward.slot_index == slot and flag not in reward.recommendation_flags:
            reward.recommendation_flags.append(flag)
