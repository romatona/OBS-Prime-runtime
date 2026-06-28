from __future__ import annotations

from ..models import RewardResult


def format_obs_reward_text(reward: RewardResult | None, slot_width_px: int | None = None) -> str:
    if reward is None:
        return "0 Du / 0 pl"
    ducats = reward.ducats if reward.ducats is not None else 0
    plat = reward.plat_price if reward.plat_price is not None else 0
    value_line = f"{_format_number(ducats)} Du / {_format_number(plat)} pl"
    label = reward_display_name(reward)
    if not label:
        return value_line
    return "\n".join([value_line, *_wrap_label(label, slot_width_px)])


def format_item_wiki_reward_text(entry: dict[str, object] | None, price: object, raw_text: str, slot_width_px: int | None = None) -> str:
    if not isinstance(entry, dict):
        label = _compact_label(raw_text) or "미인식"
        return "\n".join(["0 Du / 0 pl", *_wrap_label(label, slot_width_px)])
    ducats = _optional_int(entry.get("ducats")) or 0
    if str(entry.get("slug", "")) == "forma_blueprint":
        plat_value: object = 0
    elif price is not None and hasattr(price, "plat_price_min"):
        plat_value = getattr(price, "plat_price_min")
    else:
        plat_value = entry.get("plat")
    value_line = f"{_format_number(ducats)} Du / {_format_number(plat_value)} pl"
    label = _item_wiki_display_name(entry)
    if not label:
        return value_line
    return "\n".join([value_line, *_wrap_label(label, slot_width_px)])


def reward_display_name(reward: RewardResult) -> str:
    value = (
        reward.matched_name
        or reward.matched_item_id
        or reward.normalized_text
        or reward.raw_ocr
        or ""
    )
    return " ".join(str(value).replace("_", " ").strip().split())


def _item_wiki_display_name(entry: dict[str, object]) -> str:
    name_kr = str(entry.get("name_kr") or "").strip()
    name_en = str(entry.get("name_en") or "").strip()
    slug = str(entry.get("slug") or "").replace("_", " ").strip()
    return " ".join((name_kr or name_en or slug or "unknown").split())


def _compact_label(raw_text: str) -> str:
    value = " ".join(line.strip() for line in raw_text.splitlines() if line.strip())
    return value[:40]


def _wrap_label(label: str, slot_width_px: int | None) -> list[str]:
    max_units = _slot_width_to_units(slot_width_px)
    words = label.split()
    if not words:
        return []

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if _visual_units(candidate) <= max_units:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if _visual_units(word) <= max_units:
            current = word
            continue
        lines.extend(_split_long_word(word, max_units))
    if current:
        lines.append(current)
    return lines or [label]


def _split_long_word(word: str, max_units: int) -> list[str]:
    parts: list[str] = []
    current = ""
    current_units = 0
    for char in word:
        units = _char_units(char)
        if current and current_units + units > max_units:
            parts.append(current)
            current = char
            current_units = units
        else:
            current += char
            current_units += units
    if current:
        parts.append(current)
    return parts


def _slot_width_to_units(slot_width_px: int | None) -> int:
    try:
        width = int(slot_width_px or 0)
    except (TypeError, ValueError):
        width = 0
    if width <= 0:
        return 18
    return min(24, max(10, width // 14))


def _visual_units(value: str) -> int:
    return sum(_char_units(char) for char in value)


def _char_units(char: str) -> int:
    return 1 if ord(char) < 128 else 2


def _format_number(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _optional_int(value: object) -> int | None:
    try:
        if value in {None, ""}:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["format_item_wiki_reward_text", "format_obs_reward_text", "reward_display_name"]
