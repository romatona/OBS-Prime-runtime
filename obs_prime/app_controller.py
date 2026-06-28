from __future__ import annotations

import time
import ctypes
from dataclasses import asdict
from io import BytesIO
import getpass
import os
from typing import Any

from .capture.samples import SampleCaptureProvider, validate_image_dimensions
from .capture.screen import ScreenCaptureProvider
from .config import AppConfig
from .data.fixtures import fixture_path, load_fixture_stores
from .data.item_wiki import item_wiki_version, probe_market_api, refresh_item_wiki
from .data.item_wiki_store import load_item_wiki_records, merge_item_records
from .data.item_store import ItemStore
from .data.warframe_market import fetch_market_prices_for_items, update_market_price_cache
from .detect.auto_detector import AutoDetector
from .detect.roi_presets import load_roi_preset
from .detect.templates import load_detector_preset
from .diagnostics.artifacts import ArtifactWriter
from .diagnostics.logging import EventLog
from .matcher.correction_store import CorrectionStore
from .matcher.item_matcher import ItemMatcher
from .models import CaptureFrame, DetectorResult, PipelineResult, Rect, RewardResult
from .obs.websocket_client import capture_obs_source_screenshot
from .ocr.name_band import apply_name_band
from .ocr.paddleocr_runtime import probe_paddleocr_v5
from .ocr.providers import OcrProvider, build_ocr_provider
from .ocr.presets import load_ocr_preset
from .ocr.reward_screen import RewardScreenOcr
from .ocr.tesseract_runtime import probe_tesseract
from .overlay.providers import build_overlay_provider
from .paths import ensure_project_dirs, resolve_project_path
from .recommend.engine import RecommendationEngine
from .security.secret_store import unprotect_secret
from .validation import validate_rects_in_bounds

MAX_SAMPLE_SET_IMAGES = 200


