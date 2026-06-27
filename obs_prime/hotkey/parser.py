from __future__ import annotations

from dataclasses import dataclass


MODIFIERS = {"ctrl", "control", "alt", "shift", "win"}
NORMAL_ALIASES = {"return": "enter", "esc": "escape", "spacebar": "space"}


@dataclass(frozen=True)
class HotkeyCombo:
    normalized: str
    modifiers: tuple[str, ...]
    key: str


def parse_hotkey(combo: str) -> HotkeyCombo:
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    parts = ["ctrl" if p == "control" else "win" if p == "cmd" else NORMAL_ALIASES.get(p, p) for p in parts]
    modifiers = tuple(p for p in parts if p in MODIFIERS)
    normal = [p for p in parts if p not in MODIFIERS]
    if len(normal) != 1:
        raise ValueError("단축키에는 일반 키가 정확히 1개 필요함")
    if len(set(modifiers)) < 2:
        raise ValueError("단축키에는 modifier 키가 최소 2개 필요함")
    if normal[0] in MODIFIERS or len(normal[0]) == 0:
        raise ValueError("단축키 일반 키가 올바르지 않음")
    ordered_mods = tuple(m for m in ("ctrl", "alt", "shift", "win") if m in set(modifiers))
    return HotkeyCombo("+".join([*ordered_mods, normal[0]]), ordered_mods, normal[0])
