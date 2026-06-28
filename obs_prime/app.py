from __future__ import annotations

import argparse
import os
import json
import sys
import compileall

from .app_controller import PipelineController
from .config import AppConfig
from .functional import run_detector_functional, run_ocr_functional
from .gui.main_window import MainWindow
from .paths import ensure_project_dirs, PROJECT_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="OBS prime")
    parser.add_argument("--mvp-functional", action="store_true", help="Run offline MVP functional validation")
    parser.add_argument("--unit-smoke", action="store_true", help="Run dependency-free unit smoke checks")
    parser.add_argument("--detector-functional", action="store_true", help="Run detector functional validation on samples")
    parser.add_argument("--ocr-functional", action="store_true", help="Run OCR functional validation on samples")
    parser.add_argument("--run-sample-set", action="store_true", help="Run the shared pipeline on every sample image")
    parser.add_argument("--obs-saved-functional", default=None, help="Run functional validation on a saved OBS source screenshot")
    parser.add_argument("--config-check", action="store_true", help="Validate loaded config/profile structure")
    parser.add_argument("--list-presets", action="store_true", help="List detector, ROI, and OCR presets")
    parser.add_argument("--compilecheck", action="store_true", help="Run python compileall on source + tests")
    parser.add_argument("--gui-smoke", action="store_true", help="Create and destroy Tkinter GUI once")
    parser.add_argument("--ocr-check", action="store_true", help="Check OCR runtime readiness")
    parser.add_argument("--market-api-probe", action="store_true", help="Probe Warframe Market API with Voruna Prime Chassis")
    parser.add_argument("--market-price-update", action="store_true", help="Explicitly refresh Warframe Market price cache")
    parser.add_argument("--ducat-db-update", action="store_true", help="Refresh item_wiki ducat database")
    parser.add_argument("--market-wiki-update", action="store_true", help="Refresh full Warframe Market item wiki database")
    parser.add_argument("--samples", default=None, help="Sample directory for functional checks")
    parser.add_argument("--gui", action="store_true", help="Launch Tkinter GUI")
    parser.add_argument("--save-default-config", action="store_true", help="Write default config")
    args = parser.parse_args(argv)

    ensure_project_dirs()
    if args.save_default_config:
        cfg = AppConfig.load()
        cfg.save()
        print(f"saved config: {cfg.path}")
        return 0
    if args.mvp_functional:
        return run_mvp_functional()
    if args.unit_smoke:
        return run_unit_smoke()
    if args.detector_functional:
        return run_detector_functional(args.samples)
    if args.ocr_functional:
        return run_ocr_functional(args.samples)
    if args.run_sample_set:
        return run_sample_set(args.samples)
    if args.obs_saved_functional:
        return run_obs_saved_functional(args.obs_saved_functional)
    if args.compilecheck:
        return run_compilecheck()
    if args.gui_smoke:
        return run_gui_smoke()
    if args.ocr_check:
        return run_ocr_check()
    if args.market_api_probe:
        return run_market_api_probe()
    if args.market_price_update:
        return run_market_price_update()
    if args.ducat_db_update:
        return run_ducat_db_update()
    if args.market_wiki_update:
        return run_market_wiki_update()
    if args.config_check:
        return run_config_check()
    if args.list_presets:
        return run_list_presets()
    MainWindow().run()
    return 0


def run_compilecheck() -> int:
    obs_ok = compileall.compile_dir(str(PROJECT_ROOT / "obs_prime"), quiet=1)
    if not obs_ok:
        print("compilecheck: OBS sources failed (rerunning with diagnostics):", file=sys.stderr)
        compileall.compile_dir(str(PROJECT_ROOT / "obs_prime"), quiet=0)
        return 1
    tests_dir = PROJECT_ROOT / "tests"
    if tests_dir.exists():
        tests_ok = compileall.compile_dir(str(tests_dir), quiet=1)
        if not tests_ok:
            print("compilecheck: tests failed (rerunning with diagnostics):", file=sys.stderr)
            compileall.compile_dir(str(tests_dir), quiet=0)
            return 1
    print("compilecheck passed")
    return 0