class PipelineController:
    def __init__(self, config: AppConfig | None = None, event_log: EventLog | None = None) -> None:
        ensure_project_dirs()
        self.config = config or AppConfig.load()
        self.log = event_log or EventLog()
        self.busy = False
        self._item_wiki_cache: tuple[str, float, list] | None = None
        self._ocr_provider_cache: tuple[tuple[str, str, str], OcrProvider] | None = None

    def _artifact_writer(self) -> ArtifactWriter:
        diagnostics_cfg = self.config.section("diagnostics")
        return ArtifactWriter(
            str(diagnostics_cfg.get("artifact_dir", "debug") or "debug"),
            enabled=bool(diagnostics_cfg.get("enabled", False)),
        )

    def _ocr_provider(self, ocr_cfg: dict[str, Any], timeout_ms: int) -> OcrProvider:
        provider_name = str(ocr_cfg.get("provider", "paddleocr_v5"))
        language = str(ocr_cfg.get("language", "kor+eng"))
        preprocessing = str(ocr_cfg.get("preprocessing_preset", "default-korean-ui"))
        key = (provider_name, language, preprocessing)
        if self._ocr_provider_cache is not None and self._ocr_provider_cache[0] == key:
            provider = self._ocr_provider_cache[1]
            if hasattr(provider, "timeout_ms"):
                provider.timeout_ms = timeout_ms
            return provider
        provider = build_ocr_provider(provider_name, language, timeout_ms, preprocessing)
        self._ocr_provider_cache = (key, provider)
        return provider

    def warm_ocr_provider(self) -> dict[str, Any]:
        started = time.perf_counter()
        ocr_cfg = self.config.section("ocr")
        timeout_ms = int(ocr_cfg.get("timeout_ms", 1000))
        provider = self._ocr_provider(ocr_cfg, timeout_ms)
        try:
            pipeline = getattr(provider, "_pipeline", None)
            if callable(pipeline):
                pipeline()
            status = "ready"
            error = ""
        except Exception as exc:
            status = "unavailable"
            error = str(exc)
        payload = {
            "stage": "ocr_prewarm",
            "provider": str(ocr_cfg.get("provider", "paddleocr_v5")),
            "status": status,
            "duration_ms": _elapsed_ms(started),
        }
        if error:
            payload["error"] = error
        self.log.add("OCR", "SUCCESS" if status == "ready" else "ERROR", f"prewarm {status}: {payload['duration_ms']}ms")
        return payload

    def run_cheap_detect(self, sample_path: str | None = None, stage: str = "cheap_detect", force_sample: bool = False) -> dict[str, Any]:
        """Run input-frame + detector only; auto mode uses this before OCR."""
        started = time.perf_counter()
        frame = self._capture(sample_path, force_sample=force_sample)
        auto_cfg = self.config.section("auto")
        preset = load_detector_preset(str(auto_cfg.get("detector_preset", "default-virtual-1080p")))
        threshold = float(auto_cfg.get("confidence_threshold", preset.threshold))
        detection = AutoDetector(preset).detect(frame, threshold)
        detection = self._apply_obs_rect_detection_override(frame, detection, threshold)
        total_ms = _elapsed_ms(started)
        self.log.add("DETECT", "SUCCESS" if detection.detected else "INFO", detection.reason)
        slot_rects = [rect.to_dict() for rect in detection.slot_rects]
        return {
            "stage": stage,
            "detected": detection.detected,
            "confidence": detection.confidence,
            "preset_id": detection.preset_id,
            "template_confidence": detection.template_confidence,
            "reason": detection.reason,
            "duration_ms": total_ms,
            "capture": {"source": frame.source, "path": frame.path, "width": frame.width, "height": frame.height},
            "reward_panel_rect": detection.reward_panel_rect.to_dict() if detection.reward_panel_rect else None,
            "rects": slot_rects,
            "slot_rects": slot_rects,
        }

    def run_auto_detect(self, sample_path: str | None = None, force_sample: bool = False) -> dict[str, Any]:
        return self.run_cheap_detect(sample_path=sample_path, stage="auto_detect", force_sample=force_sample)

    def run_sample_set(self, samples: str | None = None) -> dict[str, Any]:
        sample_dir = (
            samples
            if samples is not None
            else str(self.config.section("diagnostics").get("sample_set_dir", "samples\\reward_screens"))
        )
        try:
            root = resolve_project_path(sample_dir)
        except ValueError as exc:
            return {
                "status": "BLOCKED",
                "reason": "sample directory is outside project",
                "samples": sample_dir,
                "requested_samples": sample_dir,
                "error": str(exc),
                "required_action": "샘플 이미지를 프로젝트 samples\\reward_screens 안으로 복사하세요.",
                "sample_count": 0,
                "results": [],
                "failures": [],
            }
        suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
        paths = (
            sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)
            if root.exists()
            else []
        )
        if len(paths) > MAX_SAMPLE_SET_IMAGES:
            return {
                "status": "BLOCKED",
                "reason": "too many sample images",
                "samples": sample_dir,
                "requested_samples": sample_dir,
                "resolved_samples": str(root),
                "required_action": f"샘플 이미지를 {MAX_SAMPLE_SET_IMAGES}개 이하로 줄이세요.",
                "sample_count": len(paths),
                "max_sample_count": MAX_SAMPLE_SET_IMAGES,
                "results": [],
                "failures": [],
            }
        if not paths:
            return {
                "status": "BLOCKED",
                "reason": "no sample images found",
                "samples": sample_dir,
                "requested_samples": sample_dir,
                "resolved_samples": str(root),
                "required_action": "add Warframe reward screenshots under samples\\reward_screens",
                "sample_count": 0,
                "results": [],
                "failures": [],
            }
        results = []
        failures = []
        for path in paths:
            try:
                result = self.run_pipeline(trigger="sample", sample_path=str(path))
                results.append(
                    {
                        "sample": str(path),
                        "slot_count": len(result.rewards),
                        "best_plat_slot": result.recommendation.best_plat_slot,
                        "best_ducat_slot": result.recommendation.best_ducat_slot,
                        "total_ms": result.total_ms,
                    }
                )
            except Exception as exc:
                failures.append({"sample": str(path), "error": str(exc)})
        return {
            "status": "PASS" if not failures else "FAIL",
            "requested_samples": sample_dir,
            "resolved_samples": str(root),
            "sample_count": len(paths),
            "results": results,
            "failures": failures,
        }

    def run_stage(self, stage: str, sample_path: str | None = None) -> dict[str, Any]:
        if stage == "config_check":
            required = {
                "ui": ["dark_mode"],
                "capture": ["mode", "sample_image_path", "monitor_index"],
                "auto": [
                    "enabled",
                    "detect_interval_ms",
                    "confidence_threshold",
                    "cooldown_ms",
                    "detector_preset",
                    "min_ocr_slots_for_output",
                ],
                "hotkey": ["enabled", "combo", "debounce_ms", "register_global"],
                "ocr": [
                    "provider",
                    "language",
                    "timeout_ms",
                    "min_confidence",
                    "preprocessing_preset",
                    "obs_name_band_enabled",
                    "obs_name_band_top_ratio",
                    "obs_name_band_height_ratio",
                ],
                "overlay": ["enabled", "mode", "layout", "x", "y", "w", "h", "clear_after_ms"],
                "obs_websocket": [
                    "enabled",
                    "host",
                    "port",
                    "connect_timeout_ms",
                    "password_dpapi",
                    "ocr_source_name",
                    "browser_sources",
                    "text_sources",
                    "text_sources_enabled",
                    "browser_source_rects",
                    "screenshot_format",
                    "screenshot_jpeg_quality",
                    "text_clear_after_ms",
                ],
                "data": [
                    "item_fixture",
                    "price_max_age_hours",
                    "platform",
                    "price_db_path",
                    "reward_history_path",
                    "item_wiki_dir",
                    "market_wiki_dir",
                    "market_live_enabled",
                    "market_live_timeout_ms",
                    "market_cache_same_day_only",
                    "market_language",
                    "market_crossplay",
                    "market_order_statuses",
                    "wfcd_relics_url",
                    "warframe_market_items_url",
                ],
                "roi": ["preset", "ui_scale"],
                "matching": ["confident_threshold", "usable_threshold", "uncertain_threshold", "correction_store_path"],
                "diagnostics": ["enabled", "artifact_dir", "sample_set_dir"],
            }
            missing = [name for name in required if name not in self.config.data]
            missing_fields = [
                f"{section}.{field}"
                for section, fields in required.items()
                if section in self.config.data
                for field in fields
                if field not in self.config.data.get(section, {})
            ]
            value_errors = _config_value_errors(self.config.data)
            value_warnings = _config_value_warnings(self.config.data)
            status = "PASS" if not missing and not missing_fields and not value_errors else "FAIL"
            self.log.add("CONFIG", "SUCCESS" if status == "PASS" else "ERROR", f"config check {status}")
            return {
                "stage": stage,
                "status": status,
                "missing_sections": missing,
                "missing_fields": missing_fields,
                "value_errors": value_errors,
                "value_warnings": value_warnings,
            }
        if stage == "capture_test":
            frame = self._capture(sample_path)
            self.log.add("CAPTURE", "SUCCESS", f"{frame.source} {frame.width}x{frame.height}")
            return {"stage": stage, "source": frame.source, "path": frame.path, "width": frame.width, "height": frame.height}
        if stage == "detector_test":
            frame = self._capture(sample_path)
            auto_cfg = self.config.section("auto")
            preset = load_detector_preset(str(auto_cfg.get("detector_preset", "default-virtual-1080p")))
            detection = AutoDetector(preset).detect(frame, float(auto_cfg.get("confidence_threshold", preset.threshold)))
            self.log.add("DETECT", "SUCCESS" if detection.detected else "WARNING", detection.reason)
            return {
                "stage": stage,
                "detected": detection.detected,
                "confidence": detection.confidence,
                "preset_id": detection.preset_id,
                "template_confidence": detection.template_confidence,
                "reason": detection.reason,
                "reward_panel_rect": detection.reward_panel_rect.to_dict() if detection.reward_panel_rect else None,
                "slot_rects": [rect.to_dict() for rect in detection.slot_rects],
            }
        if stage == "roi_test":
            frame = self._capture(sample_path)
            auto_cfg = self.config.section("auto")
            preset = load_detector_preset(str(auto_cfg.get("detector_preset", "default-virtual-1080p")))
            detection = AutoDetector(preset).detect(frame, float(auto_cfg.get("confidence_threshold", preset.threshold)))
            slot_rects = self._resolve_roi_slot_rects(frame, detection.slot_rects)
            roi_preset = load_roi_preset(str(self.config.section("roi").get("preset", "default-virtual-1080p")))
            validation = validate_rects_in_bounds(slot_rects, frame.width, frame.height)
            artifact = self._artifact_writer()
            slot_manifest = []
            for index, rect in enumerate(slot_rects, start=1):
                crop_path = artifact.write_crop(f"slot_{index}.png", frame.image, rect)
                slot_manifest.append(
                    {
                        "slot_index": index,
                        "rect": rect.to_dict(),
                        "crop_path": crop_path,
                        "status": "saved" if crop_path else ("OK" if validation.ok else "CHECK"),
                    }
                )
            manifest_path = artifact.write_json("slot_crops.json", slot_manifest)
            self.log.add("ROI", "SUCCESS" if validation.ok else "ERROR", f"slot rects written: {manifest_path}")
            return {
                "stage": stage,
                "ok": validation.ok,
                "errors": validation.errors,
                "roi_preset": roi_preset.preset_id,
                "slot_crops": manifest_path,
                "slot_rects": slot_manifest,
            }
        if stage == "ocr_test":
            frame = self._capture(sample_path)
            auto_cfg = self.config.section("auto")
            preset = load_detector_preset(str(auto_cfg.get("detector_preset", "default-virtual-1080p")))
            detection = AutoDetector(preset).detect(frame, float(auto_cfg.get("confidence_threshold", preset.threshold)))
            slot_rects = self._resolve_roi_slot_rects(frame, detection.slot_rects)
            ocr_cfg = self.config.section("ocr")
            timeout_ms = int(ocr_cfg.get("timeout_ms", 1000))
            min_ocr_confidence = float(ocr_cfg.get("min_confidence", 0.8))
            provider = self._ocr_provider(ocr_cfg, timeout_ms)
            ocr = RewardScreenOcr(provider, timeout_ms).read_rewards(frame, slot_rects)
            if any(slot.raw_text == "" for slot in ocr):
                self.log.add("OCR", "WARNING", f"partial OCR results (timeout: {timeout_ms}ms)")
            low_ocr_slots = [slot.slot_index for slot in ocr if slot.confidence < min_ocr_confidence]
            if low_ocr_slots:
                self.log.add("OCR", "WARNING", f"low OCR confidence slots: {low_ocr_slots}")
            artifact = self._artifact_writer()
            raw_path = artifact.write_text("raw_ocr.txt", "\n".join(slot.raw_text for slot in ocr))
            self.log.add("OCR", "SUCCESS", f"{len(ocr)} slots read")
            return {
                "stage": stage,
                "slot_count": len(ocr),
                "raw_ocr_path": raw_path,
                "min_confidence": min_ocr_confidence,
                "low_confidence_slots": low_ocr_slots,
                "ocr": [asdict(slot) for slot in ocr],
            }
        if stage == "ocr_check":
            ocr_cfg = self.config.section("ocr")
            provider = str(ocr_cfg.get("provider", "paddleocr_v5"))
            language = str(ocr_cfg.get("language", "kor+eng"))
            timeout_ms = int(ocr_cfg.get("timeout_ms", 1000))
            min_ocr_confidence = float(ocr_cfg.get("min_confidence", 0.8))
            preset = load_ocr_preset(str(ocr_cfg.get("preprocessing_preset", "default-korean-ui")))
            if provider == "tesseract":
                try:
                    import pytesseract
                except Exception as exc:
                    self.log.add("OCR", "ERROR", f"tesseract unavailable: {exc}")
                    return {"stage": stage, "provider": provider, "language": language, "status": "unavailable", "error": str(exc)}
                _ = pytesseract
                runtime = probe_tesseract(language, timeout_ms)
                if runtime.status != "ready":
                    self.log.add("OCR", "ERROR", f"tesseract runtime unavailable: {runtime.error}")
                    return {
                        "stage": stage,
                        "provider": provider,
                        "language": language,
                        "status": "unavailable",
                        "error": runtime.error,
                        "tesseract_executable": runtime.executable,
                        "tessdata_prefix": runtime.tessdata_prefix,
                        "tesseract_version": runtime.version,
                        "available_languages": runtime.available_languages,
                    }
                self.log.add("OCR", "SUCCESS", f"tesseract ready for {language}")
                return {
                    "stage": stage,
                    "provider": provider,
                    "language": language,
                    "status": "ready",
                    "tesseract_executable": runtime.executable,
                    "tessdata_prefix": runtime.tessdata_prefix,
                    "tesseract_version": runtime.version,
                    "available_languages": runtime.available_languages,
                    "preset": preset.preset_id,
                    "preprocessing": preset.preprocessing_preset,
                    "min_confidence": min_ocr_confidence,
                }
            if provider == "paddleocr_v5":
                runtime = probe_paddleocr_v5(language)
                if runtime.status != "ready":
                    self.log.add("OCR", "ERROR", f"paddleocr_v5 runtime unavailable: {runtime.error}")
                    return {
                        "stage": stage,
                        "provider": provider,
                        "language": language,
                        "status": "unavailable",
                        "error": runtime.error,
                        "paddleocr_version": runtime.package_version,
                        "paddlepaddle_version": runtime.paddle_version,
                        "paddle_language": runtime.language,
                        "ocr_version": runtime.ocr_version,
                        "detection_model": runtime.detection_model,
                        "recognition_model": runtime.recognition_model,
                        "preset": preset.preset_id,
                        "preprocessing": preset.preprocessing_preset,
                        "min_confidence": min_ocr_confidence,
                    }
                self.log.add("OCR", "SUCCESS", f"paddleocr_v5 ready for {runtime.language}")
                return {
                    "stage": stage,
                    "provider": provider,
                    "language": language,
                    "status": "ready",
                    "paddleocr_version": runtime.package_version,
                    "paddlepaddle_version": runtime.paddle_version,
                    "paddle_language": runtime.language,
                    "ocr_version": runtime.ocr_version,
                    "detection_model": runtime.detection_model,
                    "recognition_model": runtime.recognition_model,
                    "preset": preset.preset_id,
                    "preprocessing": preset.preprocessing_preset,
                    "min_confidence": min_ocr_confidence,
                }
            if provider == "windows_ocr":
                if os.name != "nt":
                    self.log.add("OCR", "ERROR", "windows_ocr requires Windows runtime")
                    return {
                        "stage": stage,
                        "provider": provider,
                        "language": language,
                        "status": "unavailable",
                        "error": "windows_ocr requires Windows",
                    }
                try:
                    import winrt  # type: ignore
                    _ = winrt.windows.media.ocr  # type: ignore[attr-defined]
                    import winrt.windows.globalization  # type: ignore
                except Exception as exc:
                    self.log.add("OCR", "ERROR", f"windows_ocr module unavailable: {exc}")
                    return {
                        "stage": stage,
                        "provider": provider,
                        "language": language,
                        "status": "unavailable",
                        "error": str(exc),
                    }
                self.log.add("OCR", "SUCCESS", "windows_ocr runtime module detected")
                return {
                    "stage": stage,
                    "provider": provider,
                    "language": language,
                    "status": "ready",
                    "preset": preset.preset_id,
                    "preprocessing": preset.preprocessing_preset,
                    "min_confidence": min_ocr_confidence,
                }
            self.log.add("OCR", "ERROR", f"unsupported OCR provider: {provider}")
            return {"stage": stage, "provider": provider, "language": language, "status": "unsupported"}
        if stage == "db_test":
            data_cfg = self.config.section("data")
            item_store, price_store, user_state_store = load_fixture_stores(
                str(data_cfg.get("item_fixture", "warframe_prime_fixture.json")),
                int(data_cfg.get("price_max_age_hours", 24)),
                str(data_cfg.get("price_db_path", "")),
            )
            stale_count = sum(1 for price in price_store.prices if price_store.is_stale(price))
            alias_count = sum(len(item.aliases) for item in item_store.items)
            wiki_status = item_wiki_version(str(data_cfg.get("item_wiki_dir", "data/item_wiki")))
            self.log.add("DB", "SUCCESS", f"{len(item_store.items)} items, {len(price_store.prices)} prices")
            return {
                "stage": stage,
                "item_count": len(item_store.items),
                "alias_count": alias_count,
                "price_count": len(price_store.prices),
                "price_cache_path": str(data_cfg.get("price_db_path", "")),
                "user_state_count": len(user_state_store.states),
                "stale_price_count": stale_count,
                "oldest_price_age_hours": round(price_store.oldest_age_hours() or 0, 2),
                "item_wiki_status": wiki_status.get("status"),
                "item_wiki_version": wiki_status.get("version"),
                "item_wiki_count": wiki_status.get("count", 0),
                "item_wiki_index_path": wiki_status.get("index_path"),
            }
        if stage == "market_api_probe":
            data_cfg = self.config.section("data")
            statuses_raw = data_cfg.get("market_order_statuses", ["ingame"])
            statuses = tuple(str(value).lower() for value in statuses_raw) if isinstance(statuses_raw, list) else ("ingame",)
            result = probe_market_api(
                platform=str(data_cfg.get("platform", "pc")),
                language=str(data_cfg.get("market_language", "ko")),
                crossplay=bool(data_cfg.get("market_crossplay", True)),
                timeout=10.0,
                statuses=statuses,
            )
            level = "SUCCESS" if result.get("status") == "PASS" else "ERROR"
            self.log.add("DB", level, f"market API probe {result.get('status')} {result.get('lowest_plat_display', '-')}")
            return result
        if stage == "market_price_update":
            data_cfg = self.config.section("data")
            item_store = ItemStore.from_fixture(fixture_path(str(data_cfg.get("item_fixture", "warframe_prime_fixture.json"))))
            statuses_raw = data_cfg.get("market_order_statuses", ["ingame"])
            statuses = tuple(str(value).lower() for value in statuses_raw) if isinstance(statuses_raw, list) else ("ingame",)
            result = update_market_price_cache(
                item_store.items,
                str(data_cfg.get("price_db_path", "")),
                platform=str(data_cfg.get("platform", "pc")),
                language=str(data_cfg.get("market_language", "ko")),
                crossplay=bool(data_cfg.get("market_crossplay", True)),
                statuses=statuses,
                timeout=10.0,
            )
            level = "SUCCESS" if result.get("status") == "PASS" else "ERROR"
            self.log.add("DB", level, f"market price update {result.get('status')} updated={result.get('updated_count', 0)}")
            return result
        if stage == "ducat_db_update":
            data_cfg = self.config.section("data")
            result = refresh_item_wiki(
                output_dir=str(data_cfg.get("item_wiki_dir", "data/item_wiki")),
                price_cache_path=str(data_cfg.get("price_db_path", "")),
                wfcd_relics_url=str(data_cfg.get("wfcd_relics_url", "")),
                market_items_url=str(data_cfg.get("warframe_market_items_url", "")),
                timeout=45.0,
            )
            level = "SUCCESS" if result.get("status") == "PASS" else "ERROR"
            self.log.add("DB", level, f"ducat DB update {result.get('status')} items={result.get('item_count', 0)}")
            return result
        if stage == "market_wiki_update":
            from .data.market_wiki import refresh_market_wiki

            data_cfg = self.config.section("data")
            result = refresh_market_wiki(
                output_dir=str(data_cfg.get("market_wiki_dir", "data/market_wiki")),
                market_items_url=str(data_cfg.get("warframe_market_items_url", "")),
                timeout=45.0,
            )
            level = "SUCCESS" if result.get("status") == "PASS" else "ERROR"
            self.log.add("DB", level, f"market wiki update {result.get('status')} items={result.get('item_count', 0)}")
            return result
        if stage == "overlay_test":
            overlay_cfg = self.config.section("overlay")
            payload = build_overlay_provider(str(overlay_cfg.get("mode", "console")), overlay_cfg).render(_overlay_preview_rewards())
            self.log.add("OVERLAY", "SUCCESS", "preview payload rendered")
            return {"stage": stage, "payload": payload}
        raise ValueError(f"unknown stage: {stage}")

    def run_pipeline(self, trigger: str = "sample", sample_path: str | None = None) -> PipelineResult:
        if self.busy:
            raise RuntimeError("pipeline busy")
        self.busy = True
        started = time.perf_counter()
        timings: dict[str, int] = {}
        artifact = self._artifact_writer()
        debug_paths: dict[str, str] = {}
        try:
            capture_started = time.perf_counter()
            frame = self._capture(sample_path, force_sample=(trigger == "sample"))
            timings["capture_ms"] = _elapsed_ms(capture_started)
            debug_paths["capture"] = artifact.write_json(
                "capture.json",
                {"source": frame.source, "path": frame.path, "width": frame.width, "height": frame.height},
            )
            self.log.add("CAPTURE", "SUCCESS", f"{frame.source} {frame.width}x{frame.height}")

            detect_started = time.perf_counter()
            auto_cfg = self.config.section("auto")
            preset = load_detector_preset(str(auto_cfg.get("detector_preset", "default-virtual-1080p")))
            detector = AutoDetector(preset)
            threshold = float(auto_cfg.get("confidence_threshold", preset.threshold))
            detection = detector.detect(frame, threshold)
            detection = self._apply_obs_rect_detection_override(frame, detection, threshold)
            timings["detect_ms"] = _elapsed_ms(detect_started)
            debug_paths["detect"] = artifact.write_json(
                "detect.json",
                {
                    "detected": detection.detected,
                    "confidence": detection.confidence,
                    "preset_id": detection.preset_id,
                    "rects": [r.to_dict() for r in detection.slot_rects],
                    "reason": detection.reason,
                },
            )
            if not detection.detected and trigger == "auto":
                raise RuntimeError(f"auto detector did not pass threshold: {detection.reason}")
            slot_rects = self._resolve_roi_slot_rects(frame, detection.slot_rects)

            ocr_started = time.perf_counter()
            ocr_cfg = self.config.section("ocr")
            timeout_ms = int(ocr_cfg.get("timeout_ms", 1000))
            min_ocr_confidence = float(ocr_cfg.get("min_confidence", 0.8))
            provider = self._ocr_provider(ocr_cfg, timeout_ms)
            ocr = RewardScreenOcr(provider, timeout_ms).read_rewards(frame, slot_rects)
            if any(slot.raw_text == "" for slot in ocr):
                self.log.add("OCR", "WARNING", f"partial OCR results (timeout: {timeout_ms}ms)")
            low_ocr_slots = [slot.slot_index for slot in ocr if slot.confidence < min_ocr_confidence]
            if low_ocr_slots:
                self.log.add("OCR", "WARNING", f"low OCR confidence slots: {low_ocr_slots}")
            for slot in ocr:
                slot.crop_path = artifact.write_crop(f"slot_{slot.slot_index}.png", frame.image, slot.rect)
            timings["ocr_ms"] = _elapsed_ms(ocr_started)
            debug_paths["ocr"] = artifact.write_json("ocr.json", [asdict(slot) for slot in ocr])
            debug_paths["raw_ocr"] = artifact.write_text("raw_ocr.txt", "\n".join(slot.raw_text for slot in ocr))
            if trigger == "auto":
                min_slots = int(auto_cfg.get("min_ocr_slots_for_output", 2))
                recognized_slots = [slot.slot_index for slot in ocr if slot.raw_text.strip()]
                gate_payload = {
                    "trigger": trigger,
                    "minimum_slots": min_slots,
                    "recognized_slot_count": len(recognized_slots),
                    "recognized_slots": recognized_slots,
                    "passed": len(recognized_slots) >= min_slots,
                }
                debug_paths["auto_ocr_gate"] = artifact.write_json("auto_ocr_gate.json", gate_payload)
                if len(recognized_slots) < min_slots:
                    message = (
                        f"auto OCR gate skipped DB/output: "
                        f"{len(recognized_slots)}/{min_slots} slots recognized"
                    )
                    self.log.add("OCR", "WARNING", message)
                    raise RuntimeError(message)

            match_started = time.perf_counter()
            data_cfg = self.config.section("data")
            item_store, price_store, user_state_store = load_fixture_stores(
                str(data_cfg.get("item_fixture", "warframe_prime_fixture.json")),
                int(data_cfg.get("price_max_age_hours", 24)),
                str(data_cfg.get("price_db_path", "")),
            )
            wiki_items = self._load_item_wiki_records_cached(data_cfg)
            matcher_items = merge_item_records(wiki_items, item_store.items) if wiki_items else item_store.items
            if wiki_items:
                self.log.add("DB", "SUCCESS", f"item_wiki records loaded: {len(wiki_items)}")
            matcher = ItemMatcher(matcher_items, self._load_corrections())
            matched_slots = []
            match_candidates: list[dict[str, Any]] = []
            for slot in ocr:
                match = matcher.match(slot.raw_text)
                matched_slots.append((slot, match))
                match_candidates.append(
                    {
                        "slot_index": slot.slot_index,
                        "raw_ocr": slot.raw_text,
                        "normalized_text": match.normalized_text,
                        "selected_item_id": match.item.id if match.item else None,
                        "selected_name": match.item.en_name if match.item else None,
                        "score": round(match.score, 3),
                        "method": match.method,
                        "candidates": match.candidates,
                    }
                )
            timings["match_ms"] = _elapsed_ms(match_started)

            price_started = time.perf_counter()
            live_market_enabled = bool(data_cfg.get("market_live_enabled", True))
            live_price_by_item: dict[str, object] = {}
            if live_market_enabled:
                matched_items = []
                seen_market_items: set[str] = set()
                for _slot, match in matched_slots:
                    if match.item is None or not match.item.tradable:
                        continue
                    if match.item.id in seen_market_items:
                        continue
                    seen_market_items.add(match.item.id)
                    matched_items.append(match.item)
                statuses_raw = data_cfg.get("market_order_statuses", ["ingame"])
                statuses = tuple(str(value).lower() for value in statuses_raw) if isinstance(statuses_raw, list) else ("ingame",)
                if matched_items:
                    market_price_result = fetch_market_prices_for_items(
                        matched_items,
                        str(data_cfg.get("price_db_path", "")),
                        platform=str(data_cfg.get("platform", "pc")),
                        language=str(data_cfg.get("market_language", "ko")),
                        crossplay=bool(data_cfg.get("market_crossplay", True)),
                        statuses=statuses,
                        timeout=max(0.3, int(data_cfg.get("market_live_timeout_ms", 1500)) / 1000),
                        max_workers=4,
                        use_today_cache=bool(data_cfg.get("market_cache_same_day_only", True)),
                    )
                else:
                    market_price_result = {
                        "status": "SKIPPED",
                        "price_by_item": {},
                        "live_count": 0,
                        "fallback_count": 0,
                        "failure_count": 0,
                        "reason": "no tradable matched items",
                    }
                price_by_item = market_price_result.get("price_by_item", {})
                live_price_by_item = price_by_item if isinstance(price_by_item, dict) else {}
                market_debug = {key: value for key, value in market_price_result.items() if key != "price_by_item"}
                market_debug["prices"] = [asdict(price) for price in live_price_by_item.values()]
                debug_paths["market_prices"] = artifact.write_json("market_prices.json", market_debug)
                self.log.add(
                    "DB",
                    "SUCCESS" if market_price_result.get("status") == "PASS" else "WARNING",
                    (
                        "market live prices "
                        f"live={market_price_result.get('live_count', 0)} "
                        f"today_cache={market_price_result.get('fallback_count', 0)} "
                        f"fail={market_price_result.get('failure_count', 0)}"
                    ),
                )
            timings["price_ms"] = _elapsed_ms(price_started)

            rewards: list[RewardResult] = []
            for slot, match in matched_slots:
                if live_market_enabled and match.item and match.item.tradable:
                    price = live_price_by_item.get(match.item.id)
                else:
                    price = price_store.get(match.item.id) if match.item else None
                user_state = user_state_store.get(match.item.id) if match.item else None
                flags: list[str] = []
                warning = ""
                is_tradable = bool(match.item.tradable) if match.item else False
                plat_price = _display_plat_price(match.item, price)
                ducats = match.item.ducats if match.item else None
                if price and is_tradable and price_store.is_stale(price):
                    flags.append("STALE_PRICE")
                    warning = "가격 정보 오래됨"
                if live_market_enabled and match.item and is_tradable and price is None:
                    flags.append("STALE_PRICE")
                    warning = _append_warning(warning, "실시간 가격 없음")
                if user_state and user_state.pinned:
                    flags.append("PINNED")
                if user_state and user_state.needed_count > 0:
                    flags.append("NEEDED")
                if slot.confidence < min_ocr_confidence:
                    flags.append("LOW_CONFIDENCE")
                    warning = _append_warning(warning, f"OCR 신뢰도 낮음 {slot.confidence:.2f}")
                rewards.append(
                    RewardResult(
                        slot_index=slot.slot_index,
                        slot_rect=slot.rect,
                        raw_ocr=slot.raw_text,
                        normalized_text=match.normalized_text,
                        matched_item_id=match.item.id if match.item else None,
                        matched_name=match.item.ko_name if match.item else None,
                        match_score=round(match.score, 3),
                        match_method=match.method,
                        plat_price=plat_price,
                        ducats=ducats,
                        recommendation_flags=flags,
                        warning=warning,
                        crop_path=slot.crop_path,
                    )
                )
            debug_paths["match"] = artifact.write_json("match.json", [asdict(r) for r in rewards])
            debug_paths["match_candidates"] = artifact.write_json("match_candidates.json", match_candidates)
            debug_paths["slot_crops"] = artifact.write_json(
                "slot_crops.json",
                [
                    {
                        "slot_index": slot.slot_index,
                        "rect": slot.rect.to_dict(),
                        "crop_path": slot.crop_path,
                        "status": "image crop pending" if slot.crop_path is None else "saved",
                    }
                    for slot in ocr
                ],
            )

            recommend_started = time.perf_counter()
            matching_cfg = self.config.section("matching")
            recommendation = RecommendationEngine(float(matching_cfg.get("usable_threshold", 0.80))).score(rewards)
            timings["recommend_ms"] = _elapsed_ms(recommend_started)
            debug_paths["recommendation"] = artifact.write_json("recommendation.json", asdict(recommendation))

            overlay_started = time.perf_counter()
            overlay_cfg = self.config.section("overlay")
            overlay = build_overlay_provider(str(overlay_cfg.get("mode", "console")), overlay_cfg)
            overlay_payload = overlay.render(rewards)
            timings["overlay_ms"] = _elapsed_ms(overlay_started)
            debug_paths["overlay"] = artifact.write_text("overlay.txt", overlay_payload)

            total_ms = _elapsed_ms(started)
            timings["total_ms"] = total_ms
            for warning in _performance_warnings(trigger, total_ms):
                self.log.add("PIPE", "WARNING", warning)
            result = PipelineResult(
                trigger=trigger,
                capture={"source": frame.source, "path": frame.path, "width": frame.width, "height": frame.height},
                detector=detection,
                ocr=ocr,
                rewards=rewards,
                recommendation=recommendation,
                overlay_payload=overlay_payload,
                debug_paths=debug_paths,
                timings_ms=timings,
                total_ms=total_ms,
            )
            debug_paths["pipeline"] = artifact.write_json("pipeline.json", result.to_dict())
            self.log.add("PIPE", "SUCCESS", f"{trigger} completed in {total_ms}ms")
            debug_paths["events"] = artifact.write_text("events.log", "\n".join(self.log.tail()))
            return result
        finally:
            self.busy = False

    def _resolve_roi_slot_rects(self, frame, detected_slots: list[Rect]) -> list[Rect]:
        obs_cfg = self.config.section("obs_websocket")
        source_name = str(getattr(frame, "source", ""))
        is_virtual_sample = source_name in {"virtual_sample", "virtual"}
        if not is_virtual_sample:
            obs_rects = _parse_rect_list(obs_cfg.get("browser_source_rects", [])) if bool(obs_cfg.get("enabled", False)) else []
            if len(obs_rects) == 4:
                ocr_rects = apply_name_band(obs_rects, self.config.section("ocr"))
                validation = validate_rects_in_bounds(ocr_rects, frame.width, frame.height)
                if validation.ok:
                    if bool(self.config.section("ocr").get("obs_name_band_enabled", False)):
                        self.log.add("ROI", "SUCCESS", "using OBS B1-B4 adjusted name-band rects for OCR")
                    else:
                        self.log.add("ROI", "SUCCESS", "using OBS B1-B4 source rects directly for OCR")
                    return ocr_rects
                self.log.add("ROI", "WARNING", f"OBS B1-B4 rects invalid: {'; '.join(validation.errors)}")
            roi_cfg = self.config.section("roi")
            manual_rects = _parse_rect_list(roi_cfg.get("slot_name_rects", []))
            if manual_rects:
                validation = validate_rects_in_bounds(manual_rects, frame.width, frame.height)
                if validation.ok:
                    self.log.add("ROI", "SUCCESS", "using manual ROI slot rects from config")
                    return manual_rects
                self.log.add("ROI", "WARNING", f"manual ROI invalid: {'; '.join(validation.errors)}")
        else:
            self.log.add("ROI", "INFO", "virtual sample ignores live OBS/manual ROI")
        roi_cfg = self.config.section("roi")
        preset = load_roi_preset(str(roi_cfg.get("preset", "default-virtual-1080p")))
        if preset.slot_name_rects:
            validation = validate_rects_in_bounds(preset.slot_name_rects, frame.width, frame.height)
            if validation.ok:
                self.log.add("ROI", "SUCCESS", "using ROI preset slot rects")
                return preset.slot_name_rects
            self.log.add("ROI", "WARNING", f"preset ROI invalid: {'; '.join(validation.errors)}")
        self.log.add("ROI", "INFO", "using detector slot rects")
        return detected_slots

    def _load_item_wiki_records_cached(self, data_cfg: dict[str, Any]) -> list:
        item_wiki_dir = resolve_project_path(str(data_cfg.get("item_wiki_dir", "data/item_wiki")))
        index_path = item_wiki_dir / "_index.json"
        mtime = index_path.stat().st_mtime if index_path.exists() else 0.0
        cache_key = str(item_wiki_dir)
        if self._item_wiki_cache is not None:
            cached_key, cached_mtime, cached_records = self._item_wiki_cache
            if cached_key == cache_key and cached_mtime == mtime:
                return cached_records
        records = load_item_wiki_records(item_wiki_dir)
        self._item_wiki_cache = (cache_key, mtime, records)
        return records

    def _capture(self, sample_path: str | None, force_sample: bool = False):
        capture_cfg = self.config.section("capture")
        mode = capture_cfg.get("mode", "sample_image")
        if sample_path:
            return SampleCaptureProvider(sample_path).capture()
        if force_sample:
            return SampleCaptureProvider(str(capture_cfg.get("sample_image_path", ""))).capture()
        obs_cfg = self.config.section("obs_websocket")
        if bool(obs_cfg.get("enabled", False)) and str(obs_cfg.get("ocr_source_name", "")).strip():
            try:
                return self._capture_obs_source(obs_cfg)
            except Exception as exc:
                self.log.add("CAPTURE", "ERROR", f"OBS OCR 소스 캡쳐 실패: {exc}")
                raise RuntimeError(f"OBS OCR 소스 캡쳐 실패: {exc}") from exc
        if mode == "screen":
            return ScreenCaptureProvider(int(capture_cfg.get("monitor_index", 0))).capture()
        return SampleCaptureProvider(str(capture_cfg.get("sample_image_path", ""))).capture()

    def _capture_obs_source(self, obs_cfg: dict[str, Any]) -> CaptureFrame:
        try:
            from PIL import Image
        except Exception as exc:
            raise RuntimeError(f"Pillow 이미지 런타임 없음: {exc}") from exc
        password_secret = str(obs_cfg.get("password_dpapi", ""))
        try:
            password = unprotect_secret(password_secret) if password_secret else ""
        except Exception as exc:
            raise RuntimeError("OBS 비밀번호 복호화 실패: OBS 연결 페이지에서 비밀번호를 다시 입력하고 저장하세요") from exc
        result = capture_obs_source_screenshot(
            str(obs_cfg.get("host", "")).strip(),
            int(obs_cfg.get("port", 4455)),
            password,
            str(obs_cfg.get("ocr_source_name", "")).strip(),
            max(0.5, int(obs_cfg.get("connect_timeout_ms", 3000)) / 1000),
            image_format=str(obs_cfg.get("screenshot_format", "jpg") or "jpg"),
            image_compression_quality=int(obs_cfg.get("screenshot_jpeg_quality", 100)),
        )
        image_bytes = result.get("image_bytes")
        if result.get("status") != "captured" or not isinstance(image_bytes, bytes):
            raise RuntimeError(str(result.get("error", "OBS OCR 소스 캡쳐 실패")))
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        validate_image_dimensions(image.width, image.height, "OBS source screenshot")
        return CaptureFrame("obs_source", None, image.width, image.height, image)

    def _apply_obs_rect_detection_override(
        self,
        frame: CaptureFrame,
        detection: DetectorResult,
        threshold: float,
    ) -> DetectorResult:
        obs_cfg = self.config.section("obs_websocket")
        if not bool(obs_cfg.get("enabled", False)) or frame.source in {"virtual_sample", "virtual"}:
            return detection
        rects = _parse_rect_list(obs_cfg.get("browser_source_rects", []))
        if len(rects) != 4:
            return detection
        validation = validate_rects_in_bounds(rects, frame.width, frame.height)
        if not validation.ok:
            self.log.add("DETECT", "WARNING", f"OBS B1~B4 감지 보정 실패: {'; '.join(validation.errors)}")
            return detection
        reason = detection.reason
        if "OBS B1~B4 좌표 감지 보정" not in reason:
            reason = f"{reason}; OBS B1~B4 좌표 감지 보정" if reason else "OBS B1~B4 좌표 감지 보정"
        detection.detected = True
        detection.confidence = max(detection.confidence, min(0.95, max(threshold, 0.87)))
        detection.reason = reason
        detection.slot_rects = rects
        return detection

    def _load_corrections(self) -> dict[str, str]:
        path = resolve_project_path(self.config.section("matching").get("correction_store_path", "data/corrections.json"))
        if not path.exists():
            return {}
        return CorrectionStore(path).corrections


