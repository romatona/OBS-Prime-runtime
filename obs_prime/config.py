from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import CONFIG_DIR, resolve_project_path

MAX_CONFIG_BYTES = 1024 * 1024


DEFAULT_CONFIG: dict[str, Any] = {
    "warframe_client_language": "ko",
    "ui": {
        "dark_mode": False,
    },
    "capture": {
        "mode": "sample_image",
        "monitor_index": 0,
        "window_title_hint": "Warframe",
        "sample_image_path": "",
        "save_debug_capture": False,
    },
    "auto": {
        "enabled": False,
        "detect_interval_ms": 3000,
        "confidence_threshold": 0.86,
        "cooldown_ms": 3000,
        "detector_preset": "default-virtual-1080p",
        "min_ocr_slots_for_output": 2,
    },
    "roi": {
        "preset": "default-virtual-1080p",
        "ui_scale": 1.0,
        "slot_labels": ["1번 칸", "2번 칸", "3번 칸", "4번 칸"],
        "slot_name_rects": [],
    },
    "hotkey": {
        "enabled": True,
        "combo": "ctrl+alt+r",
        "debounce_ms": 1500,
        "action": "capture_analyze_overlay",
        "register_global": True,
        "last_registration_error": "",
    },
    "ocr": {
        "provider": "paddleocr_v5",
        "language": "kor+eng",
        "timeout_ms": 1000,
        "min_confidence": 0.8,
        "preprocessing_preset": "default-korean-ui",
        "obs_name_band_enabled": False,
        "obs_name_band_top_ratio": 0.46,
        "obs_name_band_height_ratio": 0.52,
    },
    "overlay": {
        "enabled": True,
        "mode": "window",
        "layout": "horizontal",
        "always_on_top": True,
        "click_through": False,
        "position_preset": "top-right",
        "x": 20,
        "y": 80,
        "w": 900,
        "h": 180,
        "opacity": 0.92,
        "clear_after_ms": 6000,
    },
    "obs_websocket": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 4455,
        "connect_timeout_ms": 3000,
        "password_dpapi": "",
        "ocr_source_name": "이미지",
        "browser_sources": ["B1", "B2", "B3", "B4"],
        "text_sources": ["T1", "T2", "T3", "T4"],
        "text_sources_enabled": [True, True, True, True],
        "browser_source_rects": [],
        "screenshot_format": "jpg",
        "screenshot_jpeg_quality": 100,
        "text_clear_after_ms": 7000,
    },
    "data": {
        "item_fixture": "warframe_prime_fixture.json",
        "price_max_age_hours": 24,
        "platform": "pc",
        "item_db_path": "",
        "price_db_path": "data/market_cache/warframe_market_prices.json",
        "reward_history_path": "data/reward_results.json",
        "item_wiki_dir": "data/item_wiki",
        "market_wiki_dir": "data/market_wiki",
        "market_live_enabled": True,
        "market_live_timeout_ms": 1500,
        "market_cache_same_day_only": True,
        "market_language": "ko",
        "market_crossplay": True,
        "market_order_statuses": ["ingame"],
        "wfcd_relics_url": "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Relics.json",
        "warframe_market_items_url": "https://api.warframe.market/v2/items",
    },
    "matching": {
        "confident_threshold": 0.92,
        "usable_threshold": 0.80,
        "uncertain_threshold": 0.65,
        "enable_alias_learning": False,
        "correction_store_path": "data/corrections.json",
    },
    "diagnostics": {
        "enabled": False,
        "artifact_dir": "debug",
        "sample_set_dir": "samples\\reward_screens",
    },
}


@dataclass
class AppConfig:
    data: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_CONFIG))
    path: Path = field(default_factory=lambda: CONFIG_DIR / "default.json")
    dirty: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        cfg_path = resolve_project_path(path or CONFIG_DIR / "default.json")
        if not cfg_path.exists():
            return cls(path=cfg_path)
        if cfg_path.stat().st_size > MAX_CONFIG_BYTES:
            raise RuntimeError(f"config file is too large: {cfg_path}")
        loaded = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        merged = deepcopy(DEFAULT_CONFIG)
        _deep_update(merged, loaded)
        return cls(data=merged, path=cfg_path, dirty=False)

    def save(self) -> None:
        self.path = resolve_project_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            backup = self.path.with_suffix(self.path.suffix + f".{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bak")
            _write_redacted_config_backup(self.path, backup)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.dirty = False

    def section(self, name: str) -> dict[str, Any]:
        return self.data.setdefault(name, {})

    def set_value(self, section: str, key: str, value: Any) -> None:
        if self.data.setdefault(section, {}).get(key) != value:
            self.data[section][key] = value
            self.dirty = True


def _deep_update(base: dict[str, Any], patch: dict[str, Any]) -> None:
    if not isinstance(patch, dict):
        return
    for key, value in patch.items():
        if key not in base:
            base[key] = value
            continue
        current = base.get(key)
        if isinstance(current, dict):
            if isinstance(value, dict):
                _deep_update(current, value)
            continue
        if isinstance(current, list):
            if isinstance(value, list):
                base[key] = value
            continue
        if isinstance(value, dict):
            continue
        base[key] = value


def _write_redacted_config_backup(source: Path, backup: Path) -> None:
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        payload = {
            "backup_status": "redacted_unreadable_source",
            "source_name": source.name,
            "error": str(exc),
        }
    _redact_sensitive_values(payload)
    backup.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _redact_sensitive_values(value: Any) -> None:
    sensitive_names = {"password", "api_key", "apikey", "token", "secret"}
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in sensitive_names:
                value[key] = ""
            else:
                _redact_sensitive_values(child)
    elif isinstance(value, list):
        for child in value:
            _redact_sensitive_values(child)