def run_mvp_functional() -> int:
    controller = PipelineController()
    controller.config.set_value("data", "market_live_enabled", False)
    controller.warm_ocr_provider()
    result = controller.run_pipeline(trigger="sample")
    high_conf = [r for r in result.rewards if r.match_score >= 0.92]
    errors: list[str] = []
    if len(result.rewards) != 4:
        errors.append("expected 4 reward slots")
    if len(high_conf) < 3:
        errors.append("expected at least 3 high-confidence matches")
    if result.recommendation.best_plat_slot is None:
        errors.append("best_plat not selected")
    if result.recommendation.best_ducat_slot is None:
        errors.append("best_ducat not selected")
    if result.total_ms > 5000:
        errors.append(f"pipeline runtime exceeded 5000ms: {result.total_ms}ms")
    if "match_candidates" not in result.debug_paths:
        errors.append("match candidate debug artifact missing")
    if "events" not in result.debug_paths:
        errors.append("event log debug artifact missing")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if errors:
        print("MVP functional failed:", "; ".join(errors), file=sys.stderr)
        return 1
    print("MVP functional passed")
    return 0


def run_obs_saved_functional(path: str) -> int:
    cfg = AppConfig.load()
    cfg.set_value("data", "market_live_enabled", False)
    result = PipelineController(cfg).run_pipeline(trigger="hotkey", sample_path=path)
    matched = [reward for reward in result.rewards if reward.matched_item_id]
    errors: list[str] = []
    if len(result.rewards) != 4:
        errors.append("expected 4 reward slots")
    if len(matched) < int(cfg.section("auto").get("min_ocr_slots_for_output", 2)):
        errors.append(f"expected at least {cfg.section('auto').get('min_ocr_slots_for_output', 2)} matched slots")
    if result.total_ms > 5000:
        errors.append(f"pipeline runtime exceeded 5000ms: {result.total_ms}ms")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if errors:
        print("OBS saved functional failed:", "; ".join(errors), file=sys.stderr)
        return 1
    print("OBS saved functional passed")
    return 0


def run_gui_smoke() -> int:
    if os.environ.get("OBS_PRIME_HEADLESS", "").lower() in {"1", "true", "yes"}:
        print("gui smoke skipped (headless)")
        return 0
    try:
        from .gui.main_window import MainWindow

        app = MainWindow()
        app.root.update_idletasks()
        app.root.destroy()
        print("gui create ok")
        return 0
    except Exception as exc:
        print(f"GUI smoke failed: {exc}", file=sys.stderr)
        return 1


def run_ocr_check() -> int:
    result = PipelineController().run_stage("ocr_check")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ready" else 1


def run_market_price_update() -> int:
    result = PipelineController().run_stage("market_price_update")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


def run_market_api_probe() -> int:
    result = PipelineController().run_stage("market_api_probe")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


def run_ducat_db_update() -> int:
    result = PipelineController().run_stage("ducat_db_update")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


