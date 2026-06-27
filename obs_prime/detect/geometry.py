from __future__ import annotations

from ..models import Rect


def default_reward_geometry(width: int, height: int) -> tuple[Rect, list[Rect]]:
    panel_w = int(width * 0.78)
    panel_h = int(height * 0.34)
    panel_x = int((width - panel_w) / 2)
    panel_y = int(height * 0.34)
    slot_w = int(panel_w / 4)
    slot_h = int(panel_h * 0.42)
    slot_y = panel_y + int(panel_h * 0.25)
    slots = [
        Rect(panel_x + i * slot_w + int(slot_w * 0.10), slot_y, int(slot_w * 0.80), slot_h)
        for i in range(4)
    ]
    return Rect(panel_x, panel_y, panel_w, panel_h), slots
