from __future__ import annotations

import json
from pathlib import Path

from ..models import ItemRecord, UserItemState

MAX_FIXTURE_JSON_BYTES = 2 * 1024 * 1024


class ItemStore:
    def __init__(self, items: list[ItemRecord]) -> None:
        self.items = items
        self.by_id = {item.id: item for item in items}

    @classmethod
    def from_fixture(cls, path: Path) -> "ItemStore":
        payload = _read_fixture_payload(path)
        rows = payload.get("items", [])
        if not isinstance(rows, list):
            rows = []
        items: list[ItemRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                items.append(ItemRecord(**row))
            except TypeError:
                continue
        return cls(items)


class UserStateStore:
    def __init__(self, states: list[UserItemState]) -> None:
        self.states = states
        self.by_item = {state.item_id: state for state in states}

    @classmethod
    def from_fixture(cls, path: Path) -> "UserStateStore":
        payload = _read_fixture_payload(path)
        rows = payload.get("user_state", [])
        if not isinstance(rows, list):
            rows = []
        states: list[UserItemState] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                states.append(UserItemState(**row))
            except TypeError:
                continue
        return cls(states)

    def get(self, item_id: str) -> UserItemState | None:
        return self.by_item.get(item_id)


def _read_fixture_payload(path: Path) -> dict:
    if path.stat().st_size > MAX_FIXTURE_JSON_BYTES:
        raise RuntimeError(f"fixture JSON is too large: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"fixture JSON root must be an object: {path}")
    return payload