def run_market_wiki_update() -> int:
    from .data.market_wiki import refresh_market_wiki

    cfg = AppConfig.load()
    data_cfg = cfg.section("data")
    result = refresh_market_wiki(
        output_dir=str(data_cfg.get("market_wiki_dir", "data/market_wiki") or "data/market_wiki"),
        market_items_url=str(data_cfg.get("warframe_market_items_url", "")),
        timeout=45.0,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


def run_unit_smoke() -> int:
    import base64
    import hashlib
    from copy import deepcopy
    from pathlib import Path

    from .config import DEFAULT_CONFIG, _deep_update
    from .data.fixtures import fixture_path
    from .data.item_wiki import _validate_fetch_url
    from .data.item_wiki_store import _safe_item_wiki_entry_path, load_item_wiki_records
    from .data.item_store import UserStateStore
    from .data.warframe_market import WarframeMarketClient
    from .detect.auto_detector import AutoDetector
    from .detect.templates import load_detector_preset
    from .detect.roi_presets import load_roi_preset
    from .gui.option_utils import preserve_current_option
    from .hotkey.manager import HotkeyManager
    from .hotkey.parser import parse_hotkey
    from .hotkey.windows_backend import hotkey_to_windows_codes
    from .matcher.correction_store import CorrectionStore
    from .matcher.item_matcher import ItemMatcher
    from .models import CaptureFrame, ItemRecord, Rect, RewardResult
    from .ocr.presets import load_ocr_preset
    from .ocr.providers import OcrProvider
    from .ocr.reward_screen import RewardScreenOcr
    from .overlay.providers import WindowOverlayProvider
    from .obs.websocket_client import ObsWebSocketClient, WEBSOCKET_ACCEPT_GUID, _candidate_hosts, _normalize_host
    from .diagnostics.artifacts import ArtifactWriter
    from .diagnostics.logging import EventLog
    from .paths import resolve_project_path
    from .recommend.engine import RecommendationEngine
    from .security.secret_store import unprotect_secret
    from .validation import validate_capture_config, validate_rects_in_bounds, validate_threshold

    failures: list[str] = []
    try:
        resolve_project_path("..")
        failures.append("project path resolver accepted parent escape")
    except ValueError:
        pass
    try:
        ArtifactWriter(enabled=False)._artifact_path("../escape.txt")
        failures.append("artifact writer accepted path escape")
    except ValueError:
        pass
    try:
        AppConfig(path=Path("..") / "outside-config.json").save()
        failures.append("config save accepted path outside project")
    except ValueError:
        pass
    try:
        CorrectionStore(Path("..") / "outside-corrections.json")
        failures.append("correction store accepted path outside project")
    except ValueError:
        pass
    try:
        fixture_path("..\\outside_fixture.json")
        failures.append("fixture path accepted parent escape")
    except ValueError:
        pass
    handshake_key = base64.b64encode(b"obs-prime-test-12").decode("ascii")
    handshake_accept = base64.b64encode(
        hashlib.sha1((handshake_key + WEBSOCKET_ACCEPT_GUID).encode("ascii")).digest()
    ).decode("ascii")
    handshake_header = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {handshake_accept}\r\n"
    )
    try:
        ObsWebSocketClient("127.0.0.1", 4455)._validate_handshake_response(handshake_header, handshake_key)
    except Exception as exc:
        failures.append(f"valid websocket handshake rejected: {exc}")
    if _normalize_host("ws://127.0.0.1:4455") != "127.0.0.1":
        failures.append("OBS WebSocket host normalization rejected ws://host:port input")
    if _candidate_hosts("127.0.0.1")[:2] != ["127.0.0.1", "localhost"]:
        failures.append("OBS WebSocket localhost fallback candidates were not generated")
    try:
        ObsWebSocketClient("127.0.0.1", 4455)._validate_handshake_response(
            handshake_header.replace(handshake_accept, "invalid"),
            handshake_key,
        )
        failures.append("invalid websocket accept header was accepted")
    except RuntimeError:
        pass
    try:
        WarframeMarketClient(base_url="http://example.com/v2").top_orders("paris_prime_string")
        failures.append("Warframe Market client accepted non-HTTPS/untrusted base URL")
    except RuntimeError:
        pass
    try:
        _validate_fetch_url("https://example.com/not-allowed.json")
        failures.append("item wiki fetch URL allowlist accepted untrusted host")
    except RuntimeError:
        pass
    try:
        unprotect_secret("dpapi:" + ("A" * 9000))
        failures.append("oversized DPAPI secret payload was accepted")
    except RuntimeError:
        pass
    try:
        unprotect_secret("dpapi:not@@base64")
        failures.append("malformed DPAPI secret payload was accepted")
    except RuntimeError:
        pass
    merged_config = deepcopy(DEFAULT_CONFIG)
    _deep_update(merged_config, {"obs_websocket": [], "hotkey": "bad", "overlay": {"enabled": False}})
    if not isinstance(merged_config.get("obs_websocket"), dict) or not isinstance(merged_config.get("hotkey"), dict):
        failures.append("config merge allowed section type replacement")
    if merged_config.get("overlay", {}).get("enabled") is not False:
        failures.append("config merge rejected valid scalar override")
    try:
        combo = parse_hotkey("Ctrl + Alt + R")
        if combo.normalized != "ctrl+alt+r":
            failures.append(f"unexpected hotkey normalization: {combo.normalized}")
    except Exception as exc:
        failures.append(f"valid hotkey rejected: {exc}")
    try:
        combo = parse_hotkey("cmd+alt+r")
        if combo.normalized != "alt+win+r":
            failures.append(f"cmd modifier was not normalized to win: {combo.normalized}")
    except Exception as exc:
        failures.append(f"cmd modifier hotkey rejected: {exc}")
    try:
        parse_hotkey("r")
        failures.append("single-key hotkey accepted")
    except ValueError:
        pass
    manager = HotkeyManager(lambda trigger: None)
    manager.configure("ctrl+alt+r", True, 1500)
    try:
        manager.configure("r", True, 1500)
        failures.append("hotkey manager accepted unsafe combo")
    except ValueError:
        if manager.combo.normalized != "ctrl+alt+r":
            failures.append("hotkey manager did not preserve old combo after failed registration")
    modifiers, vk = hotkey_to_windows_codes(parse_hotkey("ctrl+alt+r"))
    if modifiers == 0 or vk == 0:
        failures.append("Windows hotkey codes were not generated")
    matcher = ItemMatcher(
        [
            ItemRecord(
                id="lex_prime_receiver",
                ko_name="렉스 프라임 리시버",
                en_name="Lex Prime Receiver",
                aliases=["Lex Receiver"],
                item_type="part",
                rarity="common",
                ducats=15,
                market_slug="lex_prime_receiver",
            )
        ]
    )
    if matcher.match("Lex Receiver").method != "alias":
        failures.append("alias match did not use alias method")
    if matcher.match("Lex Prime Reciever").score < 0.80:
        failures.append("fuzzy match score below usable threshold")
    noisy_matcher = ItemMatcher(
        [
            ItemRecord(
                id="forma_blueprint",
                ko_name="포르마 설계도",
                en_name="Forma Blueprint",
                aliases=[],
                item_type="blueprint",
                rarity="common",
                ducats=0,
                market_slug="forma_blueprint",
                tradable=False,
            )
        ]
    )
    noisy_match = noisy_matcher.match("| | J\n2 ×포르마 설계도")
    if noisy_match.item is None or noisy_match.item.id != "forma_blueprint":
        failures.append("noisy Korean reward OCR did not match item name")
    wiki_records = load_item_wiki_records(PROJECT_ROOT / "data" / "item_wiki")
    if len(wiki_records) < 500:
        failures.append(f"item_wiki record load too small: {len(wiki_records)}")
    if _safe_item_wiki_entry_path(PROJECT_ROOT / "data" / "item_wiki", "../fixtures/warframe_prime_fixture.json") is not None:
        failures.append("item_wiki index filename traversal was accepted")
    corrected = ItemMatcher(
        [
            ItemRecord(
                id="glaive_prime_blade",
                ko_name="글레이브 프라임 블레이드",
                en_name="Glaive Prime Blade",
                aliases=[],
                item_type="part",
                rarity="rare",
                ducats=100,
                market_slug="glaive_prime_blade",
            )
        ],
        corrections={"글레이브 프라임 블레": "glaive_prime_blade"},
    )
    if corrected.match("글레이브 프라임 블레").method != "manual_correction":
        failures.append("manual correction was not preferred by matcher")
    low_threshold_rewards = [
        RewardResult(1, Rect(0, 0, 10, 10), "a", "a", "a", "A", 0.85, "fuzzy", 10, 10),
        RewardResult(2, Rect(0, 0, 10, 10), "b", "b", "b", "B", 1.0, "exact", 8, 20),
    ]
    RecommendationEngine(usable_threshold=0.90).score(low_threshold_rewards)
    if "LOW_CONFIDENCE" not in low_threshold_rewards[0].recommendation_flags:
        failures.append("recommendation engine did not honor injected usable threshold")
    clean_overlay = WindowOverlayProvider({"layout": "horizontal"}).render(
        [
            RewardResult(
                1,
                Rect(0, 0, 10, 10),
                "raw",
                "raw",
                None,
                "",
                0.2,
                "none",
                None,
                None,
                ["UNMATCHED", "LOW_CONFIDENCE", "STALE_PRICE"],
                "test",
            )
        ]
    )
    if any(marker in clean_overlay for marker in ("MATCH?", "OCR?", "PRICE?")):
        failures.append("window overlay leaked diagnostic warning markers into broadcast payload")
    event_log = EventLog()
    event_log.add("OBS", "INFO", "saved dpapi:AAAA1111+/=")
    if "dpapi:AAAA" in event_log.tail(1)[0] or "dpapi:REDACTED" not in event_log.tail(1)[0]:
        failures.append("event log did not redact DPAPI payload")
    for index in range(1005):
        event_log.add("LOG", "INFO", f"entry {index}")
    if len(event_log.entries) > 1000:
        failures.append("event log did not cap retained entries")
    options, warning = preserve_current_option("missing-preset", ["default-preset"], "detector preset")
    if "missing-preset" not in options or "missing" not in warning:
        failures.append("missing dropdown value was not preserved with warning")
    if validate_threshold("1.4").ok:
        failures.append("out-of-range threshold accepted")
    if not validate_capture_config({"mode": "sample_image", "sample_image_path": ""}).ok:
        failures.append("blank sample path should allow virtual MVP sample")
    if validate_capture_config({"mode": "sample_image", "sample_image_path": "missing.png"}).ok:
        failures.append("missing explicit sample path was accepted")
    if load_detector_preset("default-virtual-1080p").preset_id != "default-virtual-1080p":
        failures.append("detector preset loader did not load default preset")
    if load_detector_preset("../default-virtual-1080p").reason != "preset missing":
        failures.append("detector preset loader accepted path-like preset id")
    if len(load_roi_preset("default-virtual-1080p").slot_name_rects) != 4:
        failures.append("ROI preset loader did not load four slot rects")
    if load_roi_preset("../default-virtual-1080p").notes != "preset missing":
        failures.append("ROI preset loader accepted path-like preset id")
    if load_ocr_preset("default-korean-ui").language != "kor+eng":
        failures.append("OCR preset loader did not load language")
    if not load_ocr_preset("../default-korean-ui").display_name.startswith("Missing OCR preset"):
        failures.append("OCR preset loader accepted path-like preset id")
    rect_validation = validate_rects_in_bounds([Rect(0, 0, 50, 50), Rect(-1, 0, 10, 10)], 100, 100)
    if rect_validation.ok or "2번 칸" not in ";".join(rect_validation.errors):
        failures.append("invalid ROI rect was not reported with slot index")
    user_state = UserStateStore.from_fixture(fixture_path("warframe_prime_fixture.json"))
    if not user_state.get("glaive_prime_blade") or not user_state.get("glaive_prime_blade").pinned:
        failures.append("fixture user pinned state did not load")
    db_stage = PipelineController().run_stage("db_test")
    if db_stage.get("item_count", 0) < 4 or db_stage.get("user_state_count", 0) < 1:
        failures.append("db_test stage did not report fixture item/user-state counts")
    if db_stage.get("alias_count", 0) < 1:
        failures.append("db_test stage did not report loaded aliases")
    if "oldest_price_age_hours" not in db_stage:
        failures.append("db_test stage did not report price age")
    overlay_stage = PipelineController().run_stage("overlay_test")
    if "1번 칸" not in overlay_stage.get("payload", ""):
        failures.append("overlay_test stage did not render four-slot preview payload")
    ocr_check = PipelineController().run_stage("ocr_check")
    if ocr_check.get("provider") not in {"tesseract", "paddleocr_v5", "windows_ocr"} or ocr_check.get("status") not in {"ready", "unavailable"}:
        failures.append("ocr_check did not report a supported OCR provider status")
    class BrokenOcrProvider(OcrProvider):
        def read_slot(self, frame, slot_index, rect):
            raise RuntimeError("intentional OCR failure")

        def read_slots(self, frame, slot_rects):
            return []

    partial_ocr = RewardScreenOcr(BrokenOcrProvider(), 50).read_rewards(
        CaptureFrame("unit", None, 10, 10, None),
        [Rect(0, 0, 10, 10)] * 4,
    )
    if len(partial_ocr) != 4 or partial_ocr[0].raw_text or partial_ocr[0].confidence != 0.0 or not partial_ocr[0].error:
        failures.append("OCR slot exception was not converted to partial OCR result")
    sample_set = PipelineController().run_sample_set("samples\\reward_screens")
    if sample_set.get("status") != "BLOCKED":
        failures.append("empty sample set did not report BLOCKED")
    auto_detect = PipelineController().run_auto_detect(force_sample=True)
    if auto_detect.get("stage") != "auto_detect" or "ocr" in auto_detect or "rewards" in auto_detect:
        failures.append("auto detect stage did not stay detector-only")
    if "rects" not in auto_detect or "slot_rects" not in auto_detect:
        failures.append("auto detect stage did not expose detector rects")
    live_detection = AutoDetector().detect(CaptureFrame("screen", None, 1920, 1080, None), threshold=0.10)
    if live_detection.detected or live_detection.confidence >= 0.50:
        failures.append("live screen geometry-only detection was allowed without template evidence")
    if PipelineController().run_stage("config_check").get("status") != "PASS":
        failures.append("config_check stage did not pass")
    if failures:
        print("Unit smoke failed:", "; ".join(failures), file=sys.stderr)
        return 1
    print("Unit smoke passed")
    return 0


def run_sample_set(samples: str) -> int:
    result = PipelineController().run_sample_set(samples)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 2


def run_config_check() -> int:
    result = PipelineController().run_stage("config_check")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


def run_list_presets() -> int:
    from .paths import PROJECT_ROOT

    payload = {}
    for kind in ["detector", "roi", "ocr"]:
        root = PROJECT_ROOT / "presets" / kind
        payload[kind] = sorted(path.stem for path in root.glob("*.json")) if root.exists() else []
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
