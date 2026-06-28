from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..models import PipelineResult, RewardResult

MAX_REWARD_LEDGER_BYTES = 5 * 1024 * 1024


@dataclass
class RewardLedgerRow:
    id: str
    number: int
    date: str
    item: str
    ducats: int | None
    plat: float | None
    received: str = ""
    sell: str = ""
    use: str = ""
    item_id: str | None = None
    raw_ocr: str = ""
    normalized_text: str = ""
    trigger: str = ""
    batch_id: str = ""
    batch_fingerprint: str = ""
    match_score: float = 0.0
    match_method: str = ""
    created_at: str = ""


@dataclass
class RewardAppendResult:
    added_count: int
    duplicate: bool
    row_ids: list[str]
    fingerprint: str


class RewardLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._payload = self._load()

    def rows(self) -> list[RewardLedgerRow]:
        rows = self._payload.get("rows", [])
        if not isinstance(rows, list):
            return []
        return [_row_from_payload(row) for row in rows if isinstance(row, dict)]

    def filtered(self, *, received: bool = False, sell: bool = False, use: bool = False) -> list[RewardLedgerRow]:
        rows = self.rows()
        if not any([received, sell, use]):
            return rows
        filtered_rows = []
        for row in rows:
            if received and row.received.strip():
                filtered_rows.append(row)
                continue
            if sell and row.sell.strip():
                filtered_rows.append(row)
                continue
            if use and row.use.strip():
                filtered_rows.append(row)
                continue
        return filtered_rows

    def append_result(self, result: PipelineResult, trigger: str | None = None, result_date: str | None = None) -> RewardAppendResult:
        current_trigger = trigger or result.trigger
        fingerprint = reward_batch_fingerprint(result.rewards)
        if current_trigger == "auto" and fingerprint and self._payload.get("last_auto_fingerprint") == fingerprint:
            return RewardAppendResult(0, True, [], fingerprint)

        rows = self._payload.setdefault("rows", [])
        if not isinstance(rows, list):
            rows = []
            self._payload["rows"] = rows
        batch_id = uuid.uuid4().hex
        created_at = _timestamp()
        row_ids: list[str] = []
        for reward in sorted(result.rewards, key=lambda row: int(row.slot_index)):
            row = _row_from_reward(
                reward,
                result_date or time.strftime("%m_%d_%y"),
                current_trigger,
                batch_id,
                fingerprint,
                created_at,
            )
            rows.append(asdict(row))
            row_ids.append(row.id)
        if fingerprint:
            self._payload["last_auto_fingerprint"] = fingerprint
        self._save()
        return RewardAppendResult(len(row_ids), False, row_ids, fingerprint)

    def update_notes(self, row_id: str, *, received: str | None = None, sell: str | None = None, use: str | None = None) -> bool:
        changed = False
        rows = self._payload.get("rows", [])
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict) or str(row.get("id", "")) != row_id:
                continue
            for key, value in {"received": received, "sell": sell, "use": use}.items():
                if value is not None and str(row.get(key, "")) != value:
                    row[key] = value
                    changed = True
            break
        if changed:
            self._save()
        return changed

    def delete(self, row_id: str) -> bool:
        rows = self._payload.get("rows", [])
        if not isinstance(rows, list):
            return False
        kept = [row for row in rows if not isinstance(row, dict) or str(row.get("id", "")) != row_id]
        if len(kept) == len(rows):
            return False
        self._payload["rows"] = kept
        self._save()
        return True

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "last_auto_fingerprint": "", "rows": []}
        if self.path.stat().st_size > MAX_REWARD_LEDGER_BYTES:
            raise RuntimeError(f"reward ledger is too large: {self.path}")
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return {"version": 1, "last_auto_fingerprint": "", "rows": []}
        payload.setdefault("version", 1)
        payload.setdefault("last_auto_fingerprint", "")
        payload.setdefault("rows", [])
        return payload

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reward_batch_fingerprint(rewards: list[RewardResult]) -> str:
    parts = []
    for reward in sorted(rewards, key=lambda row: int(row.slot_index)):
        value = reward.matched_item_id or reward.normalized_text or reward.raw_ocr
        parts.append(f"{int(reward.slot_index)}:{str(value).strip().lower()}")
    return "|".join(parts)


def _row_from_reward(
    reward: RewardResult,
    result_date: str,
    trigger: str,
    batch_id: str,
    fingerprint: str,
    created_at: str,
) -> RewardLedgerRow:
    return RewardLedgerRow(
        id=uuid.uuid4().hex,
        number=int(reward.slot_index),
        date=result_date,
        item=reward.matched_name or reward.matched_item_id or reward.normalized_text or reward.raw_ocr,
        ducats=reward.ducats,
        plat=reward.plat_price,
        item_id=reward.matched_item_id,
        raw_ocr=reward.raw_ocr,
        normalized_text=reward.normalized_text,
        trigger=trigger,
        batch_id=batch_id,
        batch_fingerprint=fingerprint,
        match_score=reward.match_score,
        match_method=reward.match_method,
        created_at=created_at,
    )


def _row_from_payload(payload: dict[str, object]) -> RewardLedgerRow:
    return RewardLedgerRow(
        id=str(payload.get("id", "")),
        number=_optional_int(payload.get("number")) or 0,
        date=str(payload.get("date", "")),
        item=str(payload.get("item", "")),
        ducats=_optional_int(payload.get("ducats")),
        plat=_optional_float(payload.get("plat")),
        received=str(payload.get("received", "")),
        sell=str(payload.get("sell", "")),
        use=str(payload.get("use", "")),
        item_id=str(payload.get("item_id", "")) or None,
        raw_ocr=str(payload.get("raw_ocr", "")),
        normalized_text=str(payload.get("normalized_text", "")),
        trigger=str(payload.get("trigger", "")),
        batch_id=str(payload.get("batch_id", "")),
        batch_fingerprint=str(payload.get("batch_fingerprint", "")),
        match_score=_optional_float(payload.get("match_score")) or 0.0,
        match_method=str(payload.get("match_method", "")),
        created_at=str(payload.get("created_at", "")),
    )


def _optional_int(value: object) -> int | None:
    try:
        if value in {None, ""}:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
