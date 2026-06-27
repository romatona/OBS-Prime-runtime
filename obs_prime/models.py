from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class CaptureFrame:
    source: str
    path: str | None
    width: int
    height: int
    image: Any = None


@dataclass
class DetectorResult:
    detected: bool
    confidence: float
    preset_id: str
    screen_rect: Rect | None
    reward_panel_rect: Rect | None
    slot_rects: list[Rect]
    reason: str
    template_confidence: float | None = None
    duration_ms: int = 0


@dataclass
class OcrSlotResult:
    slot_index: int
    raw_text: str
    confidence: float
    rect: Rect
    crop_path: str | None = None
    error: str = ""


@dataclass
class ItemRecord:
    id: str
    ko_name: str
    en_name: str
    aliases: list[str]
    item_type: str
    rarity: str
    ducats: int
    market_slug: str
    vaulted: bool = False
    tradable: bool = True


@dataclass
class PriceRecord:
    item_id: str
    platform: str
    currency: str
    plat_price_min: float | None
    plat_price_median: float | None
    plat_price_avg: float | None
    volume_48h: int
    orders_seen: int
    last_updated: str
    source: str


@dataclass
class UserItemState:
    item_id: str
    owned_count: int = 0
    needed_count: int = 0
    pinned: bool = False
    manual_priority: int = 0
    notes: str = ""


@dataclass
class MatchResult:
    item: ItemRecord | None
    score: float
    method: str
    normalized_text: str
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RewardResult:
    slot_index: int
    slot_rect: Rect
    raw_ocr: str
    normalized_text: str
    matched_item_id: str | None
    matched_name: str | None
    match_score: float
    match_method: str
    plat_price: float | None
    ducats: int | None
    recommendation_flags: list[str] = field(default_factory=list)
    warning: str = ""
    crop_path: str | None = None


@dataclass
class Recommendation:
    best_plat_slot: int | None
    best_ducat_slot: int | None
    best_ratio_slot: int | None
    warnings: list[str]


@dataclass
class PipelineResult:
    trigger: str
    capture: dict[str, Any]
    detector: DetectorResult
    ocr: list[OcrSlotResult]
    rewards: list[RewardResult]
    recommendation: Recommendation
    overlay_payload: str
    debug_paths: dict[str, str]
    timings_ms: dict[str, int]
    total_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "capture": self.capture,
            "detector": {
                "detected": self.detector.detected,
                "confidence": self.detector.confidence,
                "preset_id": self.detector.preset_id,
                "screen_rect": self.detector.screen_rect.to_dict() if self.detector.screen_rect else None,
                "reward_panel_rect": self.detector.reward_panel_rect.to_dict() if self.detector.reward_panel_rect else None,
                "slot_rects": [r.to_dict() for r in self.detector.slot_rects],
                "reason": self.detector.reason,
                "template_confidence": self.detector.template_confidence,
                "duration_ms": self.detector.duration_ms,
            },
            "ocr": [asdict(o) for o in self.ocr],
            "rewards": [asdict(r) for r in self.rewards],
            "recommendation": asdict(self.recommendation),
            "overlay_payload": self.overlay_payload,
            "debug_paths": self.debug_paths,
            "timings_ms": self.timings_ms,
            "total_ms": self.total_ms,
        }
