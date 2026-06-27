from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping

from ..models import RewardResult


FLAG_LABELS = {
    "PINNED": "고정",
    "NEEDED": "필요",
    "BEST_PLAT": "최고 플래티넘",
    "BEST_DUCAT": "최고 두캇",
    "BEST_RATIO": "최고 효율",
    "LOW_CONFIDENCE": "낮은 신뢰도",
    "UNMATCHED": "미매칭",
    "STALE_PRICE": "가격 오래됨",
}


class OverlayProvider(ABC):
    @abstractmethod
    def render(self, rewards: list[RewardResult]) -> str:
        raise NotImplementedError


class ConsoleOverlayProvider(OverlayProvider):
    def render(self, rewards: list[RewardResult]) -> str:
        lines = []
        for reward in rewards:
            plat = "-" if reward.plat_price is None else f"{reward.plat_price:g}플"
            ducats = "-" if reward.ducats is None else f"{reward.ducats}두캇"
            flags = ", ".join(FLAG_LABELS.get(flag, flag) for flag in reward.recommendation_flags) if reward.recommendation_flags else "-"
            name = reward.matched_name or f"OCR:{reward.raw_ocr}"
            warn = f" !{reward.warning}" if reward.warning else ""
            lines.append(f"{reward.slot_index} {name:<28} {plat:>6} / {ducats:<5} {flags}{warn}")
        return "\n".join(lines)


class WindowOverlayProvider(OverlayProvider):
    def __init__(self, overlay_config: Mapping[str, object] | None = None) -> None:
        self.config = dict(overlay_config or {})

    def render(self, rewards: list[RewardResult]) -> str:
        layout = str(self.config.get("layout", "horizontal") or "horizontal").lower()
        ordered_rewards = sorted(rewards, key=lambda reward: reward.slot_index)
        if layout == "vertical":
            return "\n\n".join(_vertical_reward_line(reward) for reward in ordered_rewards)
        return "\n".join(_horizontal_reward_line(reward) for reward in ordered_rewards)


def build_overlay_provider(mode: str, overlay_config: Mapping[str, object] | None = None) -> OverlayProvider:
    if mode == "window":
        return WindowOverlayProvider(overlay_config)
    if mode in {"console", "disabled"}:
        return ConsoleOverlayProvider()
    raise ValueError(f"unsupported overlay mode: {mode}")


def _reward_name(reward: RewardResult) -> str:
    value = reward.matched_name or reward.matched_item_id or reward.normalized_text or reward.raw_ocr or "unknown"
    return " ".join(str(value).replace("_", " ").strip().split())


def _number(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _horizontal_reward_line(reward: RewardResult) -> str:
    flags = _compact_flags(reward)
    suffix = f" [{flags}]" if flags else ""
    return f"{_reward_name(reward)} {_number(reward.ducats)} ducat / {_number(reward.plat_price)} plat{suffix}"


def _vertical_reward_line(reward: RewardResult) -> str:
    flags = _compact_flags(reward)
    suffix = f"\n[{flags}]" if flags else ""
    return f"{_reward_name(reward)}\n{_number(reward.ducats)} ducat / {_number(reward.plat_price)} plat{suffix}"


def _compact_flags(reward: RewardResult) -> str:
    markers = []
    flag_set = set(reward.recommendation_flags)
    if "UNMATCHED" in flag_set:
        markers.append("MATCH?")
    if "LOW_CONFIDENCE" in flag_set:
        markers.append("OCR?")
    if "STALE_PRICE" in flag_set:
        markers.append("PRICE?")
    return " ".join(markers)
