from __future__ import annotations

import time

from ..models import CaptureFrame, OcrSlotResult, Rect
from .providers import OcrProvider


class RewardScreenOcr:
    def __init__(self, provider: OcrProvider, timeout_ms: int | None = None) -> None:
        self.provider = provider
        self.timeout_ms = timeout_ms

    def read_rewards(self, frame: CaptureFrame, slot_rects: list[Rect]) -> list[OcrSlotResult]:
        if len(slot_rects) != 4:
            raise ValueError("reward OCR expects exactly four slot rects")
        deadline = (time.monotonic() + self.timeout_ms / 1000) if self.timeout_ms and self.timeout_ms > 0 else None
        results: list[OcrSlotResult] = []
        for index, rect in enumerate(slot_rects, start=1):
            if deadline is not None and time.monotonic() > deadline:
                for remaining in range(index, 5):
                    results.append(OcrSlotResult(remaining, "", 0.0, slot_rects[remaining - 1]))
                break
            original_timeout = getattr(self.provider, "timeout_ms", None)
            if deadline is not None and isinstance(original_timeout, int):
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                setattr(self.provider, "timeout_ms", remaining_ms)
            try:
                results.append(self.provider.read_slot(frame, index, rect))
            except Exception as exc:
                results.append(OcrSlotResult(index, "", 0.0, rect, error=str(exc)))
            finally:
                if isinstance(original_timeout, int):
                    setattr(self.provider, "timeout_ms", original_timeout)
        return results