def _config_value_errors(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ui_cfg = data.get("ui", {}) if isinstance(data.get("ui", {}), dict) else {}
    if not isinstance(ui_cfg.get("dark_mode"), bool):
        errors.append("ui.dark_mode must be a boolean")

    obs_cfg = data.get("obs_websocket", {}) if isinstance(data.get("obs_websocket", {}), dict) else {}
    _require_int_range(errors, obs_cfg.get("port"), "obs_websocket.port", 1, 65535)
    _require_int_range(errors, obs_cfg.get("connect_timeout_ms"), "obs_websocket.connect_timeout_ms", 500, 30000)
    _require_int_range(errors, obs_cfg.get("screenshot_jpeg_quality"), "obs_websocket.screenshot_jpeg_quality", 1, 100)
    _require_int_range(errors, obs_cfg.get("text_clear_after_ms"), "obs_websocket.text_clear_after_ms", 500, 30000)
    for key, default_prefix in (("browser_sources", "B"), ("text_sources", "T")):
        value = obs_cfg.get(key)
        if not isinstance(value, list) or len(value) != 4:
            errors.append(f"obs_websocket.{key} must contain 4 source names")
            continue
        names = [str(item).strip() or f"{default_prefix}{index}" for index, item in enumerate(value, start=1)]
        if len({name.lower() for name in names}) != 4:
            errors.append(f"obs_websocket.{key} contains duplicate source names")
    enabled = obs_cfg.get("text_sources_enabled")
    if not isinstance(enabled, list) or len(enabled) != 4:
        errors.append("obs_websocket.text_sources_enabled must contain 4 booleans")

    auto_cfg = data.get("auto", {}) if isinstance(data.get("auto", {}), dict) else {}
    _require_int_range(errors, auto_cfg.get("detect_interval_ms"), "auto.detect_interval_ms", 500, 30000)
    _require_int_range(errors, auto_cfg.get("cooldown_ms"), "auto.cooldown_ms", 0, 30000)
    _require_int_range(errors, auto_cfg.get("min_ocr_slots_for_output"), "auto.min_ocr_slots_for_output", 1, 4)

    overlay_cfg = data.get("overlay", {}) if isinstance(data.get("overlay", {}), dict) else {}
    _require_int_range(errors, overlay_cfg.get("w"), "overlay.w", 50, 5000)
    _require_int_range(errors, overlay_cfg.get("h"), "overlay.h", 30, 3000)
    _require_int_range(errors, overlay_cfg.get("clear_after_ms"), "overlay.clear_after_ms", 500, 30000)
    return errors


def _config_value_warnings(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    obs_cfg = data.get("obs_websocket", {}) if isinstance(data.get("obs_websocket", {}), dict) else {}
    password_secret = str(obs_cfg.get("password_dpapi", "") or "")
    if bool(obs_cfg.get("enabled", False)) and password_secret:
        try:
            unprotect_secret(password_secret)
        except Exception as exc:
            current_user = _current_windows_user()
            warnings.append(
                "obs_websocket.password_dpapi cannot be decrypted for "
                f"current Windows user '{current_user}': {exc}. "
                "Run OBS prime as the same user that saved the password, or re-enter and save the OBS password."
            )
    return warnings


def _current_windows_user() -> str:
    if os.name == "nt":
        try:
            size = ctypes.c_ulong(256)
            buffer = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
                return buffer.value
        except Exception:
            pass
    return getpass.getuser()


def _require_int_range(errors: list[str], value: object, name: str, minimum: int, maximum: int) -> None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be an integer")
        return
    if not minimum <= parsed <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")


def _parse_rect_list(value: object) -> list[Rect]:
    if not isinstance(value, list):
        return []
    rects: list[Rect] = []
    for row in value:
        if not isinstance(row, dict):
            return []
        try:
            rects.append(Rect(int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"])))
        except (KeyError, TypeError, ValueError):
            return []
    return rects


def _display_plat_price(item, price) -> float | None:
    if item is None:
        return None
    if not bool(getattr(item, "tradable", True)):
        return 0
    if price is None:
        return None
    value = getattr(price, "plat_price_min", None)
    return value if value is not None else getattr(price, "plat_price_median", None)


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _performance_warnings(trigger: str, total_ms: int) -> list[str]:
    warnings: list[str] = []
    if trigger == "hotkey" and total_ms > 3000:
        warnings.append(f"hotkey target exceeded: {total_ms}ms > 3000ms")
    if trigger == "auto" and total_ms > 3500:
        warnings.append(f"auto target exceeded: {total_ms}ms > 3500ms")
    if total_ms > 5000:
        warnings.append(f"hard 5s target exceeded: {total_ms}ms")
    return warnings


def _append_warning(current: str, addition: str) -> str:
    if not current:
        return addition
    if addition in current:
        return current
    return f"{current}; {addition}"


def _overlay_preview_rewards() -> list[RewardResult]:
    return [
        RewardResult(1, Rect(0, 0, 100, 30), "1번 칸 미리보기", "1번 칸 미리보기", None, "1번 칸 미리보기", 0.0, "preview", 12, 45, [], ""),
        RewardResult(2, Rect(100, 0, 100, 30), "2번 칸 미리보기", "2번 칸 미리보기", None, "2번 칸 미리보기", 0.0, "preview", 8, 15, [], ""),
        RewardResult(3, Rect(200, 0, 100, 30), "3번 칸 미리보기", "3번 칸 미리보기", None, "3번 칸 미리보기", 0.0, "preview", None, 0, ["LOW_CONFIDENCE"], "미리보기 신뢰도 낮음"),
        RewardResult(4, Rect(300, 0, 100, 30), "4번 칸 미리보기", "4번 칸 미리보기", None, "4번 칸 미리보기", 0.0, "preview", 95, 100, ["BEST_PLAT", "BEST_DUCAT"], ""),
    ]
