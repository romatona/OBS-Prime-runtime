from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


CHANNEL_LABELS = {
    "CAPTURE": "캡처",
    "CONFIG": "설정",
    "DB": "DB",
    "DETECT": "감지",
    "ERR": "오류",
    "HOTKEY": "단축키",
    "LOG": "로그",
    "MATCH": "매칭",
    "OCR": "OCR",
    "OVERLAY": "오버레이",
    "PIPE": "파이프라인",
    "ROI": "ROI",
}
LEVEL_LABELS = {
    "INFO": "정보",
    "SUCCESS": "성공",
    "WARNING": "경고",
    "ERROR": "오류",
}
MAX_EVENT_LOG_ENTRIES = 1000
DPAPI_RE = re.compile(r"dpapi:[A-Za-z0-9+/=]+")


@dataclass
class EventLog:
    entries: list[str] = field(default_factory=list)

    def add(self, channel: str, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        channel_label = CHANNEL_LABELS.get(channel, channel)
        level_label = LEVEL_LABELS.get(level, level)
        safe_message = DPAPI_RE.sub("dpapi:REDACTED", str(message))
        self.entries.append(f"{stamp} [{channel_label}] {level_label} {safe_message}")
        if len(self.entries) > MAX_EVENT_LOG_ENTRIES:
            del self.entries[: len(self.entries) - MAX_EVENT_LOG_ENTRIES]

    def tail(self, count: int = 200) -> list[str]:
        return self.entries[-count:]
