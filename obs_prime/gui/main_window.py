from __future__ import annotations

from dataclasses import asdict
import difflib
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox
from tkinter import simpledialog
from tkinter import ttk

from ..app_controller import PipelineController
from ..capture.samples import validate_image_dimensions
from ..config import AppConfig
from ..data.fixtures import fixture_path
from ..data.item_wiki import item_wiki_version
from ..data.item_wiki_store import MAX_ITEM_WIKI_ENTRY_BYTES, MAX_ITEM_WIKI_INDEX_BYTES, _safe_item_wiki_entry_path
from ..data.reward_ledger import RewardLedger
from ..data.warframe_market import WarframeMarketClient, fetch_market_prices_for_items
from ..diagnostics.artifacts import ArtifactWriter
from ..hotkey.manager import HotkeyManager
from ..hotkey.parser import parse_hotkey
from ..matcher.correction_store import CorrectionStore
from ..matcher.normalize import normalize_text
from ..models import CaptureFrame, ItemRecord, Rect
from ..obs.websocket_client import (
    capture_obs_source_screenshot,
    check_obs_websocket,
    fetch_obs_source_rects,
    update_obs_text_sources,
)
from ..obs.text_format import format_item_wiki_reward_text, format_obs_reward_text
from ..ocr.providers import build_ocr_provider
from ..ocr.name_band import apply_name_band_to_dicts
from ..ocr.reward_screen import RewardScreenOcr
from ..overlay.providers import build_overlay_provider
from ..overlay.window import ObsCaptureOverlayWindow, OverlayRect, OverlayWindow
from ..paths import PROJECT_ROOT, resolve_project_path
from ..security.secret_store import protect_secret, unprotect_secret
from ..validation import (
    ValidationResult,
    validate_capture_config,
    validate_existing_dir,
    validate_existing_file,
    validate_int_range,
    validate_positive_int,
    validate_rects_in_bounds,
    validate_threshold,
)
from ..detect.roi_presets import load_roi_preset
from .option_utils import preserve_current_option


CAPTURE_MODE_LABELS = {
    "sample_image": "샘플 이미지",
    "screen": "화면 캡처",
}
OCR_PROVIDER_LABELS = {
    "tesseract": "Tesseract",
    "paddleocr_v5": "PaddleOCR v5 Korean",
    "windows_ocr": "Windows OCR",
}
OCR_PROVIDER_OPTIONS = list(OCR_PROVIDER_LABELS.keys())
OVERLAY_MODE_LABELS = {
    "console": "콘솔(디버그)",
    "window": "창 오버레이",
    "disabled": "사용 안 함",
}
OVERLAY_POSITION_LABELS = {
    "top-right": "오른쪽 위",
    "top-left": "왼쪽 위",
    "bottom-right": "오른쪽 아래",
    "bottom-left": "왼쪽 아래",
    "custom": "직접 입력 / 직접 조절",
}
HOTKEY_STATUS_LABELS = {
    "disabled": "꺼짐",
    "enabled": "켜짐",
    "registered": "등록됨",
    "failed": "실패",
    "busy": "실행중",
    "debounced": "무시됨",
}
STAGE_LABELS = {
    "capture_test": "입력 확인",
    "detector_test": "감지 테스트",
    "ocr_check": "OCR 확인",
    "ocr_test": "OCR 테스트",
    "db_test": "DB 테스트",
    "market_api_probe": "마켓 API 검증 테스트",
    "ducat_db_update": "두캇 DB 갱신",
    "market_wiki_update": "마켓 Wiki 갱신",
    "overlay_test": "오버레이 테스트",
    "roi_test": "ROI 테스트",
    "gui_button": "단일 실행",
    "sample": "샘플 세트",
    "auto": "자동 실행",
    "hotkey": "단축키 실행",
}
FLAG_LABELS = {
    "PINNED": "고정",
    "NEEDED": "필요",
    "BEST_PLAT": "최고 플래티넘",
    "BEST_DUCAT": "최고 두캇",
    "BEST_RATIO": "최고 효율",
    "LOW_CONFIDENCE": "낮은 신뢰도",
    "UNMATCHED": "미매칭",
    "STALE_PRICE": "가격 오래됨",
}
RESULT_OUTPUT_AUTO_SAFEGUARD_MS = 10000


def home_toggle_text(label: str, enabled: bool) -> str:
    return f"{label} : {'on' if enabled else 'off'}"


def is_result_memo_value_valid(value: str) -> bool:
    return isinstance(value, str)


class MainWindow:
    def __init__(self, controller: PipelineController | None = None) -> None:
        self.controller = controller or PipelineController()
        self.config = self.controller.config
        self.reward_ledger = RewardLedger(
            resolve_project_path(str(self.config.section("data").get("reward_history_path", "data/reward_results.json")))
        )
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.auto_running = False
        self.auto_detect_busy = False
        self.last_auto_trigger_ms = 0
        self.last_auto_idle_log_ms = 0
        self.result_output_block_until_ms = 0
        self.last_auto_output_block_log_ms = 0
        self.result_overlay_output_active = False
        self.result_obs_text_output_active = False
        self.last_detector_payload: dict[str, object] | None = None
        self.last_db_summary = "?"
        self.last_ocr_summary = "?"
        self.last_result = None
        self.last_obs_capture_path: str | None = None
        self.last_obs_capture_source_name = ""
        self.last_obs_ocr_payload: dict[str, object] | None = None
        self.obs_connected = False
        self.last_obs_info: dict[str, object] = {}
        self.obs_auto_setup_pending = False
        self.ui_busy = False
        self.gui_dirty = False
        self._loading_config = False
        self.overlay_window: OverlayWindow | None = None
        self.obs_capture_overlay_window: ObsCaptureOverlayWindow | None = None
        self.overlay_adjust_window: tk.Toplevel | None = None
        self.overlay_adjust_button_text: tk.StringVar | None = None
        self.overlay_clear_after_id: str | None = None
        self.obs_text_clear_after_id: str | None = None
        self.home_auto_toggle_button: tk.Button | None = None
        self.home_overlay_toggle_button: tk.Button | None = None
        self.home_one_pc_toggle_button: tk.Button | None = None
        self.one_pc_mode_active = False
        self.one_pc_mode_snapshot: dict[str, object] | None = None
        self.last_hotkey_status = ""
        self._loaded_obs_password = ""
        self._obs_password_decrypt_error = ""
        self._mapped_display_vars: list[tk.StringVar] = []
        self.market_autocomplete_after_id: str | None = None
        self.market_autocomplete_cache_key: tuple[object, ...] | None = None
        self.market_autocomplete_candidates: list[dict[str, object]] = []
        self.market_autocomplete_visible: list[dict[str, object]] = []
        self.market_autocomplete_selecting = False
        self.dark_mode = False
        self.current_page_id = ""
        self.root = tk.Tk()
        self.root.title("OBS prime")
        self.root.geometry("1280x800")
        self.root.minsize(1280, 800)
        self.root.maxsize(1280, 800)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.style = ttk.Style(self.root)
        self.hotkey_manager = HotkeyManager(self._on_hotkey_trigger)
        self._build()
        self._load_config_to_gui()
        self._apply_theme()
        self._bind_dirty_traces()
        self._sync_hotkey_from_gui()
        self._refresh_profile_title()
        self.root.after(100, self._poll_events)
        self.root.after(120, self._restore_main_window)
        self.root.after(800, self._restore_main_window)
        self.root.after(350, self._startup_obs_bootstrap)
        self.root.after(900, self._startup_ocr_prewarm)

    def run(self) -> None:
        self.root.mainloop()

    def _manual_artifact_writer(self) -> ArtifactWriter:
        diagnostics_cfg = self.config.section("diagnostics")
        return ArtifactWriter(str(diagnostics_cfg.get("artifact_dir", "debug") or "debug"), enabled=True)

    def _restore_main_window(self) -> None:
        try:
            if self.root.state() == "iconic":
                self.root.state("normal")
            self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            return

    def _build(self) -> None:
        self.status_var = tk.StringVar(value="대기 중")
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=6, pady=4)
        top.columnconfigure(1, weight=1)
        self.profile_label = ttk.Label(top, text="OBS prime")
        self.profile_label.grid(row=0, column=0, sticky="w")
        self.indicator_var = tk.StringVar(value="[현재 OCR 엔진 : ?] [자동 감지 : off] [현재 핫키 : ?] [ducats DB 갱신 : -]")
        self.home_indicator_var = tk.StringVar(value="[현재 OCR 엔진 : ?] [자동 감지 : off] [현재 핫키 : ?]\n[ducats DB 갱신 : -]")
        ttk.Label(top, textvariable=self.indicator_var).grid(row=0, column=1, sticky="w", padx=(16, 8))
        actions = ttk.Frame(top)
        actions.grid(row=0, column=2, sticky="e")
        self.dark_mode_button_text = tk.StringVar(value="다크모드")
        ttk.Button(actions, textvariable=self.dark_mode_button_text, command=self._toggle_dark_mode).pack(side="left", padx=3)
        ttk.Button(actions, text="되돌리기", command=self._revert_config).pack(side="left", padx=3)
        ttk.Button(actions, text="적용", command=self._apply_config).pack(side="left", padx=3)
        ttk.Button(actions, text="저장", command=self._save_config).pack(side="left", padx=3)
        ttk.Label(top, textvariable=self.status_var, anchor="center").grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=4)
        nav = ttk.Frame(body, width=190)
        nav.pack_propagate(False)
        body.add(nav, weight=0)

        right = ttk.Frame(body, padding=(6, 0, 0, 0))
        body.add(right, weight=1)

        self.page_title_var = tk.StringVar(value="홈")
        ttk.Label(right, textvariable=self.page_title_var, font=("", 10, "bold")).pack(fill="x", padx=2, pady=(0, 6))
        self.page_stack = ttk.Frame(right)
        self.page_stack.pack(fill="both", expand=True)
        self.page_stack.rowconfigure(0, weight=1)
        self.page_stack.columnconfigure(0, weight=1)
        self.pages: dict[str, ttk.Frame] = {}
        self.page_titles: dict[str, str] = {}
        self.nav_buttons: dict[str, ttk.Button] = {}

        self._build_navigation(nav)
        self._build_controls(self.page_stack)
        self._build_observability(self.page_stack)
        self._select_page("home")

    def _build_navigation(self, parent) -> None:
        ttk.Label(parent, text="OBS prime", font=("", 10, "bold"), anchor="center").pack(fill="x", padx=8, pady=(8, 14))
        ttk.Label(parent, text="빠른 실행", font=("", 9, "bold")).pack(fill="x", padx=8, pady=(0, 3))
        ttk.Button(parent, text="1회 작동", command=lambda: self._run_pipeline_async("gui_button")).pack(fill="x", padx=8, pady=2)
        ttk.Button(parent, text="자동감지 on/off", command=self._toggle_auto_detect).pack(fill="x", padx=8, pady=2)
        self._nav_gap(parent, 14)
        ttk.Label(parent, text="기본 설정", font=("", 9, "bold")).pack(fill="x", padx=8, pady=(0, 3))
        for label, page_id in [
            ("OBS 연결", "obs"),
            ("OCR 엔진", "ocr"),
            ("OCR / 매칭", "details"),
            ("입력 / 좌표", "roi"),
        ]:
            self._nav_button(parent, label, page_id)
        self._nav_separator(parent)
        for label, page_id in [
            ("자동 감지", "auto"),
            ("단축키", "hotkey"),
            ("오버레이", "overlay"),
            ("오버레이 미리보기", "overlay_preview"),
        ]:
            self._nav_button(parent, label, page_id)
        self._nav_separator(parent)
        for label, page_id in [
            ("이벤트 로그", "event_log"),
            ("매칭", "matching"),
        ]:
            self._nav_button(parent, label, page_id)
        self._nav_button(parent, "데이터 베이스", "database")
        self._nav_button(parent, "보상 결과", "results")
        self._nav_gap(parent, 8)
        self._home_nav_button(parent)

    def _nav_gap(self, parent, height: int = 8) -> None:
        ttk.Frame(parent, height=height).pack(fill="x")

    def _nav_separator(self, parent) -> None:
        ttk.Separator(parent).pack(fill="x", padx=8, pady=8)

    def _nav_button(self, parent, label: str, page_id: str) -> None:
        button = ttk.Button(parent, text=label, command=lambda key=page_id: self._select_page(key))
        button.pack(fill="x", padx=8, pady=2)
        self.nav_buttons[page_id] = button

    def _home_nav_button(self, parent) -> None:
        button = tk.Button(parent, text="홈", height=2, font=("", 9, "bold"), command=lambda: self._select_page("home"))
        button.pack(fill="x", padx=8, pady=(4, 2))
        self.nav_buttons["home"] = button

    def _select_page(self, page_id: str) -> None:
        if page_id == "home" and self.current_page_id == "home":
            page_id = "market_search"
        page = self.pages.get(page_id)
        if page is None:
            return
        if page_id == "database":
            self._refresh_database_page_status()
        page.tkraise()
        self.current_page_id = page_id
        self.page_title_var.set(self.page_titles.get(page_id, page_id))

    def _scroll_page(self, parent, page_id: str, title: str) -> ttk.Frame:
        outer = ttk.Frame(parent)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=max(event.width, 1)))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.pages[page_id] = outer
        self.page_titles[page_id] = title
        return content

    def _plain_page(self, parent, page_id: str, title: str) -> ttk.Frame:
        outer = ttk.Frame(parent)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        self.pages[page_id] = outer
        self.page_titles[page_id] = title
        return outer

    def _group(self, parent, title: str):
        frame = ttk.LabelFrame(parent, text=title)
        frame.pack(fill="x", padx=4, pady=4)
        return frame

    def _build_controls(self, parent) -> None:
        self.capture_mode = tk.StringVar()
        self.capture_monitor_index = tk.StringVar()
        self.sample_path = tk.StringVar()

        self.auto_enabled = tk.BooleanVar()
        self.auto_interval = tk.StringVar()
        self.auto_cooldown = tk.StringVar()
        self.auto_threshold = tk.StringVar()
        self.auto_preset = tk.StringVar()
        self.auto_min_ocr_slots = tk.StringVar()
        page = self._scroll_page(parent, "auto", "자동 감지")
        g = self._group(page, "자동 감지")
        ttk.Checkbutton(g, text="자동 감지 사용", variable=self.auto_enabled).pack(anchor="w")
        self._entry_row(g, "간격 ms", self.auto_interval)
        self._entry_row(g, "쿨다운 ms", self.auto_cooldown)
        self._entry_row(g, "감지 임계값", self.auto_threshold)
        self._entry_row(g, "최소 OCR 칸", self.auto_min_ocr_slots)
        detector_options, detector_warning = preserve_current_option(
            str(self.config.section("auto").get("detector_preset", "")),
            ["default-virtual-1080p"],
            "감지 프리셋",
        )
        self.auto_preset_combo = self._combo_row(g, "프리셋", self.auto_preset, detector_options)
        if detector_warning:
            self.controller.log.add("DETECT", "WARNING", detector_warning)
        self._button_row(
            g,
            [
                ("새로고침", self._refresh_auto_preset_options),
                ("프리셋 편집", self._edit_detector_preset),
                ("감지 테스트", lambda: self._run_stage_async("detector_test")),
                ("자동 시작", self._start_auto),
                ("자동 중지", self._stop_auto),
            ],
        )

        self.roi_preset = tk.StringVar()
        self.roi_scale = tk.StringVar()
        self.roi_slot_rects: list[dict[str, tk.StringVar]] = []
        self.slot_labels: list[tk.StringVar] = []
        self.obs_input_sources: list[tk.StringVar] = []
        self.obs_output_sources: list[tk.StringVar] = []
        self.obs_output_source_enabled: list[tk.BooleanVar] = []
        page = self._scroll_page(parent, "roi", "입력 / 좌표")
        coord_group = self._group(page, "좌표")
        self._entry_row(coord_group, "UI 배율", self.roi_scale)
        for slot_index in range(1, 5):
            row = {
                "label": tk.StringVar(),
                "x": tk.StringVar(),
                "y": tk.StringVar(),
                "w": tk.StringVar(),
                "h": tk.StringVar(),
            }
            self.roi_slot_rects.append(row)
            self.slot_labels.append(row["label"])
            self._roi_slot_row(coord_group, slot_index, row)
        input_group = self._group(page, "입력소스")
        for slot_index in range(1, 5):
            self.obs_input_sources.append(tk.StringVar(value=f"B{slot_index}"))
        self._source_row(input_group, self.obs_input_sources)
        output_group = self._group(page, "출력소스")
        for slot_index in range(1, 5):
            self.obs_output_sources.append(tk.StringVar(value=f"T{slot_index}"))
            self.obs_output_source_enabled.append(tk.BooleanVar(value=True))
        self._source_row(output_group, self.obs_output_sources, self.obs_output_source_enabled)
        self._button_row(
            coord_group,
            [
                ("감지값 가져오기", lambda: self._fetch_obs_source_rects_async("input")),
                ("좌표 테스트", lambda: self._run_stage_async("roi_test")),
            ],
        )

        self.hotkey_enabled = tk.BooleanVar()
        self.hotkey_global = tk.BooleanVar()
        self.hotkey_combo = tk.StringVar()
        self.hotkey_debounce = tk.StringVar()
        page = self._scroll_page(parent, "hotkey", "단축키")
        g = self._group(page, "단축키")
        ttk.Checkbutton(g, text="단축키 사용", variable=self.hotkey_enabled).pack(anchor="w")
        ttk.Checkbutton(g, text="전역 후킹", variable=self.hotkey_global).pack(anchor="w")
        self._entry_row(g, "조합", self.hotkey_combo)
        self._entry_row(g, "중복 방지 ms", self.hotkey_debounce)
        self._button_row(
            g,
            [
                ("입력", self._record_hotkey),
                ("테스트", self._test_hotkey),
                ("비우기", lambda: self.hotkey_combo.set("")),
            ],
        )

        self.ocr_provider = tk.StringVar()
        self.ocr_language = tk.StringVar()
        self.ocr_timeout = tk.StringVar()
        self.ocr_min_confidence = tk.StringVar()
        self.ocr_preprocessing_preset = tk.StringVar()
        page = self._scroll_page(parent, "ocr", "OCR 엔진")
        g = self._group(page, "OCR 엔진")
        ocr_options, ocr_warning = preserve_current_option(
            str(self.config.section("ocr").get("provider", "")),
            OCR_PROVIDER_OPTIONS,
            "OCR 엔진",
        )
        self.ocr_provider_combo = self._mapped_combo_row(g, "엔진", self.ocr_provider, OCR_PROVIDER_LABELS, fallback_values=ocr_options)
        if ocr_warning:
            self.controller.log.add("OCR", "WARNING", ocr_warning)
        self._entry_row(g, "언어", self.ocr_language)
        self._entry_row(g, "제한 시간 ms", self.ocr_timeout)
        self._entry_row(g, "최소 신뢰도", self.ocr_min_confidence)
        ocr_preset_options, ocr_preset_warning = preserve_current_option(
            str(self.config.section("ocr").get("preprocessing_preset", "")),
            self._preset_ids("ocr"),
            "OCR 전처리 프리셋",
        )
        self.ocr_preset_combo = self._combo_row(g, "전처리", self.ocr_preprocessing_preset, ocr_preset_options)
        if ocr_preset_warning:
            self.controller.log.add("OCR", "WARNING", ocr_preset_warning)
        self._button_row(
            g,
            [
                ("새로고침", self._refresh_ocr_preset_options),
                ("프리셋 편집", self._edit_ocr_preset),
                ("OCR 확인", lambda: self._run_stage_async("ocr_check")),
                ("OCR 테스트", lambda: self._run_stage_async("ocr_test")),
            ],
        )

        self.db_fixture = tk.StringVar()
        self.price_db_path = tk.StringVar()
        self.item_wiki_dir = tk.StringVar()
        self.market_wiki_dir = tk.StringVar()
        self.market_live_enabled = tk.BooleanVar()
        self.market_live_timeout = tk.StringVar()
        self.market_cache_same_day_only = tk.BooleanVar()
        self.database_item_status = tk.StringVar(value="두캇 DB: 확인 전")
        self.database_market_status = tk.StringVar(value="마켓 Wiki: 확인 전")
        self.database_price_status = tk.StringVar(value="가격 캐시: 확인 전")
        self.sample_set_dir = tk.StringVar()
        page = self._scroll_page(parent, "database", "데이터베이스")
        status_group = self._group(page, "상태 요약")
        ttk.Label(status_group, textvariable=self.database_item_status, wraplength=860).pack(fill="x", padx=4, pady=2)
        ttk.Label(status_group, textvariable=self.database_market_status, wraplength=860).pack(fill="x", padx=4, pady=2)
        ttk.Label(status_group, textvariable=self.database_price_status, wraplength=860).pack(fill="x", padx=4, pady=2)

        path_group = self._group(page, "경로")
        self._entry_row(path_group, "두캇 DB", self.item_wiki_dir)
        self._entry_row(path_group, "마켓 Wiki", self.market_wiki_dir)
        self._entry_row(path_group, "가격 캐시", self.price_db_path)

        policy_group = self._group(page, "가격 정책")
        ttk.Checkbutton(policy_group, text="실시간 가격 조회 사용", variable=self.market_live_enabled).pack(anchor="w", padx=4, pady=2)
        self._entry_row(policy_group, "조회 제한 ms", self.market_live_timeout)
        ttk.Checkbutton(policy_group, text="캐시는 오늘 값만 사용", variable=self.market_cache_same_day_only).pack(anchor="w", padx=4, pady=2)

        action_group = self._group(page, "검증 / 갱신")
        self._button_row(
            action_group,
            [
                ("상태 새로고침", self._refresh_database_page_status),
                ("마켓 검색기", lambda: self._select_page("market_search")),
                ("마켓 API 검증 테스트", lambda: self._run_stage_async("market_api_probe")),
                ("두캇 DB 갱신", lambda: self._run_stage_async("ducat_db_update")),
                ("마켓 Wiki 갱신", lambda: self._run_stage_async("market_wiki_update")),
                ("DB 테스트", lambda: self._run_stage_async("db_test")),
            ],
        )
        folder_group = self._group(page, "폴더")
        self._button_row(
            folder_group,
            [
                ("두캇 DB 폴더", self._open_item_wiki_folder),
                ("마켓 Wiki 폴더", self._open_market_wiki_folder),
                ("가격 캐시 폴더", self._open_price_cache_folder),
                ("data 폴더", self._open_db_folder),
            ],
        )

        self.match_confident = tk.StringVar()
        self.match_usable = tk.StringVar()
        self.match_uncertain = tk.StringVar()
        self.alias_learning = tk.BooleanVar()
        page = self._scroll_page(parent, "matching", "매칭")
        g = self._group(page, "매칭")
        self._entry_row(g, "확실", self.match_confident)
        self._entry_row(g, "사용 가능", self.match_usable)
        self._entry_row(g, "불확실", self.match_uncertain)
        ttk.Checkbutton(g, text="별칭 학습 사용", variable=self.alias_learning).pack(anchor="w")
        self._button_row(g, [("매칭 수정", self._correct_match)])

        self.overlay_mode = tk.StringVar()
        self.overlay_layout = tk.StringVar()
        self.overlay_enabled = tk.BooleanVar()
        self.overlay_x = tk.StringVar()
        self.overlay_y = tk.StringVar()
        self.overlay_w = tk.StringVar()
        self.overlay_h = tk.StringVar()
        self.overlay_opacity = tk.StringVar()
        self.overlay_click_through = tk.BooleanVar()
        self.overlay_clear_ms = tk.StringVar()
        self.overlay_position = tk.StringVar()
        self.overlay_topmost = tk.BooleanVar()
        self.overlay_adjust_button_text = tk.StringVar(value="오버레이 위치 직접 조절")
        page = self._scroll_page(parent, "overlay", "오버레이")
        g = self._group(page, "오버레이")
        overlay_options, overlay_warning = preserve_current_option(
            str(self.config.section("overlay").get("mode", "")),
            ["console", "window", "disabled"],
            "오버레이 모드",
        )
        self._mapped_combo_row(g, "모드", self.overlay_mode, OVERLAY_MODE_LABELS, fallback_values=overlay_options)
        ttk.Checkbutton(g, text="오버레이 사용", variable=self.overlay_enabled).pack(anchor="w")
        self._mapped_combo_row(g, "위치", self.overlay_position, OVERLAY_POSITION_LABELS)
        ttk.Checkbutton(g, text="항상 위", variable=self.overlay_topmost).pack(anchor="w")
        ttk.Checkbutton(g, text="클릭 통과", variable=self.overlay_click_through).pack(anchor="w")
        self._entry_row(g, "X", self.overlay_x)
        self._entry_row(g, "Y", self.overlay_y)
        self._entry_row(g, "W", self.overlay_w)
        self._entry_row(g, "H", self.overlay_h)
        self._entry_row(g, "불투명도", self.overlay_opacity)
        self._entry_row(g, "자동 지움 ms", self.overlay_clear_ms)
        if overlay_warning:
            self.controller.log.add("OVERLAY", "WARNING", overlay_warning)
        self._button_row(
            g,
            [
                ("오버레이 테스트", lambda: self._run_stage_async("overlay_test")),
                ("오버레이 창 띄우기", self._show_overlay_window_now),
                ("오버레이 OBS용 창 띄우기", self._show_obs_capture_overlay_window_now),
                ("오버레이 지우기", self._clear_overlay),
            ],
            max_columns=4,
        )
        overlay_tools = ttk.Frame(g)
        overlay_tools.pack(fill="x", pady=(6, 2))
        for column in range(4):
            overlay_tools.columnconfigure(column, weight=1)
        ttk.Button(overlay_tools, textvariable=self.overlay_adjust_button_text, command=self._toggle_overlay_adjust_window).grid(
            row=0, column=0, sticky="ew", padx=2, pady=2
        )
        ttk.Button(overlay_tools, text="오버레이 창 위치 초기화", command=self._reset_overlay_window_position).grid(
            row=0, column=1, sticky="ew", padx=2, pady=2
        )
        ttk.Button(overlay_tools, text="오버레이 가로모드", command=lambda: self._set_overlay_layout("horizontal")).grid(
            row=0, column=2, sticky="ew", padx=2, pady=2
        )
        ttk.Button(overlay_tools, text="오버레이 세로모드", command=lambda: self._set_overlay_layout("vertical")).grid(
            row=0, column=3, sticky="ew", padx=2, pady=2
        )

        self.obs_enabled = tk.BooleanVar()
        self.obs_host = tk.StringVar()
        self.obs_port = tk.StringVar()
        self.obs_timeout = tk.StringVar()
        self.obs_password = tk.StringVar()
        self.obs_ocr_source = tk.StringVar(value="이미지")
        self.obs_password_visible = tk.BooleanVar(value=False)
        self.obs_status = tk.StringVar(value="미확인")
        self.obs_password_entry: ttk.Entry | None = None
        self.obs_password_toggle: ttk.Button | None = None
        page = self._scroll_page(parent, "obs", "OBS 연결")
        g = self._group(page, "OBS WebSocket")
        ttk.Checkbutton(g, text="OBS WebSocket 사용", variable=self.obs_enabled).pack(anchor="w")
        self._entry_row(g, "서버 IP", self.obs_host)
        self._entry_row(g, "포트", self.obs_port)
        self._entry_row(g, "제한 시간 ms", self.obs_timeout)
        self._password_row(g, "비밀번호", self.obs_password)
        ttk.Label(g, textvariable=self.obs_status).pack(anchor="w", pady=(4, 2))
        self._button_row(
            g,
            [
                ("연결 테스트", self._test_obs_websocket_async),
                ("비밀번호 비우기", self._clear_obs_password),
            ],
        )

        page = self._scroll_page(parent, "pipeline", "파이프라인")
        g = self._group(page, "파이프라인")
        self._entry_row(g, "샘플 폴더", self.sample_set_dir)
        self._button_row(
            g,
            [
                ("디버그 열기", self._open_debug_folder),
                ("ROI 테스트", lambda: self._run_stage_async("roi_test")),
                ("단일 실행", lambda: self._run_pipeline_async("gui_button")),
            ],
        )

    def _build_observability(self, parent) -> None:
        home_page = self._plain_page(parent, "home", "홈")
        home_page.columnconfigure(0, weight=1)
        home_page.columnconfigure(1, weight=1)
        home_page.rowconfigure(0, weight=1)

        home_left = ttk.Frame(home_page)
        home_left.grid(row=0, column=0, sticky="nsew", padx=(4, 5), pady=0)
        home_left.columnconfigure(0, weight=1)
        for row in (0, 2, 4, 6):
            home_left.rowconfigure(row, weight=1, uniform="home_left", minsize=112)
        for row in (1, 3, 5):
            home_left.rowconfigure(row, weight=0, minsize=10)

        home_right = ttk.Frame(home_page)
        home_right.grid(row=0, column=1, sticky="nsew", padx=(5, 4), pady=0)
        home_right.columnconfigure(0, weight=1)
        for row in (0, 2, 4, 6, 8):
            home_right.rowconfigure(row, weight=1, uniform="home_right", minsize=88)
        for row in (1, 3, 5, 7):
            home_right.rowconfigure(row, weight=0, minsize=10)

        self.home_obs_status = tk.StringVar(value="WebSocket: Off")
        self.home_ocr_status = tk.StringVar(value="엔진: PaddleOCR v5 Korean")
        self.home_input_status = tk.StringVar(value="B1~B4 좌표 미동기화")
        self.home_output_status = tk.StringVar(value="T1~T4 출력 대기")
        self.home_hotkey_status = tk.StringVar(value="현재 기기의 핫키는 확인 중입니다")
        self.home_auto_status = tk.StringVar(value="현재 자동감지가 off 입니다")
        self.home_database_status = tk.StringVar(value="현재 데이터 베이스는 미구축 상태입니다.")
        self.result_cell_editor: ttk.Entry | None = None

        obs_card = self._dashboard_card(home_left, "OBS 연결상태", 0, 0)
        ttk.Label(obs_card, textvariable=self.home_obs_status).pack(anchor="w", padx=6, pady=(4, 2))
        ttk.Button(obs_card, text="OBS 연결", command=self._test_obs_websocket_async).pack(fill="x", padx=6, pady=(0, 4))

        input_card = self._dashboard_card(home_left, "OBS input", 2, 0)
        ttk.Label(input_card, text="B1, B2, B3, B4").pack(anchor="w", padx=6, pady=(6, 2))
        ttk.Label(input_card, textvariable=self.home_input_status).pack(anchor="w", padx=6, pady=2)
        ttk.Button(input_card, text="인풋 좌표 갱신", command=lambda: self._fetch_obs_source_rects_async("input")).pack(fill="x", padx=6, pady=(2, 6))

        output_card = self._dashboard_card(home_left, "OBS output", 4, 0)
        ttk.Label(output_card, text="T1, T2, T3, T4 출력소스").pack(anchor="w", padx=6, pady=(6, 2))
        ttk.Label(output_card, textvariable=self.home_output_status).pack(anchor="w", padx=6, pady=2)
        ttk.Button(output_card, text="출력테스트", command=self._test_obs_outputs_async).pack(fill="x", padx=6, pady=(2, 6))

        database_card = self._dashboard_card(home_left, "데이터 베이스", 6, 0)
        ttk.Label(database_card, textvariable=self.home_database_status, wraplength=420).pack(anchor="w", padx=6, pady=(6, 2))
        ttk.Button(database_card, text="데이터 베이스", command=lambda: self._select_page("database")).pack(fill="x", padx=6, pady=(2, 6))

        home_status = self._dashboard_card(home_right, "상태", 0, 0)
        home_status.columnconfigure(0, weight=1)
        ttk.Label(home_status, textvariable=self.home_indicator_var).grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        ttk.Label(home_status, textvariable=self.status_var).grid(row=1, column=0, sticky="ew", padx=6, pady=(2, 6))

        home_actions = self._dashboard_card(home_right, "실행", 2, 0)
        self._build_home_action_buttons(home_actions)

        auto_card = self._dashboard_card(home_right, "자동 감지", 4, 0)
        ttk.Label(auto_card, textvariable=self.home_auto_status, wraplength=420).pack(anchor="w", padx=6, pady=6)

        hotkey_card = self._dashboard_card(home_right, "Hot-Key", 6, 0)
        ttk.Label(hotkey_card, textvariable=self.home_hotkey_status, wraplength=420).pack(anchor="w", padx=6, pady=6)

        ocr_card = self._dashboard_card(home_right, "현재 OCR", 8, 0)
        ttk.Label(ocr_card, textvariable=self.home_ocr_status).pack(anchor="w", padx=6, pady=(4, 4))
        self._refresh_home_dashboard()

        market_page = self._plain_page(parent, "market_search", "마켓 검색기")
        market_page.columnconfigure(0, weight=1)
        market_page.rowconfigure(2, weight=1)
        self.market_search_query = tk.StringVar()
        self.market_search_rank_mode = tk.StringVar(value="0랭크")
        self.market_search_rank_custom = tk.StringVar(value="0")
        self.market_search_status = tk.StringVar(value="홈 버튼을 한 번 더 눌러 연 히든 마켓 검색기입니다. 검색은 게임중(ingame) 판매 주문만 사용합니다.")
        search_group = ttk.LabelFrame(market_page, text="검색")
        search_group.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        search_group.columnconfigure(1, weight=1)
        ttk.Label(search_group, text="아이템").grid(row=0, column=0, sticky="w", padx=(6, 4), pady=6)
        self.market_search_entry = ttk.Entry(search_group, textvariable=self.market_search_query)
        self.market_search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4), pady=6)
        self.market_search_entry.bind("<KeyRelease>", self._on_market_search_keyrelease)
        self.market_search_entry.bind("<Return>", self._on_market_search_return)
        self.market_search_entry.bind("<Down>", self._on_market_autocomplete_down)
        self.market_search_entry.bind("<Up>", self._on_market_autocomplete_up)
        self.market_search_entry.bind("<Escape>", self._on_market_autocomplete_escape)
        ttk.Button(search_group, text="검색", command=self._run_market_search_async).grid(row=0, column=2, sticky="ew", padx=(0, 6), pady=6)
        self.market_autocomplete_list = tk.Listbox(search_group, height=6, exportselection=False)
        self.market_autocomplete_list.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=(0, 6))
        self.market_autocomplete_list.grid_remove()
        self.market_autocomplete_list.bind("<ButtonRelease-1>", self._on_market_autocomplete_click)
        self.market_autocomplete_list.bind("<Double-Button-1>", self._on_market_autocomplete_click)
        self.market_autocomplete_list.bind("<Return>", self._on_market_autocomplete_list_return)
        self.market_autocomplete_list.bind("<Escape>", self._on_market_autocomplete_escape)
        self.market_autocomplete_list.bind("<Up>", self._on_market_autocomplete_up)
        self.market_autocomplete_list.bind("<Down>", self._on_market_autocomplete_down)
        rank_group = ttk.LabelFrame(market_page, text="랭크 / 기준")
        rank_group.grid(row=1, column=0, sticky="ew", padx=4, pady=4)
        for column in range(4):
            rank_group.columnconfigure(column, weight=1)
        ttk.Label(rank_group, text="가격 기준: 게임중(ingame) 판매 최저가").grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 2))
        ttk.Label(rank_group, text="랭크").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(
            rank_group,
            textvariable=self.market_search_rank_mode,
            values=["0랭크", "최대 랭크", "직접 입력", "전체 랭크"],
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(rank_group, text="직접").grid(row=1, column=2, sticky="e", padx=4, pady=4)
        ttk.Entry(rank_group, textvariable=self.market_search_rank_custom, width=8).grid(row=1, column=3, sticky="ew", padx=(0, 6), pady=4)
        result_group = ttk.LabelFrame(market_page, text="결과")
        result_group.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        result_group.rowconfigure(1, weight=1)
        result_group.columnconfigure(0, weight=1)
        ttk.Label(result_group, textvariable=self.market_search_status, wraplength=760).grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))
        self.market_search_text = tk.Text(result_group, height=12)
        market_scroll = ttk.Scrollbar(result_group, orient="vertical", command=self.market_search_text.yview)
        self.market_search_text.configure(yscrollcommand=market_scroll.set, wrap="word")
        self.market_search_text.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        market_scroll.grid(row=1, column=1, sticky="ns", pady=(0, 6))
        self._set_market_search_text(
            "검색어 예시: 패리스 프라임 설계도, 메사 왈츠, 몰트 어그먼티드\n"
            "검색 순서: 기존 item_wiki -> market_wiki -> Warframe Market API\n"
            "모드/아케인은 0랭크와 최대 랭크 가격이 다를 수 있으므로 랭크 옵션을 확인하세요.",
        )

        self.result_filter_received = tk.BooleanVar(value=False)
        self.result_filter_sell = tk.BooleanVar(value=False)
        self.result_filter_use = tk.BooleanVar(value=False)
        result_page = self._plain_page(parent, "results", "보상 결과")
        result_group = ttk.LabelFrame(result_page, text="보상 결과")
        result_group.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        result_page.rowconfigure(0, weight=1)
        result_page.columnconfigure(0, weight=1)
        filter_row = ttk.Frame(result_group)
        filter_row.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 2))
        ttk.Checkbutton(filter_row, text="수령 입력만", variable=self.result_filter_received, command=self._refresh_result_table).pack(
            side="left", padx=(0, 10)
        )
        ttk.Checkbutton(filter_row, text="판매 입력만", variable=self.result_filter_sell, command=self._refresh_result_table).pack(
            side="left", padx=(0, 10)
        )
        ttk.Checkbutton(filter_row, text="사용 입력만", variable=self.result_filter_use, command=self._refresh_result_table).pack(
            side="left", padx=(0, 10)
        )
        ttk.Button(filter_row, text="전체 보기", command=self._clear_result_filters).pack(side="left", padx=(4, 0))
        columns = ("number", "received", "date", "item", "ducats", "plat", "sell", "use")
        headings = {
            "number": "#",
            "received": "수령",
            "date": "날짜(mm_dd_yy)",
            "item": "아이템 이름",
            "ducats": "두캇 가격",
            "plat": "플래티넘 가격",
            "sell": "판매",
            "use": "사용",
        }
        self.result_table = ttk.Treeview(result_group, columns=columns, show="headings", height=8)
        result_group.rowconfigure(1, weight=1)
        result_group.columnconfigure(0, weight=1)
        yscroll = ttk.Scrollbar(result_group, orient="vertical", command=self.result_table.yview)
        xscroll = ttk.Scrollbar(result_group, orient="horizontal", command=self.result_table.xview)
        self.result_table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        for col in columns:
            self.result_table.heading(col, text=headings[col])
            width = {
                "number": 54,
                "received": 150,
                "date": 110,
                "item": 260,
                "ducats": 90,
                "plat": 110,
                "sell": 130,
                "use": 130,
            }.get(col, 90)
            self.result_table.column(col, width=width, minwidth=60, stretch=True)
        self.result_table.grid(row=1, column=0, sticky="nsew")
        self.result_table.bind("<Button-3>", self._on_result_right_click)
        self.result_table.bind("<Delete>", lambda _event: self._delete_selected_result_row())
        self.result_table.bind("<Double-1>", self._on_result_double_click)
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll.grid(row=2, column=0, sticky="ew")
        self._refresh_result_table()

        details_page = self._plain_page(parent, "details", "OCR / 매칭 상세")
        details = ttk.LabelFrame(details_page, text="OCR 원문 / 매칭 상세")
        details.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        details_page.rowconfigure(0, weight=1)
        details_page.columnconfigure(0, weight=1)
        details.rowconfigure(1, weight=1)
        details.columnconfigure(0, weight=1)
        detail_controls = ttk.Frame(details)
        detail_controls.grid(row=0, column=0, sticky="ew")
        detail_controls.columnconfigure(1, weight=1)
        ttk.Label(detail_controls, text="OCR OBS 소스").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=2)
        ttk.Entry(detail_controls, textvariable=self.obs_ocr_source).grid(row=0, column=1, sticky="ew", padx=(0, 4), pady=2)
        ttk.Button(detail_controls, text="소스 저장", command=self._save_obs_ocr_source).grid(row=0, column=2, sticky="ew", pady=2)
        capture_buttons = ttk.Frame(detail_controls)
        capture_buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self._button_row(
            capture_buttons,
            [
                ("캡쳐", lambda: self._capture_obs_ocr_source_async(run_ocr=False)),
                ("캡쳐 OCR", lambda: self._capture_obs_ocr_source_async(run_ocr=True)),
                ("OCR", self._ocr_last_obs_capture_async),
            ],
        )
        detail_buttons = ttk.Frame(detail_controls)
        detail_buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self._button_row(
            detail_buttons,
            [
                ("OCR 복사", self._copy_ocr),
                ("크롭 열기", self._open_crop),
                ("매칭 수정", self._correct_match),
            ],
        )
        self.details_text = tk.Text(details, height=8)
        details_scroll = ttk.Scrollbar(details, orient="vertical", command=self.details_text.yview)
        self.details_text.configure(yscrollcommand=details_scroll.set, wrap="word")
        self.details_text.grid(row=1, column=0, sticky="nsew")
        details_scroll.grid(row=1, column=1, sticky="ns")

        overlay_page = self._plain_page(parent, "overlay_preview", "오버레이 미리보기")
        overlay = ttk.LabelFrame(overlay_page, text="오버레이 미리보기 / 페이로드")
        overlay.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        overlay_page.rowconfigure(0, weight=1)
        overlay_page.columnconfigure(0, weight=1)
        overlay.rowconfigure(1, weight=1)
        overlay.columnconfigure(0, weight=1)
        overlay_buttons = ttk.Frame(overlay)
        overlay_buttons.grid(row=0, column=0, sticky="ew")
        self._button_row(overlay_buttons, [("오버레이 복사", self._copy_overlay)])
        self.overlay_text = tk.Text(overlay, height=7, bg="black", fg="#00ff66")
        overlay_scroll = ttk.Scrollbar(overlay, orient="vertical", command=self.overlay_text.yview)
        self.overlay_text.configure(yscrollcommand=overlay_scroll.set, wrap="word")
        self.overlay_text.grid(row=1, column=0, sticky="nsew")
        overlay_scroll.grid(row=1, column=1, sticky="ns")

        log_page = self._plain_page(parent, "event_log", "이벤트 로그")
        log = ttk.LabelFrame(log_page, text="이벤트 로그")
        log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        log_page.rowconfigure(0, weight=1)
        log_page.columnconfigure(0, weight=1)
        log.rowconfigure(0, weight=1)
        log.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log, height=8, bg="black", fg="#00ff66")
        log_scroll = ttk.Scrollbar(log, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _dashboard_card(self, parent, title: str, row: int, column: int, columnspan: int = 1):
        card = ttk.LabelFrame(parent, text=title)
        card.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=4, pady=0)
        card.columnconfigure(0, weight=1)
        return card

    def _button_row(self, parent, buttons: list[tuple[str, object]], max_columns: int = 3) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(4, 2))
        for column in range(max_columns):
            row.columnconfigure(column, weight=1)
        for index, (label, command) in enumerate(buttons):
            grid_row, grid_col = divmod(index, max_columns)
            ttk.Button(row, text=label, command=command).grid(row=grid_row, column=grid_col, sticky="ew", padx=2, pady=2)

    def _build_home_action_buttons(self, parent) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=4, pady=6)
        for column in range(3):
            row.columnconfigure(column, weight=1)
        actions: list[tuple[str, object]] = [
            ("전체 재연결", self._auto_connect_obs_websocket),
            ("웹소켓 설정", lambda: self._select_page("obs")),
            ("좌표입력", lambda: self._select_page("roi")),
            ("자동 감지", self._toggle_auto_detect),
            ("오버레이", self._toggle_overlay_remote),
            ("1PC 모드", self._toggle_one_pc_mode),
            ("1회 작동", lambda: self._run_pipeline_async("gui_button")),
        ]
        for index, (label, command) in enumerate(actions):
            grid_row, grid_column = divmod(index, 3)
            button = tk.Button(row, text=label, command=command, relief="raised", bd=1)
            button.grid(row=grid_row, column=grid_column, sticky="ew", padx=2, pady=2)
            if label == "자동 감지":
                self.home_auto_toggle_button = button
            elif label == "1PC 모드":
                self.home_one_pc_toggle_button = button
            elif label == "오버레이":
                self.home_overlay_toggle_button = button
        self._refresh_home_toggle_buttons()

    def _refresh_home_toggle_buttons(self) -> None:
        fg = self._theme_palette()["fg"]
        if self.home_auto_toggle_button is not None:
            self.home_auto_toggle_button.configure(text=home_toggle_text("자동 감지", self.auto_running), fg=fg)
        if self.home_one_pc_toggle_button is not None:
            self.home_one_pc_toggle_button.configure(text=home_toggle_text("1PC 모드", self.one_pc_mode_active), fg=fg)
        if self.home_overlay_toggle_button is not None:
            self.home_overlay_toggle_button.configure(text=home_toggle_text("오버레이", self._overlay_remote_is_on()), fg=fg)

    def _entry_row(self, parent, label: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=label, width=14).grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(row, textvariable=variable).grid(row=0, column=1, sticky="ew")

    def _password_row(self, parent, label: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=label, width=14).grid(row=0, column=0, sticky="w", padx=(0, 6))
        entry = ttk.Entry(row, textvariable=variable, show="*")
        entry.grid(row=0, column=1, sticky="ew")
        button = ttk.Button(row, text="👁", width=3, command=self._toggle_obs_password_visibility)
        button.grid(row=0, column=2, sticky="e", padx=(4, 0))
        self.obs_password_entry = entry
        self.obs_password_toggle = button

    def _combo_row(self, parent, label: str, variable: tk.StringVar, values: list[str]) -> ttk.Combobox:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text=label, width=14).grid(row=0, column=0, sticky="w", padx=(0, 6))
        combo = ttk.Combobox(row, textvariable=variable, values=values)
        combo.grid(row=0, column=1, sticky="ew")
        return combo

    def _mapped_combo_row(
        self,
        parent,
        label: str,
        variable: tk.StringVar,
        labels: dict[str, str],
        fallback_values: list[str] | None = None,
    ) -> ttk.Combobox:
        display_var = tk.StringVar()
        values = list(fallback_values or labels.keys())
        for key in labels:
            if key not in values:
                values.append(key)
        display_values = [labels.get(value, value) for value in values]
        combo = self._combo_row(parent, label, display_var, display_values)
        display_to_value = {labels.get(value, value): value for value in values}

        def sync_display(*_args: object) -> None:
            value = variable.get()
            display = labels.get(value, value)
            if display_var.get() != display:
                display_var.set(display)

        def sync_value(*_args: object) -> None:
            display = display_var.get()
            value = display_to_value.get(display, display)
            if variable.get() != value:
                variable.set(value)

        variable.trace_add("write", sync_display)
        display_var.trace_add("write", sync_value)
        self._mapped_display_vars.append(display_var)
        sync_display()
        return combo

    def _roi_slot_row(self, parent, slot_index: int, vars_row: dict[str, tk.StringVar]) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        row.columnconfigure(1, weight=1)
        ttk.Entry(row, textvariable=vars_row["label"], width=14).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        fields = ttk.Frame(row)
        fields.grid(row=0, column=1, sticky="ew")
        for index, key in enumerate(("x", "y", "w", "h")):
            fields.columnconfigure(index, weight=1)
            cell = ttk.Frame(fields)
            cell.grid(row=0, column=index, sticky="ew", padx=(0, 4))
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, text=key.upper(), width=2).grid(row=0, column=0, sticky="w")
            ttk.Entry(cell, textvariable=vars_row[key], width=7).grid(row=0, column=1, sticky="ew")

    def _source_row(self, parent, variables: list[tk.StringVar], enabled_flags: list[tk.BooleanVar] | None = None) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        for index, variable in enumerate(variables):
            row.columnconfigure(index, weight=1)
            cell = ttk.Frame(row)
            cell.grid(row=0, column=index, sticky="ew", padx=(0, 4))
            cell.columnconfigure(0, weight=1)
            ttk.Label(cell, text=f"{index + 1}번 칸").grid(row=0, column=0, sticky="w")
            if enabled_flags is not None:
                ttk.Checkbutton(cell, text="사용", variable=enabled_flags[index]).grid(row=0, column=1, sticky="e")
            ttk.Entry(cell, textvariable=variable).grid(row=1, column=0, columnspan=2, sticky="ew")

    def _load_roi_slot_rects_from_config(self) -> None:
        roi = self.config.section("roi")
        raw_rects = roi.get("slot_name_rects", [])
        if isinstance(raw_rects, list) and len(raw_rects) == 4 and all(isinstance(row, dict) for row in raw_rects):
            source_rects = raw_rects
        else:
            source_rects = [r.to_dict() for r in load_roi_preset(str(roi.get("preset", "default-virtual-1080p")).strip()).slot_name_rects]
            if len(source_rects) != 4:
                source_rects = [{"x": "", "y": "", "w": "", "h": ""} for _ in range(4)]
        for row_vars, row_data in zip(self.roi_slot_rects, source_rects):
            row_vars["x"].set(str(row_data.get("x", "")))
            row_vars["y"].set(str(row_data.get("y", "")))
            row_vars["w"].set(str(row_data.get("w", "")))
            row_vars["h"].set(str(row_data.get("h", "")))
        labels = self.config.section("roi").get("slot_labels", [])
        if not isinstance(labels, list) or len(labels) != 4:
            labels = [f"{index}번 칸" for index in range(1, 5)]
        for index, row_vars in enumerate(self.roi_slot_rects):
            row_vars["label"].set(str(labels[index] or f"{index + 1}번 칸"))

    def _load_source_names_from_config(self) -> None:
        obs_cfg = self.config.section("obs_websocket")
        input_sources = obs_cfg.get("browser_sources", [])
        output_sources = obs_cfg.get("text_sources", [])
        output_enabled = obs_cfg.get("text_sources_enabled", [])
        if not isinstance(input_sources, list) or len(input_sources) != 4:
            input_sources = [f"B{index}" for index in range(1, 5)]
        if not isinstance(output_sources, list) or len(output_sources) != 4:
            output_sources = [f"T{index}" for index in range(1, 5)]
        elif [str(value).strip().upper() for value in output_sources] == ["P1", "P2", "P3", "P4"]:
            output_sources = [f"T{index}" for index in range(1, 5)]
        if not isinstance(output_enabled, list) or len(output_enabled) != 4:
            output_enabled = [True, True, True, True]
        for index, variable in enumerate(self.obs_input_sources):
            variable.set(str(input_sources[index] or f"B{index + 1}"))
        for index, variable in enumerate(self.obs_output_sources):
            variable.set(str(output_sources[index] or f"T{index + 1}"))
        for index, variable in enumerate(self.obs_output_source_enabled):
            variable.set(bool(output_enabled[index]))

    def _roi_slot_rect_payload(self) -> list[dict[str, int]]:
        payload: list[dict[str, int]] = []
        for row_vars in self.roi_slot_rects:
            raw = {key: row_vars[key].get().strip() for key in ("x", "y", "w", "h")}
            if all(not value for value in raw.values()):
                return []
            payload.append(
                {
                    "x": int(raw["x"]),
                    "y": int(raw["y"]),
                    "w": int(raw["w"]),
                    "h": int(raw["h"]),
                }
            )
        return payload

    def _preset_ids(self, kind: str) -> list[str]:
        preset_dir = PROJECT_ROOT / "presets" / kind
        if not preset_dir.exists():
            return []
        return sorted(path.stem for path in preset_dir.glob("*.json") if path.is_file())

    def _preset_path(self, kind: str, preset_id: str) -> Path:
        return PROJECT_ROOT / "presets" / kind / f"{preset_id}.json"

    def _refresh_combo_options(
        self,
        kind: str,
        field: tk.StringVar,
        combo: ttk.Combobox,
        channel: str,
    ) -> None:
        current = field.get()
        kind_labels = {"detector": "감지", "roi": "ROI", "ocr": "OCR"}
        options, warning = preserve_current_option(current, self._preset_ids(kind), f"{kind_labels.get(kind, kind)} 프리셋")
        combo["values"] = options
        if warning:
            self._stage_log(channel.upper(), warning)

    def _refresh_auto_preset_options(self) -> None:
        self._refresh_combo_options("detector", self.auto_preset, self.auto_preset_combo, "DETECT")

    def _refresh_ocr_preset_options(self) -> None:
        self._refresh_combo_options("ocr", self.ocr_preprocessing_preset, self.ocr_preset_combo, "OCR")

    def _open_preset_file(self, kind: str, preset_id: str, label: str) -> None:
        preset_path = self._preset_path(kind, preset_id)
        if not preset_path.exists():
            self._stage_log("CONFIG", f"{label} 파일을 찾을 수 없음: {preset_path.name}")
            return
        os.startfile(str(preset_path))

    def _edit_detector_preset(self) -> None:
        self._open_preset_file("detector", self.auto_preset.get(), "감지 프리셋")

    def _edit_ocr_preset(self) -> None:
        self._open_preset_file("ocr", self.ocr_preprocessing_preset.get(), "OCR 프리셋")

    def _import_detector_rects(self) -> None:
        payload = self.last_detector_payload
        if not payload:
            self._stage_log("ROI", "가져올 감지 결과가 아직 없음")
            return
        rects = payload.get("slot_rects")
        if not isinstance(rects, list) or len(rects) != 4:
            self._stage_log("ROI", "감지 결과에 사용할 수 있는 보상칸 좌표가 없음")
            return
        for row_vars, rect in zip(self.roi_slot_rects, rects):
            if not isinstance(rect, dict):
                continue
            row_vars["x"].set(str(rect.get("x", "")))
            row_vars["y"].set(str(rect.get("y", "")))
            row_vars["w"].set(str(rect.get("w", "")))
            row_vars["h"].set(str(rect.get("h", "")))
        self._apply_gui_to_runtime()
        self._stage_log("ROI", "감지 보상칸 좌표를 ROI 필드로 가져옴")

    def _load_config_to_gui(self) -> None:
        self._loading_config = True
        cfg = self.config.data
        try:
            self.dark_mode = bool(cfg.get("ui", {}).get("dark_mode", False))
            self.capture_mode.set(cfg["capture"]["mode"])
            self.sample_path.set(cfg["capture"]["sample_image_path"])
            self.capture_monitor_index.set(str(cfg["capture"].get("monitor_index", 0)))
            self.auto_enabled.set(cfg["auto"]["enabled"])
            self.auto_interval.set(str(cfg["auto"].get("detect_interval_ms", 3000)))
            self.auto_cooldown.set(str(cfg["auto"].get("cooldown_ms", 3000)))
            self.auto_threshold.set(str(cfg["auto"]["confidence_threshold"]))
            self.auto_preset.set(cfg["auto"]["detector_preset"])
            self.auto_min_ocr_slots.set(str(cfg["auto"].get("min_ocr_slots_for_output", 2)))
            self.roi_preset.set(cfg.get("roi", {}).get("preset", "default-virtual-1080p"))
            self.roi_scale.set(str(cfg.get("roi", {}).get("ui_scale", 1.0)))
            self._load_roi_slot_rects_from_config()
            self._load_source_names_from_config()
            self.hotkey_enabled.set(cfg["hotkey"]["enabled"])
            self.hotkey_global.set(bool(cfg["hotkey"].get("register_global", False)))
            self.hotkey_combo.set(cfg["hotkey"]["combo"])
            self.hotkey_debounce.set(str(cfg["hotkey"]["debounce_ms"]))
            ocr_provider = str(cfg["ocr"].get("provider", "paddleocr_v5"))
            self.ocr_provider.set(ocr_provider)
            self.ocr_language.set(cfg["ocr"]["language"])
            self.ocr_timeout.set(str(cfg["ocr"]["timeout_ms"]))
            self.ocr_min_confidence.set(str(cfg["ocr"].get("min_confidence", 0.8)))
            self.ocr_preprocessing_preset.set(str(cfg["ocr"].get("preprocessing_preset", "default-korean-ui")))
            self.db_fixture.set(cfg["data"]["item_fixture"])
            self.price_db_path.set(str(cfg["data"].get("price_db_path", "data/market_cache/warframe_market_prices.json")))
            self.item_wiki_dir.set(str(cfg["data"].get("item_wiki_dir", "data/item_wiki")))
            self.market_wiki_dir.set(str(cfg["data"].get("market_wiki_dir", "data/market_wiki")))
            self.market_live_enabled.set(bool(cfg["data"].get("market_live_enabled", True)))
            self.market_live_timeout.set(str(cfg["data"].get("market_live_timeout_ms", 1500)))
            self.market_cache_same_day_only.set(bool(cfg["data"].get("market_cache_same_day_only", True)))
            self.sample_set_dir.set(str(cfg.get("diagnostics", {}).get("sample_set_dir", "samples\\reward_screens")))
            self.match_confident.set(str(cfg.get("matching", {}).get("confident_threshold", 0.92)))
            self.match_usable.set(str(cfg.get("matching", {}).get("usable_threshold", 0.80)))
            self.match_uncertain.set(str(cfg.get("matching", {}).get("uncertain_threshold", 0.65)))
            self.alias_learning.set(bool(cfg.get("matching", {}).get("enable_alias_learning", False)))
            overlay_mode = str(cfg["overlay"].get("mode", "window"))
            self.overlay_enabled.set(bool(cfg["overlay"].get("enabled", overlay_mode != "disabled")))
            self.overlay_mode.set(overlay_mode)
            self.overlay_layout.set(str(cfg["overlay"].get("layout", "horizontal") or "horizontal"))
            self.overlay_position.set(str(cfg["overlay"].get("position_preset", "top-right")))
            self.overlay_topmost.set(bool(cfg["overlay"].get("always_on_top", True)))
            self.overlay_click_through.set(bool(cfg["overlay"].get("click_through", False)))
            self.overlay_x.set(str(cfg["overlay"].get("x", 20)))
            self.overlay_y.set(str(cfg["overlay"].get("y", 80)))
            self.overlay_w.set(str(cfg["overlay"].get("w", 620)))
            self.overlay_h.set(str(cfg["overlay"].get("h", 180)))
            self.overlay_opacity.set(str(cfg["overlay"].get("opacity", 0.92)))
            self.overlay_clear_ms.set(str(cfg["overlay"].get("clear_after_ms", 6000)))
            obs_cfg = cfg.get("obs_websocket", {})
            self.obs_enabled.set(bool(obs_cfg.get("enabled", False)))
            self.obs_host.set(str(obs_cfg.get("host", "127.0.0.1")))
            self.obs_port.set(str(obs_cfg.get("port", 4455)))
            self.obs_timeout.set(str(obs_cfg.get("connect_timeout_ms", 3000)))
            self.obs_ocr_source.set(str(obs_cfg.get("ocr_source_name", "이미지") or "이미지"))
            encrypted_password = str(obs_cfg.get("password_dpapi", ""))
            self._obs_password_decrypt_error = ""
            try:
                decrypted_password = unprotect_secret(encrypted_password) if encrypted_password else ""
                self.obs_status.set("OBS 비밀번호 저장됨" if decrypted_password else "OBS 비밀번호 없음")
            except Exception as exc:
                decrypted_password = ""
                self._obs_password_decrypt_error = str(exc)
                self.obs_status.set("OBS 비밀번호 복호화 실패: 비밀번호를 다시 입력하고 저장하세요")
            self.obs_password.set(decrypted_password)
            self._loaded_obs_password = decrypted_password
            self.obs_password_visible.set(False)
            self._apply_obs_password_visibility()
        finally:
            self._loading_config = False
        self.gui_dirty = False
        self._refresh_profile_title()
        self._refresh_indicators()

    def _apply_gui_to_runtime(self) -> None:
        self.config.set_value("capture", "mode", self.capture_mode.get())
        self.config.set_value("ui", "dark_mode", bool(self.dark_mode))
        self.config.set_value("capture", "sample_image_path", self.sample_path.get())
        self.config.set_value("capture", "monitor_index", int(self.capture_monitor_index.get()))
        self.config.set_value("auto", "enabled", bool(self.auto_enabled.get()))
        self.config.set_value("auto", "detect_interval_ms", int(self.auto_interval.get()))
        self.config.set_value("auto", "cooldown_ms", int(self.auto_cooldown.get()))
        self.config.set_value("auto", "confidence_threshold", float(self.auto_threshold.get()))
        self.config.set_value("auto", "detector_preset", self.auto_preset.get())
        self.config.set_value("auto", "min_ocr_slots_for_output", int(self.auto_min_ocr_slots.get()))
        self.config.set_value("roi", "preset", "default-virtual-1080p")
        self.config.set_value("roi", "ui_scale", float(self.roi_scale.get()))
        self.config.set_value("roi", "slot_labels", [value.get().strip() or f"{index}번 칸" for index, value in enumerate(self.slot_labels, start=1)])
        self.config.set_value("roi", "slot_name_rects", self._roi_slot_rect_payload())
        self.config.set_value("hotkey", "enabled", bool(self.hotkey_enabled.get()))
        self.config.set_value("hotkey", "register_global", bool(self.hotkey_global.get()))
        self.config.set_value("hotkey", "combo", self.hotkey_combo.get())
        self.config.set_value("hotkey", "debounce_ms", int(self.hotkey_debounce.get()))
        self.config.set_value("ocr", "provider", self.ocr_provider.get())
        self.config.set_value("ocr", "language", self.ocr_language.get())
        self.config.set_value("ocr", "timeout_ms", int(self.ocr_timeout.get()))
        self.config.set_value("ocr", "min_confidence", float(self.ocr_min_confidence.get()))
        self.config.set_value("ocr", "preprocessing_preset", self.ocr_preprocessing_preset.get())
        self.config.set_value("data", "item_fixture", self.db_fixture.get())
        self.config.set_value("data", "price_db_path", self.price_db_path.get().strip() or "data/market_cache/warframe_market_prices.json")
        self.config.set_value("data", "item_wiki_dir", self.item_wiki_dir.get().strip() or "data/item_wiki")
        self.config.set_value("data", "market_wiki_dir", self.market_wiki_dir.get().strip() or "data/market_wiki")
        self.config.set_value("data", "market_live_enabled", bool(self.market_live_enabled.get()))
        self.config.set_value("data", "market_live_timeout_ms", int(self.market_live_timeout.get()))
        self.config.set_value("data", "market_cache_same_day_only", bool(self.market_cache_same_day_only.get()))
        self.config.set_value("diagnostics", "sample_set_dir", self.sample_set_dir.get())
        self.config.set_value("matching", "confident_threshold", float(self.match_confident.get()))
        self.config.set_value("matching", "usable_threshold", float(self.match_usable.get()))
        self.config.set_value("matching", "uncertain_threshold", float(self.match_uncertain.get()))
        self.config.set_value("matching", "enable_alias_learning", bool(self.alias_learning.get()))
        self.config.set_value("overlay", "enabled", bool(self.overlay_enabled.get()))
        overlay_mode = self.overlay_mode.get()
        if not self.overlay_enabled.get():
            overlay_mode = "disabled"
        elif overlay_mode == "disabled":
            overlay_mode = "window"
            self.overlay_mode.set(overlay_mode)
        self.config.set_value("overlay", "mode", overlay_mode)
        self.config.set_value("overlay", "layout", self.overlay_layout.get() or "horizontal")
        self.config.set_value("overlay", "position_preset", self.overlay_position.get())
        self.config.set_value("overlay", "always_on_top", bool(self.overlay_topmost.get()))
        self.config.set_value("overlay", "click_through", bool(self.overlay_click_through.get()))
        self.config.set_value("overlay", "x", int(self.overlay_x.get()))
        self.config.set_value("overlay", "y", int(self.overlay_y.get()))
        self.config.set_value("overlay", "w", int(self.overlay_w.get()))
        self.config.set_value("overlay", "h", int(self.overlay_h.get()))
        self.config.set_value("overlay", "opacity", float(self.overlay_opacity.get()))
        self.config.set_value("overlay", "clear_after_ms", int(self.overlay_clear_ms.get()))
        self.config.set_value("obs_websocket", "enabled", bool(self.obs_enabled.get()))
        self.config.set_value("obs_websocket", "host", self.obs_host.get().strip())
        self.config.set_value("obs_websocket", "port", int(self.obs_port.get()))
        self.config.set_value("obs_websocket", "connect_timeout_ms", int(self.obs_timeout.get()))
        self.config.set_value("obs_websocket", "ocr_source_name", self.obs_ocr_source.get().strip() or "이미지")
        self.config.set_value("obs_websocket", "browser_sources", [value.get().strip() or f"B{index}" for index, value in enumerate(self.obs_input_sources, start=1)])
        self.config.set_value("obs_websocket", "text_sources", [value.get().strip() or f"T{index}" for index, value in enumerate(self.obs_output_sources, start=1)])
        self.config.set_value("obs_websocket", "text_sources_enabled", [bool(value.get()) for value in self.obs_output_source_enabled])
        current_obs_password = self.obs_password.get().strip()
        if current_obs_password != self.obs_password.get():
            self.obs_password.set(current_obs_password)
        if self._obs_password_decrypt_error:
            if current_obs_password:
                self.config.set_value("obs_websocket", "password_dpapi", protect_secret(current_obs_password))
                self._loaded_obs_password = current_obs_password
                self._obs_password_decrypt_error = ""
                self.obs_status.set("OBS 비밀번호 다시 저장됨")
        elif current_obs_password != self._loaded_obs_password:
            self.config.set_value("obs_websocket", "password_dpapi", protect_secret(current_obs_password))
            self._loaded_obs_password = current_obs_password
        self.gui_dirty = False
        self._sync_hotkey_from_gui()
        self._refresh_profile_title()
        self._refresh_indicators()

    def _validate_stage(self, stage: str) -> ValidationResult:
        pipeline_stages = {"auto", "gui_button", "hotkey", "sample"}
        if stage in {"capture_test", "detector_test", "auto", "gui_button", "hotkey", "sample", "roi_test", "ocr_test"}:
            capture = validate_capture_config(
                {
                    "mode": self.capture_mode.get(),
                    "sample_image_path": self.sample_path.get(),
                    "monitor_index": self.capture_monitor_index.get(),
                }
            )
            if not capture.ok:
                return capture
        if stage == "hotkey":
            try:
                parse_hotkey(self.hotkey_combo.get())
            except ValueError as exc:
                return ValidationResult.fail([str(exc)])
            debounce = validate_positive_int(self.hotkey_debounce.get(), "단축키 중복 방지 ms")
            if not debounce.ok:
                return debounce
        if stage in {"detector_test", "auto", "gui_button", "hotkey", "sample", "roi_test", "ocr_test", "db_test", "overlay_test"}:
            threshold = validate_threshold(self.auto_threshold.get())
            if not threshold.ok:
                return threshold
        if stage in {"auto", "gui_button", "hotkey", "sample", "roi_test", "ocr_test"}:
            detect_interval = validate_positive_int(self.auto_interval.get(), "자동 감지 간격")
            if not detect_interval.ok:
                return detect_interval
            cooldown = validate_positive_int(self.auto_cooldown.get(), "자동 감지 쿨다운")
            if not cooldown.ok:
                return cooldown
        if stage in {"auto", "gui_button", "hotkey", "sample", "ocr_test"}:
            timeout = validate_positive_int(self.ocr_timeout.get(), "OCR 제한 시간")
            if not timeout.ok:
                return timeout
            min_confidence = validate_threshold(self.ocr_min_confidence.get())
            if not min_confidence.ok:
                return ValidationResult.fail([f"OCR 최소 신뢰도: {'; '.join(min_confidence.errors)}"])
        if stage in pipeline_stages:
            min_ocr_slots = validate_positive_int(self.auto_min_ocr_slots.get(), "자동 출력 최소 OCR 칸")
            if not min_ocr_slots.ok:
                return min_ocr_slots
            try:
                min_ocr_slot_count = int(self.auto_min_ocr_slots.get())
            except ValueError:
                min_ocr_slot_count = 0
            if not 1 <= min_ocr_slot_count <= 4:
                return ValidationResult.fail(["자동 출력 최소 OCR 칸은 1부터 4 사이여야 함"])
        if stage in {"roi_test", "auto", "gui_button", "hotkey", "sample"}:
            try:
                scale = float(self.roi_scale.get())
            except (TypeError, ValueError):
                return ValidationResult.fail(["ROI UI 배율은 숫자여야 함"])
            if scale <= 0:
                return ValidationResult.fail(["ROI UI 배율은 양수여야 함"])
            if any(
                any(slot_row[key].get().strip() for key in ("x", "y", "w", "h"))
                for slot_row in self.roi_slot_rects
            ):
                try:
                    self._roi_slot_rect_payload()
                except ValueError:
                    return ValidationResult.fail(["ROI 보상칸 좌표를 입력할 때는 모든 값이 정수여야 함"])
        if stage in pipeline_stages and bool(self.obs_enabled.get()) and self.obs_ocr_source.get().strip():
            if len(self._ordered_obs_rects_from_list(self.config.section("obs_websocket").get("browser_source_rects", []))) != 4:
                return ValidationResult.fail(["OBS B1~B4 좌표가 아직 없습니다. 홈에서 전체 재연결 또는 입력 / 좌표의 인풋 좌표 갱신을 먼저 실행하세요."])
        if stage in {"db_test", "gui_button", "hotkey", "sample", "ocr_test", "auto"}:
            try:
                fixture = fixture_path(self.db_fixture.get())
            except ValueError as exc:
                return ValidationResult.fail([f"아이템 픽스처 경로가 프로젝트 밖임: {exc}"])
            exists = validate_existing_file(str(fixture), "아이템 픽스처")
            if not exists.ok:
                return exists
        if stage == "sample":
            sample_dir = self.sample_set_dir.get().strip() or "samples\\reward_screens"
            exists = validate_existing_dir(sample_dir, "샘플 세트 폴더")
            if not exists.ok:
                return exists
        for label, value in [
            ("확실 매칭 임계값", self.match_confident.get()),
            ("사용 가능 매칭 임계값", self.match_usable.get()),
            ("불확실 매칭 임계값", self.match_uncertain.get()),
        ]:
            threshold = validate_threshold(value)
            if not threshold.ok:
                return ValidationResult.fail([f"{label}: {'; '.join(threshold.errors)}"])
        if stage in {"overlay_test", "gui_button", "hotkey", "sample", "auto"}:
            for label, value in [
                ("오버레이 W", self.overlay_w.get()),
                ("오버레이 H", self.overlay_h.get()),
                ("오버레이 불투명도", self.overlay_opacity.get()),
                ("자동 지움 ms", self.overlay_clear_ms.get()),
            ]:
                if label == "오버레이 불투명도":
                    try:
                        parsed_opacity = float(value)
                    except (TypeError, ValueError):
                        return ValidationResult.fail(["오버레이 불투명도는 숫자여야 함"])
                    if not 0.2 <= parsed_opacity <= 1.0:
                        return ValidationResult.fail(["오버레이 불투명도는 0.2부터 1.0 사이여야 함"])
                    continue
                parsed = validate_positive_int(value, label)
                if not parsed.ok:
                    return parsed
        if stage in {"overlay_test", "gui_button", "hotkey", "sample", "auto"} and self.overlay_mode.get() == "disabled":
            return ValidationResult.pass_(["오버레이가 꺼져 있어 결과는 디버그/미리보기에만 남음"])
        return ValidationResult.pass_()

    def _save_config(self) -> None:
        one_pc_save = self.one_pc_mode_active and self.one_pc_mode_snapshot is not None
        saved = False
        try:
            self._apply_gui_to_runtime()
            if one_pc_save:
                self._apply_one_pc_snapshot_to_config()
            check = self.controller.run_stage("config_check")
            if check.get("status") != "PASS":
                self._stage_log("CONFIG", f"오류: 설정 검증 실패: {_config_check_message(check)}", level="ERROR")
                return
            self._show_config_warnings(check)
            self.config.save()
            saved = True
        except Exception as exc:
            self._stage_log("CONFIG", f"오류: 설정 저장 실패: {exc}", level="ERROR")
            return
        finally:
            if one_pc_save:
                previous_loading = self._loading_config
                try:
                    self._loading_config = True
                    self._set_one_pc_mode_variables()
                    self._apply_gui_to_runtime()
                finally:
                    self._loading_config = previous_loading
                    if saved:
                        self.gui_dirty = False
                        self.config.dirty = False
        self._refresh_profile_title()
        self._stage_log("CONFIG", "설정 저장됨")

    def _apply_config(self) -> None:
        try:
            self._apply_gui_to_runtime()
            check = self.controller.run_stage("config_check")
            if check.get("status") != "PASS":
                self._stage_log("CONFIG", f"오류: 설정 검증 실패: {_config_check_message(check)}", level="ERROR")
                return
            self._show_config_warnings(check)
        except Exception as exc:
            self._stage_log("CONFIG", f"오류: 설정값이 올바르지 않음: {exc}")
            return
        self._stage_log("CONFIG", "런타임 설정 적용됨")

    def _show_config_warnings(self, check: dict[str, object]) -> None:
        warnings = check.get("value_warnings", [])
        if not isinstance(warnings, list):
            return
        for warning in warnings:
            message = str(warning)
            self._stage_log("CONFIG", f"경고: {message}", level="WARNING")
            if "password_dpapi" in message:
                self.obs_status.set("OBS 비밀번호 복호화 실패: 비밀번호를 다시 입력하고 저장하세요")

    def _revert_config(self) -> None:
        self.config = AppConfig.load(self.config.path)
        self.controller.config = self.config
        self._load_config_to_gui()
        self._sync_hotkey_from_gui()
        self.config.dirty = False
        self._refresh_profile_title()
        self._stage_log("CONFIG", "설정 되돌림")

    def _browse_sample(self) -> None:
        path = filedialog.askopenfilename(
            title="Warframe 보상 화면 스크린샷 선택",
            filetypes=[("이미지", "*.png *.jpg *.jpeg *.bmp *.webp"), ("모든 파일", "*.*")],
        )
        if path:
            self.sample_path.set(path)
            self._apply_gui_to_runtime()
            self._stage_log("CAPTURE", f"샘플 선택됨: {path}")

    def _load_fixture(self) -> None:
        path = filedialog.askopenfilename(
            title="아이템 픽스처 불러오기",
            initialdir=str(resolve_project_path("data")),
            filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        self.db_fixture.set(path)
        self._apply_gui_to_runtime()
        self._stage_log("DB", f"픽스처 선택됨: {path}")

    def _browse_sample_set_dir(self) -> None:
        directory = filedialog.askdirectory(title="샘플 이미지 폴더 선택")
        if not directory:
            return
        self.sample_set_dir.set(directory)
        self._apply_gui_to_runtime()
        self._stage_log("DB", f"샘플 세트 폴더 선택됨: {directory}")

    def _test_hotkey(self) -> None:
        try:
            self._apply_gui_to_runtime()
            validation = self._validate_stage("hotkey")
            if not validation.ok:
                self._stage_log("HOTKEY", f"오류: {'; '.join(validation.errors)}")
                return
            self.hotkey_manager.configure(
                self.hotkey_combo.get(),
                self.hotkey_enabled.get(),
                int(self.hotkey_debounce.get()),
                register_global=bool(self.hotkey_global.get()),
            )
            self.config.set_value("hotkey", "last_registration_error", "")
            self.hotkey_manager.trigger_for_test()
            self._stage_log("HOTKEY", f"상태: {self._hotkey_status_label(self.hotkey_manager.status)}")
        except Exception as exc:
            self.config.set_value("hotkey", "last_registration_error", str(exc))
            self._stage_log("HOTKEY", f"오류: {exc}")

    def _record_hotkey(self) -> None:
        if self.hotkey_manager.backend is not None:
            self.hotkey_manager.backend.unregister()
            self.hotkey_manager.backend = None
            self.hotkey_manager.status = "validated"

        dialog = tk.Toplevel(self.root)
        dialog.title("단축키 입력")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))

        captured = tk.StringVar(value="수정할 단축키를 누르세요")
        detail = tk.StringVar(value="modifier 2개 이상 + 일반 키 1개")
        active_modifiers: set[str] = set()
        accepted = {"value": False}

        body = ttk.Frame(dialog, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        ttk.Label(body, textvariable=captured, font=("", 12, "bold")).grid(row=0, column=0, sticky="ew")
        ttk.Label(body, textvariable=detail).grid(row=1, column=0, sticky="ew", pady=(8, 12))
        ttk.Button(body, text="취소", command=lambda: close(False)).grid(row=2, column=0, sticky="ew")

        def close(ok: bool) -> None:
            accepted["value"] = ok
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            if dialog.winfo_exists():
                dialog.destroy()
            if not ok:
                self._sync_hotkey_from_gui()

        def update_preview() -> None:
            modifiers = [name for name in ("ctrl", "alt", "shift", "win") if name in active_modifiers]
            captured.set(" + ".join(modifiers) if modifiers else "수정할 단축키를 누르세요")

        def on_key_press(event) -> str:
            modifier = self._hotkey_modifier_from_keysym(str(event.keysym))
            if modifier:
                active_modifiers.add(modifier)
                update_preview()
                return "break"
            normal_key = self._hotkey_normal_key_from_event(event)
            if not normal_key:
                detail.set(f"지원하지 않는 일반 키: {event.keysym}")
                return "break"
            modifiers = active_modifiers | self._hotkey_modifiers_from_state(int(getattr(event, "state", 0)))
            if normal_key == "escape" and not modifiers:
                close(False)
                return "break"
            combo = "+".join([*[name for name in ("ctrl", "alt", "shift", "win") if name in modifiers], normal_key])
            try:
                parsed = parse_hotkey(combo)
            except ValueError as exc:
                captured.set(combo or "수정할 단축키를 누르세요")
                detail.set(str(exc))
                return "break"
            self.hotkey_combo.set(parsed.normalized)
            self._apply_gui_to_runtime()
            self._stage_log("HOTKEY", f"단축키 입력됨: {parsed.normalized}")
            close(True)
            return "break"

        def on_key_release(event) -> str:
            modifier = self._hotkey_modifier_from_keysym(str(event.keysym))
            if modifier and modifier in active_modifiers:
                active_modifiers.remove(modifier)
                update_preview()
            return "break"

        dialog.bind("<KeyPress>", on_key_press)
        dialog.bind("<KeyRelease>", on_key_release)
        dialog.update_idletasks()
        dialog.geometry(f"+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 120}")
        dialog.focus_force()

    def _hotkey_modifier_from_keysym(self, keysym: str) -> str | None:
        key = keysym.lower()
        if key.startswith("control"):
            return "ctrl"
        if key.startswith("alt") or key.startswith("option"):
            return "alt"
        if key.startswith("shift"):
            return "shift"
        if key.startswith("super") or key.startswith("win") or key.startswith("meta"):
            return "win"
        return None

    def _hotkey_modifiers_from_state(self, state: int) -> set[str]:
        modifiers: set[str] = set()
        if state & 0x0001:
            modifiers.add("shift")
        if state & 0x0004:
            modifiers.add("ctrl")
        if state & 0x0008 or state & 0x20000:
            modifiers.add("alt")
        if state & 0x0040 or state & 0x0080 or state & 0x40000:
            modifiers.add("win")
        return modifiers

    def _hotkey_normal_key_from_event(self, event) -> str | None:
        keysym = str(event.keysym)
        lower = keysym.lower()
        aliases = {"return": "enter", "escape": "escape", "esc": "escape", "space": "space", "spacebar": "space"}
        if lower in aliases:
            return aliases[lower]
        if self._hotkey_modifier_from_keysym(keysym):
            return None
        char = str(getattr(event, "char", "") or "")
        if len(char) == 1 and char.isprintable() and char not in "\r\n\t":
            return char.lower()
        if len(lower) == 1 and lower.isprintable():
            return lower
        return None

    def _on_hotkey_trigger(self, trigger: str) -> None:
        if self.controller.busy:
            self.last_hotkey_status = "busy"
            self.hotkey_manager.status = "busy"
            self.events.put(("hotkey_busy", trigger))
            return
        self.events.put(("hotkey_trigger", trigger))

    def _sync_hotkey_from_gui(self) -> None:
        try:
            self.hotkey_manager.configure(
                self.hotkey_combo.get(),
                bool(self.hotkey_enabled.get()),
                int(self.hotkey_debounce.get()),
                register_global=bool(self.hotkey_global.get()),
            )
            self.config.set_value("hotkey", "last_registration_error", "")
        except Exception as exc:
            self.config.set_value("hotkey", "last_registration_error", str(exc))
            self._stage_log("HOTKEY", f"동기화 실패: {exc}")

    def _run_pipeline_async(self, trigger: str) -> None:
        if self.controller.busy:
            self._stage_log("PIPE", "건너뜀: 파이프라인이 이미 실행 중")
            return
        try:
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("PIPE", f"오류: 설정값이 올바르지 않음: {exc}")
            return
        validation = self._validate_stage(trigger)
        if not validation.ok:
            self._stage_log("PIPE", f"오류: {'; '.join(validation.errors)}")
            return
        for warning in validation.warnings:
            self._stage_log("PIPE", warning, level="WARNING")
        self.status_var.set(f"분석 중: {self._stage_label(trigger)}")
        self._set_busy_ui(True)
        threading.Thread(target=self._worker_run, args=(trigger,), daemon=True).start()

    def _startup_obs_bootstrap(self) -> None:
        if not self.obs_enabled.get():
            return
        if self.controller.busy or self.ui_busy:
            self.root.after(500, self._startup_obs_bootstrap)
            return
        host = self.obs_host.get().strip()
        if not host:
            return
        self._stage_log("OBS", "시작 자동 확인: OBS 연결과 B/T 좌표를 갱신함")
        self._test_obs_websocket_async(after_connect="input")

    def _startup_ocr_prewarm(self) -> None:
        if self.controller.busy or self.ui_busy:
            self.root.after(500, self._startup_ocr_prewarm)
            return
        if self.ocr_provider.get() != "paddleocr_v5":
            return
        threading.Thread(target=self._worker_ocr_prewarm, daemon=True).start()

    def _worker_ocr_prewarm(self) -> None:
        payload = self.controller.warm_ocr_provider()
        status = payload.get("status", "?")
        duration = payload.get("duration_ms", "?")
        self.events.put(("log", ("OCR", f"PaddleOCR prewarm {status}: {duration}ms")))

    def _run_stage_async(self, stage: str) -> None:
        try:
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("PIPE", f"오류: 설정값이 올바르지 않음: {exc}")
            return
        validation = self._validate_stage(stage)
        if not validation.ok:
            self._stage_log("PIPE", f"오류: {'; '.join(validation.errors)}")
            return
        for warning in validation.warnings:
            self._stage_log("PIPE", warning, level="WARNING")
        self.status_var.set(f"단계 실행 중: {self._stage_label(stage)}")
        self._set_busy_ui(True)
        threading.Thread(target=self._worker_stage, args=(stage,), daemon=True).start()

    def _run_sample_set_async(self) -> None:
        try:
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("PIPE", f"오류: 설정값이 올바르지 않음: {exc}")
            return
        self.status_var.set("샘플 세트 실행 중")
        self._set_busy_ui(True)
        threading.Thread(target=self._worker_sample_set, daemon=True).start()

    def _validate_obs_connection_inputs(self) -> ValidationResult:
        if not self.obs_host.get().strip():
            return ValidationResult.fail(["OBS 서버 IP가 비어 있음"])
        port = validate_int_range(self.obs_port.get(), "OBS 포트", 1, 65535)
        if not port.ok:
            return port
        timeout = validate_int_range(self.obs_timeout.get(), "OBS 연결 제한 시간 ms", 500, 30000)
        if not timeout.ok:
            return timeout
        if self._obs_password_decrypt_error and not self.obs_password.get():
            return ValidationResult.fail(["저장된 OBS 비밀번호 복호화 실패: OBS 연결 페이지에서 비밀번호를 다시 입력하고 저장하세요"])
        return ValidationResult.pass_()

    def _test_obs_websocket_async(self, after_connect: str | None = None) -> bool:
        try:
            self._apply_gui_to_runtime()
            validation = self._validate_obs_connection_inputs()
            if not validation.ok:
                self._stage_log("OBS", f"오류: {'; '.join(validation.errors)}")
                if after_connect == "setup":
                    self.obs_auto_setup_pending = False
                return False
            host = self.obs_host.get().strip()
            port = int(self.obs_port.get())
            timeout_ms = int(self.obs_timeout.get())
            password = self._obs_password_for_connection("OBS")
        except Exception as exc:
            self._stage_log("OBS", f"오류: OBS 설정값이 올바르지 않음: {exc}")
            if after_connect == "setup":
                self.obs_auto_setup_pending = False
            return False
        self.status_var.set("OBS WebSocket 연결 확인 중")
        self.obs_status.set("연결 확인 중")
        self._set_busy_ui(True)
        threading.Thread(
            target=self._worker_obs_websocket,
            args=(host, port, password, max(500, timeout_ms) / 1000, after_connect),
            daemon=True,
        ).start()
        return True

    def _auto_connect_obs_websocket(self) -> None:
        self.obs_enabled.set(True)
        self.obs_auto_setup_pending = True
        self._stage_log("OBS", "자동연결 시작: WebSocket -> B 좌표 -> T 출력테스트")
        self._test_obs_websocket_async(after_connect="setup")

    def _fetch_obs_source_rects_async(self, source_kind: str) -> None:
        try:
            self._apply_gui_to_runtime()
            validation = self._validate_obs_connection_inputs()
            if not validation.ok:
                self._stage_log("OBS", f"오류: {'; '.join(validation.errors)}")
                return
            host = self.obs_host.get().strip()
            port = int(self.obs_port.get())
            timeout_ms = int(self.obs_timeout.get())
            password = self._obs_password_for_connection("OBS")
        except Exception as exc:
            self._stage_log("OBS", f"오류: OBS 설정값이 올바르지 않음: {exc}")
            return
        if source_kind == "input":
            names = [value.get().strip() or f"B{index}" for index, value in enumerate(self.obs_input_sources, start=1)]
            if len(names) != 4 or len({name.lower() for name in names}) != 4:
                self._stage_log("OBS", "오류: 입력소스 B1~B4 이름 4개가 필요하며 중복되면 안 됨")
                return
            self.home_input_status.set("B1~B4 좌표 확인 중")
        else:
            self.home_output_status.set("T1~T4는 좌표 확인 없이 텍스트 출력만 사용")
            self._stage_log("OBS", "T 출력소스는 좌표 확인을 건너뜀")
            return
        self.status_var.set(f"OBS {source_kind} 소스 좌표 확인 중")
        self._set_busy_ui(True)
        threading.Thread(
            target=self._worker_obs_source_rects,
            args=(source_kind, names, host, port, password, max(500, timeout_ms) / 1000),
            daemon=True,
        ).start()

    def _test_obs_outputs_async(self) -> None:
        try:
            self._apply_gui_to_runtime()
            validation = self._validate_obs_connection_inputs()
            if not validation.ok:
                self._stage_log("OBS", f"오류: {'; '.join(validation.errors)}")
                return
            host = self.obs_host.get().strip()
            port = int(self.obs_port.get())
            timeout_ms = int(self.obs_timeout.get())
            password = self._obs_password_for_connection("OBS")
            source_name = self.obs_ocr_source.get().strip() or "이미지"
            output_names = [value.get().strip() or f"T{index}" for index, value in enumerate(self.obs_output_sources, start=1)]
            rects = self._snapshot_ocr_rects()
            ocr_cfg = self._snapshot_ocr_settings()
            data_cfg = dict(self.config.section("data"))
        except Exception as exc:
            self._stage_log("OBS", f"오류: 출력 테스트 설정값이 올바르지 않음: {exc}")
            return
        if len(output_names) != 4 or len({name.lower() for name in output_names}) != 4:
            self._stage_log("OBS", "오류: 출력소스 T1~T4 이름 4개가 필요하며 중복되면 안 됨")
            return
        if len(rects) != 4:
            self._stage_log("OBS", "오류: OCR 대상 B1~B4 좌표 4개가 필요함. 먼저 인풋 좌표 갱신을 실행하세요.")
            return
        self.home_output_status.set("출력테스트 실행 중: T1~T4 초기화")
        self.status_var.set("OBS 출력테스트 실행 중")
        self._set_busy_ui(True)
        threading.Thread(
            target=self._worker_obs_output_test,
            args=(
                host,
                port,
                password,
                max(500, timeout_ms) / 1000,
                source_name,
                output_names,
                rects,
                ocr_cfg,
                data_cfg,
            ),
            daemon=True,
        ).start()

    def _start_auto(self) -> None:
        if self.auto_running:
            self._stage_log("DETECT", "자동 루프가 이미 실행 중")
            return
        try:
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("DETECT", f"오류: 자동 설정값이 올바르지 않음: {exc}")
            return
        validation = self._validate_stage("auto")
        if not validation.ok:
            self._stage_log("DETECT", f"오류: {'; '.join(validation.errors)}")
            return
        for warning in validation.warnings:
            self._stage_log("DETECT", warning, level="WARNING")
        self.auto_running = True
        self.auto_enabled.set(True)
        self.config.set_value("auto", "enabled", True)
        self.gui_dirty = False
        self.last_auto_trigger_ms = 0
        self.auto_detect_busy = False
        self._sync_hotkey_from_gui()
        self._refresh_profile_title()
        self._stage_log("DETECT", "자동 루프 시작됨: 가벼운 감지만 반복")
        self._auto_tick()
        self._refresh_home_toggle_buttons()

    def _stop_auto(self) -> None:
        self.auto_running = False
        self.auto_enabled.set(False)
        self.config.set_value("auto", "enabled", False)
        self.gui_dirty = False
        self._refresh_profile_title()
        self._stage_log("DETECT", "자동 루프 중지됨")
        self._refresh_home_toggle_buttons()

    def _toggle_auto_detect(self) -> None:
        if self.auto_running:
            self._stop_auto()
        else:
            self._start_auto()

    def _overlay_remote_is_on(self) -> bool:
        return bool(self.overlay_window is not None and self.overlay_window.is_visible())

    def _toggle_one_pc_mode(self) -> None:
        if self.one_pc_mode_active:
            self._disable_one_pc_mode()
            return
        self._enable_one_pc_mode()

    def _capture_one_pc_mode_snapshot(self) -> dict[str, object]:
        return {
            "overlay_enabled": bool(self.overlay_enabled.get()),
            "overlay_mode": self.overlay_mode.get(),
            "overlay_layout": self.overlay_layout.get(),
            "overlay_position": self.overlay_position.get(),
            "overlay_topmost": bool(self.overlay_topmost.get()),
            "overlay_click_through": bool(self.overlay_click_through.get()),
            "overlay_x": self.overlay_x.get(),
            "overlay_y": self.overlay_y.get(),
            "overlay_w": self.overlay_w.get(),
            "overlay_h": self.overlay_h.get(),
            "overlay_opacity": self.overlay_opacity.get(),
            "overlay_clear_ms": self.overlay_clear_ms.get(),
            "obs_output_source_enabled": [bool(value.get()) for value in self.obs_output_source_enabled],
        }

    def _restore_one_pc_mode_snapshot(self, snapshot: dict[str, object]) -> None:
        self.overlay_enabled.set(bool(snapshot.get("overlay_enabled", True)))
        self.overlay_mode.set(str(snapshot.get("overlay_mode", "window") or "window"))
        self.overlay_layout.set(str(snapshot.get("overlay_layout", "horizontal") or "horizontal"))
        self.overlay_position.set(str(snapshot.get("overlay_position", "custom") or "custom"))
        self.overlay_topmost.set(bool(snapshot.get("overlay_topmost", True)))
        self.overlay_click_through.set(bool(snapshot.get("overlay_click_through", False)))
        self.overlay_x.set(str(snapshot.get("overlay_x", self.overlay_x.get())))
        self.overlay_y.set(str(snapshot.get("overlay_y", self.overlay_y.get())))
        self.overlay_w.set(str(snapshot.get("overlay_w", self.overlay_w.get())))
        self.overlay_h.set(str(snapshot.get("overlay_h", self.overlay_h.get())))
        self.overlay_opacity.set(str(snapshot.get("overlay_opacity", self.overlay_opacity.get())))
        self.overlay_clear_ms.set(str(snapshot.get("overlay_clear_ms", self.overlay_clear_ms.get())))
        enabled_values = snapshot.get("obs_output_source_enabled", [])
        if isinstance(enabled_values, list):
            for variable, value in zip(self.obs_output_source_enabled, enabled_values):
                variable.set(bool(value))

    def _apply_one_pc_snapshot_to_config(self) -> None:
        snapshot = self.one_pc_mode_snapshot
        if snapshot is None:
            return
        overlay_values = {
            "enabled": bool(snapshot.get("overlay_enabled", True)),
            "mode": str(snapshot.get("overlay_mode", "window") or "window"),
            "layout": str(snapshot.get("overlay_layout", "horizontal") or "horizontal"),
            "position_preset": str(snapshot.get("overlay_position", "custom") or "custom"),
            "always_on_top": bool(snapshot.get("overlay_topmost", True)),
            "click_through": bool(snapshot.get("overlay_click_through", False)),
            "x": int(snapshot.get("overlay_x", self.overlay_x.get()) or 0),
            "y": int(snapshot.get("overlay_y", self.overlay_y.get()) or 0),
            "w": int(snapshot.get("overlay_w", self.overlay_w.get()) or 620),
            "h": int(snapshot.get("overlay_h", self.overlay_h.get()) or 180),
            "opacity": float(snapshot.get("overlay_opacity", self.overlay_opacity.get()) or 0.92),
            "clear_after_ms": int(snapshot.get("overlay_clear_ms", self.overlay_clear_ms.get()) or 0),
        }
        for key, value in overlay_values.items():
            self.config.set_value("overlay", key, value)
        enabled_values = snapshot.get("obs_output_source_enabled", [])
        if isinstance(enabled_values, list):
            self.config.set_value("obs_websocket", "text_sources_enabled", [bool(value) for value in enabled_values])

    def _set_one_pc_mode_variables(self) -> None:
        width, height = self._overlay_layout_size("vertical")
        self.overlay_enabled.set(True)
        self.overlay_mode.set("window")
        self.overlay_layout.set("vertical")
        self.overlay_position.set("top-right")
        self.overlay_topmost.set(True)
        self.overlay_click_through.set(True)
        self.overlay_w.set(str(width))
        self.overlay_h.set(str(height))
        for variable in self.obs_output_source_enabled:
            variable.set(False)

    def _enable_one_pc_mode(self) -> None:
        self.one_pc_mode_snapshot = self._capture_one_pc_mode_snapshot()
        previous_gui_dirty = self.gui_dirty
        previous_config_dirty = self.config.dirty
        previous_loading = self._loading_config
        try:
            self._loading_config = True
            self._set_one_pc_mode_variables()
            if self.obs_text_clear_after_id is not None:
                self.root.after_cancel(self.obs_text_clear_after_id)
                self.obs_text_clear_after_id = None
            if self.obs_capture_overlay_window is not None:
                self.obs_capture_overlay_window.clear()
                self.obs_capture_overlay_window.hide()
            self._apply_gui_to_runtime()
        finally:
            self._loading_config = previous_loading
            self.gui_dirty = previous_gui_dirty
            self.config.dirty = previous_config_dirty
        self._clear_current_obs_text_outputs_for_one_pc_mode()
        self.one_pc_mode_active = True
        self._refresh_profile_title()
        self._refresh_home_toggle_buttons()
        previous_gui_dirty = self.gui_dirty
        previous_config_dirty = self.config.dirty
        self._show_overlay_window_now()
        self.gui_dirty = previous_gui_dirty
        self.config.dirty = previous_config_dirty
        self._refresh_profile_title()
        self._stage_log("OVERLAY", "1PC 모드 켜짐: T 출력 비활성화, 일반 오버레이 세로/오른쪽 위 고정")

    def _clear_current_obs_text_outputs_for_one_pc_mode(self) -> None:
        names = [value.get().strip() for value in self.obs_output_sources if value.get().strip()]
        if not names:
            return
        self._clear_obs_text_outputs_async(names)

    def _disable_one_pc_mode(self) -> None:
        snapshot = self.one_pc_mode_snapshot
        previous_gui_dirty = self.gui_dirty
        previous_config_dirty = self.config.dirty
        previous_loading = self._loading_config
        try:
            self._loading_config = True
            if snapshot is not None:
                self._restore_one_pc_mode_snapshot(snapshot)
            self._apply_gui_to_runtime()
        finally:
            self._loading_config = previous_loading
            self.gui_dirty = previous_gui_dirty
            self.config.dirty = previous_config_dirty
        self.one_pc_mode_active = False
        self.one_pc_mode_snapshot = None
        payload = self._current_overlay_payload()
        if payload:
            self._apply_overlay_window(payload)
        else:
            self._clear_overlay_window()
        self._refresh_profile_title()
        self._refresh_home_toggle_buttons()
        self._stage_log("OVERLAY", "1PC 모드 꺼짐: 기존 오버레이/T 출력 설정 복원")

    def _toggle_overlay_remote(self) -> None:
        if self.one_pc_mode_active:
            self._set_one_pc_mode_variables()
            if self.obs_capture_overlay_window is not None:
                self.obs_capture_overlay_window.clear()
                self.obs_capture_overlay_window.hide()
            self._show_overlay_window_now()
            self._stage_log("OVERLAY", "1PC 모드에서는 일반 오버레이 창을 유지함")
            self._refresh_home_toggle_buttons()
            return
        if self._overlay_remote_is_on():
            self.overlay_enabled.set(False)
            self.overlay_mode.set("disabled")
            if self.overlay_clear_after_id is not None:
                self.root.after_cancel(self.overlay_clear_after_id)
                self.overlay_clear_after_id = None
            self._clear_overlay_window()
            try:
                self._apply_gui_to_runtime()
            except Exception as exc:
                self._stage_log("OVERLAY", f"오류: 오버레이 리모컨 상태 저장 실패: {exc}", level="ERROR")
                return
            self._stage_log("OVERLAY", "오버레이 리모컨 꺼짐")
            self._refresh_home_toggle_buttons()
            return
        self._show_overlay_window_now()
        self._refresh_home_toggle_buttons()

    def _auto_tick(self) -> None:
        if not self.auto_running:
            return
        auto_cfg = self.config.section("auto")
        interval = int(auto_cfg.get("detect_interval_ms", 3000))
        cooldown = int(auto_cfg.get("cooldown_ms", 3000))
        now = int(time.monotonic() * 1000)
        block_reason = self._auto_output_block_reason(now)
        if block_reason:
            if now - self.last_auto_output_block_log_ms >= 2000:
                self.last_auto_output_block_log_ms = now
                self._stage_log("DETECT", f"자동 대기: {block_reason}")
            self.root.after(max(50, interval), self._auto_tick)
            return
        if not self.controller.busy and not self.auto_detect_busy and now - self.last_auto_trigger_ms >= cooldown:
            self.auto_detect_busy = True
            threading.Thread(target=self._worker_auto_detect, daemon=True).start()
        self.root.after(max(50, interval), self._auto_tick)

    def _auto_output_block_reason(self, now_ms: int | None = None) -> str:
        now = int(time.monotonic() * 1000) if now_ms is None else now_ms
        if self.overlay_clear_after_id is not None or self.result_overlay_output_active:
            return "오버레이 결과 표시 중"
        if self.obs_text_clear_after_id is not None or self.result_obs_text_output_active:
            return "OBS T 출력 결과 표시 중"
        if now < self.result_output_block_until_ms:
            remaining = max(1, (self.result_output_block_until_ms - now + 999) // 1000)
            return f"결과 출력 후 안전 대기 {remaining}s"
        return ""

    def _worker_auto_detect(self) -> None:
        try:
            result = self.controller.run_auto_detect()
            self.events.put(("auto_detect", result))
        except Exception as exc:
            self.events.put(("auto_detect_error", str(exc)))

    def _worker_run(self, trigger: str) -> None:
        try:
            result = self.controller.run_pipeline(trigger=trigger)
            self.events.put(("result", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _worker_stage(self, stage: str) -> None:
        try:
            result = self.controller.run_stage(stage)
            self.events.put(("stage", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _worker_sample_set(self) -> None:
        try:
            result = self.controller.run_sample_set(self.sample_set_dir.get() or "samples\\reward_screens")
            self.events.put(("stage", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _worker_obs_websocket(self, host: str, port: int, password: str, timeout: float, after_connect: str | None = None) -> None:
        result = check_obs_websocket(host, port, password, timeout).to_dict()
        result["after_connect"] = after_connect
        self.events.put(("obs_websocket", result))

    def _worker_obs_source_rects(
        self,
        source_kind: str,
        names: list[str],
        host: str,
        port: int,
        password: str,
        timeout: float,
    ) -> None:
        result = fetch_obs_source_rects(host, port, password, names, timeout)
        result["source_kind"] = source_kind
        result["source_names"] = names
        self.events.put(("obs_source_rects", result))

    def _worker_obs_text_outputs(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float,
        text_by_source: dict[str, str],
        is_clear: bool = False,
        clear_after_ms: int = 0,
    ) -> None:
        result = update_obs_text_sources(host, port, password, text_by_source, timeout)
        if _should_retry_obs_text_update(result):
            first_error = str(result.get("error", ""))
            time.sleep(0.15)
            retry = update_obs_text_sources(host, port, password, text_by_source, timeout)
            retry["retry_count"] = 1
            retry["first_error"] = first_error
            result = retry
        result["text_by_source"] = text_by_source
        result["is_clear"] = is_clear
        result["clear_after_ms"] = clear_after_ms
        self.events.put(("obs_text_outputs", result))

    def _worker_obs_output_test(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float,
        source_name: str,
        output_names: list[str],
        rects: list[dict[str, int]],
        ocr_cfg: dict[str, object],
        data_cfg: dict[str, object],
    ) -> None:
        artifact = self._manual_artifact_writer()
        payload: dict[str, object] = {
            "stage": "obs_output_test",
            "status": "failed",
            "source_name": source_name,
            "output_names": output_names,
            "debug_dir": str(artifact.run_dir),
        }
        reset_text = {name: "0 Du / 0 pl" for name in output_names}
        payload["reset_text_by_source"] = reset_text
        reset_result = update_obs_text_sources(host, port, password, reset_text, timeout)
        payload["reset_update"] = reset_result
        if reset_result.get("status") == "failed":
            payload["error"] = reset_result.get("error", "T1~T4 초기화 실패")
            self.events.put(("obs_output_test", payload))
            return
        capture_result = capture_obs_source_screenshot(
            host,
            port,
            password,
            source_name,
            timeout,
            image_format="jpg",
            image_compression_quality=100,
        )
        image_bytes = capture_result.pop("image_bytes", b"")
        payload["capture"] = capture_result
        if capture_result.get("status") != "captured" or not isinstance(image_bytes, bytes):
            payload["error"] = capture_result.get("error", "OBS OCR 소스 캡쳐 실패")
            self.events.put(("obs_output_test", payload))
            return
        capture_path = artifact.write_binary("obs_source.png", image_bytes)
        payload["capture_path"] = capture_path
        try:
            ocr_payload = self._ocr_capture_file(capture_path, source_name, rects, ocr_cfg, artifact)
            payload["ocr_payload"] = ocr_payload
            output_rows, price_payload = self._build_obs_output_test_rows(ocr_payload.get("ocr", []), data_cfg)
            payload["price_payload"] = price_payload
            payload["output_rows"] = output_rows
            final_text = {
                output_names[index - 1]: str(row.get("text", "0 Du / - pl\n미인식"))
                for index, row in enumerate(output_rows, start=1)
                if index - 1 < len(output_names)
            }
            payload["final_text_by_source"] = final_text
            final_result = update_obs_text_sources(host, port, password, final_text, timeout)
            payload["final_update"] = final_result
            payload["recognized_count"] = sum(1 for row in output_rows if row.get("matched"))
            payload["status"] = "PASS" if final_result.get("status") in {"updated", "partial"} else "FAIL"
            if final_result.get("status") == "failed":
                payload["error"] = final_result.get("error", "최종 출력 갱신 실패")
        except Exception as exc:
            payload["error"] = str(exc)
        self.events.put(("obs_output_test", payload))

    def _capture_obs_ocr_source_async(self, run_ocr: bool) -> None:
        try:
            self._apply_gui_to_runtime()
            host = self.obs_host.get().strip()
            port = int(self.obs_port.get())
            timeout_ms = int(self.obs_timeout.get())
            password = self._obs_password_for_connection("OCR")
            source_name = self.obs_ocr_source.get().strip() or "이미지"
            rects = self._snapshot_ocr_rects() if run_ocr else []
            ocr_cfg = self._snapshot_ocr_settings()
        except Exception as exc:
            self._stage_log("OCR", f"오류: OBS OCR 설정값이 올바르지 않음: {exc}")
            return
        if not host:
            self._stage_log("OCR", "오류: OBS 서버 IP가 비어 있음")
            return
        if run_ocr and len(rects) != 4:
            self._stage_log("OCR", "오류: OCR 대상 좌표 4개가 필요함. 먼저 OBS input 또는 입력 / 좌표를 확인하세요.")
            return
        self.status_var.set("OBS 소스 캡쳐 OCR 중" if run_ocr else "OBS 소스 캡쳐 중")
        self._set_busy_ui(True)
        threading.Thread(
            target=self._worker_obs_source_capture,
            args=(host, port, password, max(500, timeout_ms) / 1000, source_name, run_ocr, rects, ocr_cfg),
            daemon=True,
        ).start()

    def _ocr_last_obs_capture_async(self) -> None:
        path = self.last_obs_capture_path
        if not path or not Path(path).exists():
            self._stage_log("OCR", "오류: OCR할 OBS 캡쳐가 없음. 먼저 캡쳐를 실행하세요.")
            return
        try:
            self._apply_gui_to_runtime()
            source_name = self.obs_ocr_source.get().strip() or self.last_obs_capture_source_name or "이미지"
            rects = self._snapshot_ocr_rects()
            ocr_cfg = self._snapshot_ocr_settings()
        except Exception as exc:
            self._stage_log("OCR", f"오류: OCR 설정값이 올바르지 않음: {exc}")
            return
        if self.last_obs_capture_source_name and self.last_obs_capture_source_name != source_name:
            self._stage_log("OCR", f"주의: 마지막 캡쳐 소스는 {self.last_obs_capture_source_name}, 현재 소스는 {source_name}", level="WARNING")
        if len(rects) != 4:
            self._stage_log("OCR", "오류: OCR 대상 좌표 4개가 필요함. 먼저 OBS input 또는 입력 / 좌표를 확인하세요.")
            return
        self.status_var.set("마지막 OBS 캡쳐 OCR 중")
        self._set_busy_ui(True)
        threading.Thread(
            target=self._worker_obs_existing_capture_ocr,
            args=(path, source_name, rects, ocr_cfg),
            daemon=True,
        ).start()

    def _snapshot_ocr_settings(self) -> dict[str, object]:
        return {
            "provider": self.ocr_provider.get(),
            "language": self.ocr_language.get(),
            "timeout_ms": int(self.ocr_timeout.get()),
            "min_confidence": float(self.ocr_min_confidence.get()),
            "preprocessing_preset": self.ocr_preprocessing_preset.get(),
            "obs_name_band_enabled": bool(self.config.section("ocr").get("obs_name_band_enabled", False)),
            "obs_name_band_top_ratio": float(self.config.section("ocr").get("obs_name_band_top_ratio", 0.46)),
            "obs_name_band_height_ratio": float(self.config.section("ocr").get("obs_name_band_height_ratio", 0.52)),
        }

    def _snapshot_ocr_rects(self) -> list[dict[str, int]]:
        try:
            rects = self._roi_slot_rect_payload()
            if len(rects) == 4:
                return apply_name_band_to_dicts(rects, self._snapshot_ocr_settings())
        except Exception:
            pass
        rects = self._ordered_obs_rects_from_list(self.config.section("obs_websocket").get("browser_source_rects", []))
        return apply_name_band_to_dicts(rects, self._snapshot_ocr_settings()) if len(rects) == 4 else rects

    def _ordered_obs_rects_from_list(self, value: object) -> list[dict[str, int]]:
        if not isinstance(value, list) or len(value) != 4:
            return []
        rects: list[dict[str, int]] = []
        for row in value:
            if not isinstance(row, dict):
                return []
            try:
                rects.append(
                    {
                        "x": int(row["x"]),
                        "y": int(row["y"]),
                        "w": int(row["w"]),
                        "h": int(row["h"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                return []
        return rects

    def _worker_obs_source_capture(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float,
        source_name: str,
        run_ocr: bool,
        rects: list[dict[str, int]],
        ocr_cfg: dict[str, object],
    ) -> None:
        artifact = self._manual_artifact_writer()
        result = capture_obs_source_screenshot(
            host,
            port,
            password,
            source_name,
            timeout,
            image_format="jpg",
            image_compression_quality=100,
        )
        image_bytes = result.pop("image_bytes", b"")
        if result.get("status") != "captured" or not isinstance(image_bytes, bytes):
            result["stage"] = "obs_source_capture"
            self.events.put(("obs_source_capture", result))
            return
        capture_path = artifact.write_binary("obs_source.png", image_bytes)
        result["stage"] = "obs_source_capture"
        result["capture_path"] = capture_path
        result["debug_dir"] = str(artifact.run_dir)
        if not run_ocr:
            self.events.put(("obs_source_capture", result))
            return
        try:
            payload = self._ocr_capture_file(capture_path, source_name, rects, ocr_cfg, artifact)
            payload["capture"] = result
        except Exception as exc:
            payload = {
                "stage": "obs_source_ocr",
                "status": "failed",
                "source_name": source_name,
                "capture_path": capture_path,
                "error": str(exc),
            }
        self.events.put(("obs_source_ocr", payload))

    def _worker_obs_existing_capture_ocr(
        self,
        path: str,
        source_name: str,
        rects: list[dict[str, int]],
        ocr_cfg: dict[str, object],
    ) -> None:
        artifact = self._manual_artifact_writer()
        try:
            payload = self._ocr_capture_file(path, source_name, rects, ocr_cfg, artifact)
        except Exception as exc:
            payload = {
                "stage": "obs_source_ocr",
                "status": "failed",
                "source_name": source_name,
                "capture_path": path,
                "error": str(exc),
            }
        self.events.put(("obs_source_ocr", payload))

    def _ocr_capture_file(
        self,
        path: str,
        source_name: str,
        rects: list[dict[str, int]],
        ocr_cfg: dict[str, object],
        artifact: ArtifactWriter,
    ) -> dict[str, object]:
        try:
            from PIL import Image
        except Exception as exc:
            raise RuntimeError("OBS 소스 OCR에는 Pillow가 필요함") from exc
        image = Image.open(path).convert("RGB")
        validate_image_dimensions(image.width, image.height, "OBS source capture")
        frame = CaptureFrame(
            source=f"obs_source:{source_name}",
            path=path,
            width=int(image.width),
            height=int(image.height),
            image=image,
        )
        slot_rects = [Rect(int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"])) for row in rects]
        if len(slot_rects) != 4:
            raise RuntimeError("OCR 대상 좌표가 4개가 아님")
        validation = validate_rects_in_bounds(slot_rects, frame.width, frame.height)
        if not validation.ok:
            raise RuntimeError("; ".join(validation.errors))
        timeout_ms = int(ocr_cfg.get("timeout_ms", 1000))
        provider = build_ocr_provider(
            str(ocr_cfg.get("provider", "paddleocr_v5")),
            str(ocr_cfg.get("language", "kor+eng")),
            timeout_ms,
            str(ocr_cfg.get("preprocessing_preset", "default-korean-ui")),
        )
        ocr = RewardScreenOcr(provider, timeout_ms).read_rewards(frame, slot_rects)
        for slot in ocr:
            slot.crop_path = artifact.write_crop(f"slot_{slot.slot_index}.png", frame.image, slot.rect)
        raw_ocr = "\n".join(f"{slot.slot_index}: {slot.raw_text}" for slot in ocr)
        debug_paths = {
            "ocr": artifact.write_json("ocr.json", [asdict(slot) for slot in ocr]),
            "raw_ocr": artifact.write_text("raw_ocr.txt", raw_ocr),
        }
        return {
            "stage": "obs_source_ocr",
            "status": "ready",
            "source_name": source_name,
            "capture_path": path,
            "image_size": {"w": frame.width, "h": frame.height},
            "rects": [rect.to_dict() for rect in slot_rects],
            "ocr": [asdict(slot) for slot in ocr],
            "raw_ocr": raw_ocr,
            "debug_paths": debug_paths,
        }

    def _build_obs_output_test_rows(self, ocr_rows: object, data_cfg: dict[str, object]) -> tuple[list[dict[str, object]], dict[str, object]]:
        item_wiki_dir = resolve_project_path(str(data_cfg.get("item_wiki_dir", "data/item_wiki") or "data/item_wiki"))
        index_payload = self._load_item_wiki_index(item_wiki_dir)
        rows: list[dict[str, object]] = []
        price_items: list[ItemRecord] = []
        if not isinstance(ocr_rows, list):
            ocr_rows = []
        for slot_index in range(1, 5):
            ocr_row = next((row for row in ocr_rows if isinstance(row, dict) and int(row.get("slot_index", 0) or 0) == slot_index), {})
            raw_text = str(ocr_row.get("raw_text", "")) if isinstance(ocr_row, dict) else ""
            match = self._match_item_wiki_text(raw_text, item_wiki_dir, index_payload)
            entry = match.get("entry") if isinstance(match, dict) else None
            if isinstance(entry, dict) and str(entry.get("slug", "")) != "forma_blueprint":
                price_items.append(self._item_record_from_wiki_entry(entry))
            rows.append(
                {
                    "slot_index": slot_index,
                    "raw_ocr": raw_text,
                    "match": match,
                    "entry": entry if isinstance(entry, dict) else None,
                }
            )
        price_payload: dict[str, object] = {"status": "skipped", "price_by_item": {}, "error": ""}
        if price_items and bool(data_cfg.get("market_live_enabled", True)):
            statuses_raw = data_cfg.get("market_order_statuses", ["ingame"])
            statuses = tuple(str(value).lower() for value in statuses_raw) if isinstance(statuses_raw, list) else ("ingame",)
            try:
                price_payload = fetch_market_prices_for_items(
                    price_items,
                    str(data_cfg.get("price_db_path", "")),
                    platform=str(data_cfg.get("platform", "pc")),
                    language=str(data_cfg.get("market_language", "ko")),
                    crossplay=bool(data_cfg.get("market_crossplay", True)),
                    statuses=statuses,
                    timeout=max(0.3, int(data_cfg.get("market_live_timeout_ms", 1500)) / 1000),
                    max_workers=4,
                    use_today_cache=bool(data_cfg.get("market_cache_same_day_only", True)),
                )
            except Exception as exc:
                price_payload = {"status": "failed", "price_by_item": {}, "error": str(exc)}
        price_by_item = price_payload.get("price_by_item", {})
        price_by_item = price_by_item if isinstance(price_by_item, dict) else {}
        for row in rows:
            entry = row.get("entry")
            price = price_by_item.get(str(entry.get("slug", ""))) if isinstance(entry, dict) else None
            row["matched"] = isinstance(entry, dict)
            row["text"] = self._obs_output_test_text(entry if isinstance(entry, dict) else None, price, str(row.get("raw_ocr", "")))
            row.pop("entry", None)
        serializable_price_payload = {key: value for key, value in price_payload.items() if key != "price_by_item"}
        serializable_price_payload["prices"] = [asdict(price) for price in price_by_item.values()]
        return rows, serializable_price_payload

    def _load_item_wiki_index(self, item_wiki_dir: Path) -> dict[str, object]:
        index_path = item_wiki_dir / "_index.json"
        if not index_path.exists():
            raise RuntimeError(f"item_wiki 인덱스 없음: {index_path}")
        if index_path.stat().st_size > MAX_ITEM_WIKI_INDEX_BYTES:
            raise RuntimeError(f"item_wiki 인덱스가 너무 큼: {index_path}")
        payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise RuntimeError("item_wiki 인덱스 형식이 올바르지 않음")
        return payload

    def _match_item_wiki_text(self, raw_text: str, item_wiki_dir: Path, index_payload: dict[str, object]) -> dict[str, object]:
        lookups = [
            ("exact_ko", index_payload.get("by_ko", {})),
            ("exact_en", index_payload.get("by_en", {})),
            ("exact_slug", index_payload.get("by_slug", {})),
        ]
        candidates = self._ocr_lookup_candidates(raw_text)
        for key in candidates:
            for method, mapping in lookups:
                if not isinstance(mapping, dict):
                    continue
                filename = mapping.get(key)
                if isinstance(filename, str):
                    entry = self._load_item_wiki_entry(item_wiki_dir, filename)
                    return {"method": method, "score": 1.0, "lookup_key": key, "filename": filename, "entry": entry}
        fuzzy_sources = [
            ("fuzzy_ko", index_payload.get("by_ko", {})),
            ("fuzzy_en", index_payload.get("by_en", {})),
        ]
        for key in candidates:
            if len(key) < 4:
                continue
            for method, mapping in fuzzy_sources:
                if not isinstance(mapping, dict) or not mapping:
                    continue
                match = difflib.get_close_matches(key, list(mapping.keys()), n=1, cutoff=0.80)
                if not match:
                    continue
                filename = mapping.get(match[0])
                if isinstance(filename, str):
                    entry = self._load_item_wiki_entry(item_wiki_dir, filename)
                    return {"method": method, "score": round(difflib.SequenceMatcher(None, key, match[0]).ratio(), 3), "lookup_key": match[0], "filename": filename, "entry": entry}
        return {"method": "unmatched", "score": 0.0, "lookup_key": "", "filename": "", "entry": None}

    def _load_item_wiki_entry(self, item_wiki_dir: Path, filename: str) -> dict[str, object]:
        path = _safe_item_wiki_entry_path(item_wiki_dir, filename)
        if path is None:
            raise RuntimeError(f"item_wiki 항목 경로가 폴더 밖임: {filename}")
        if not path.exists():
            raise RuntimeError(f"item_wiki 항목 없음: {path}")
        if path.stat().st_size > MAX_ITEM_WIKI_ENTRY_BYTES:
            raise RuntimeError(f"item_wiki 항목이 너무 큼: {path}")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"item_wiki 항목 형식 오류: {path}")
        return payload

    def _ocr_lookup_candidates(self, raw_text: str) -> list[str]:
        raw_values = [raw_text, raw_text.replace("\n", " ")]
        raw_values.extend(line for line in raw_text.splitlines() if line.strip())
        candidates: list[str] = []
        seen: set[str] = set()
        for value in raw_values:
            for cleaned in self._strip_reward_quantity(value):
                normalized = normalize_text(cleaned)
                for candidate in [normalized, normalized.replace(" ", "_"), normalized.replace("_", " ")]:
                    if candidate and candidate not in seen:
                        seen.add(candidate)
                        candidates.append(candidate)
        return candidates

    def _strip_reward_quantity(self, value: str) -> list[str]:
        stripped = value.strip()
        variants = [stripped]
        without_prefix = re.sub(r"^\s*\d+\s*[xX×]\s*", "", stripped).strip()
        if without_prefix and without_prefix not in variants:
            variants.append(without_prefix)
        without_leading_number = re.sub(r"^\s*\d+\s+", "", stripped).strip()
        if without_leading_number and without_leading_number not in variants:
            variants.append(without_leading_number)
        without_leading_noise = re.sub(r"^[^0-9A-Za-z가-힣]+", "", stripped).strip()
        if without_leading_noise and without_leading_noise not in variants:
            variants.append(without_leading_noise)
        return variants

    def _item_record_from_wiki_entry(self, entry: dict[str, object]) -> ItemRecord:
        slug = str(entry.get("slug", ""))
        aliases_raw = entry.get("aliases", [])
        aliases = [str(value) for value in aliases_raw] if isinstance(aliases_raw, list) else []
        try:
            ducats = int(entry.get("ducats", 0) or 0)
        except (TypeError, ValueError):
            ducats = 0
        return ItemRecord(
            id=slug,
            ko_name=str(entry.get("name_kr", "")),
            en_name=str(entry.get("name_en", "")),
            aliases=aliases,
            item_type="part",
            rarity=", ".join(str(value) for value in entry.get("rarities", [])) if isinstance(entry.get("rarities", []), list) else "",
            ducats=ducats,
            market_slug=slug,
            vaulted=False,
            tradable=slug != "forma_blueprint",
        )

    def _on_market_search_keyrelease(self, event) -> None:
        if getattr(event, "keysym", "") in {"Return", "Escape", "Up", "Down", "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"}:
            return
        self._schedule_market_autocomplete()

    def _on_market_search_return(self, _event=None):
        if self._market_autocomplete_is_visible() and self._accept_market_autocomplete(run_search=True):
            return "break"
        self._run_market_search_async()
        return "break"

    def _on_market_autocomplete_click(self, _event=None):
        self._accept_market_autocomplete(run_search=False)
        return "break"

    def _on_market_autocomplete_list_return(self, _event=None):
        self._accept_market_autocomplete(run_search=True)
        return "break"

    def _on_market_autocomplete_escape(self, _event=None):
        self._hide_market_autocomplete()
        return "break"

    def _on_market_autocomplete_down(self, _event=None):
        if not self._market_autocomplete_is_visible():
            self._refresh_market_autocomplete()
        self._move_market_autocomplete_selection(1)
        return "break"

    def _on_market_autocomplete_up(self, _event=None):
        if self._market_autocomplete_is_visible():
            self._move_market_autocomplete_selection(-1)
        return "break"

    def _schedule_market_autocomplete(self) -> None:
        if self.market_autocomplete_after_id is not None:
            try:
                self.root.after_cancel(self.market_autocomplete_after_id)
            except tk.TclError:
                pass
            self.market_autocomplete_after_id = None
        if len(self.market_search_query.get().strip()) < 1:
            self._hide_market_autocomplete()
            return
        self.market_autocomplete_after_id = self.root.after(300, self._refresh_market_autocomplete)

    def _refresh_market_autocomplete(self) -> None:
        self.market_autocomplete_after_id = None
        query = self.market_search_query.get().strip()
        if not query:
            self._hide_market_autocomplete()
            return
        suggestions = self._market_autocomplete_suggestions(query, limit=8)
        if not suggestions:
            self._hide_market_autocomplete()
            return
        self.market_autocomplete_visible = suggestions
        self.market_autocomplete_list.configure(height=min(8, max(1, len(suggestions))))
        self.market_autocomplete_list.delete(0, "end")
        for suggestion in suggestions:
            self.market_autocomplete_list.insert("end", str(suggestion.get("display", "")))
        self.market_autocomplete_list.selection_clear(0, "end")
        self.market_autocomplete_list.selection_set(0)
        self.market_autocomplete_list.activate(0)
        self.market_autocomplete_list.grid()

    def _hide_market_autocomplete(self) -> None:
        if self.market_autocomplete_after_id is not None:
            try:
                self.root.after_cancel(self.market_autocomplete_after_id)
            except tk.TclError:
                pass
            self.market_autocomplete_after_id = None
        self.market_autocomplete_visible = []
        if hasattr(self, "market_autocomplete_list"):
            self.market_autocomplete_list.grid_remove()

    def _market_autocomplete_is_visible(self) -> bool:
        return bool(getattr(self, "market_autocomplete_visible", [])) and bool(self.market_autocomplete_list.winfo_ismapped())

    def _move_market_autocomplete_selection(self, delta: int) -> None:
        if not self._market_autocomplete_is_visible():
            return
        size = self.market_autocomplete_list.size()
        if size <= 0:
            return
        current = self.market_autocomplete_list.curselection()
        index = current[0] if current else 0
        index = max(0, min(size - 1, index + delta))
        self.market_autocomplete_list.selection_clear(0, "end")
        self.market_autocomplete_list.selection_set(index)
        self.market_autocomplete_list.activate(index)
        self.market_autocomplete_list.see(index)

    def _accept_market_autocomplete(self, run_search: bool) -> bool:
        if not self._market_autocomplete_is_visible():
            return False
        selection = self.market_autocomplete_list.curselection()
        index = selection[0] if selection else 0
        if index < 0 or index >= len(self.market_autocomplete_visible):
            return False
        suggestion = self.market_autocomplete_visible[index]
        fill = str(suggestion.get("fill") or suggestion.get("display") or "").strip()
        if not fill:
            return False
        self.market_autocomplete_selecting = True
        try:
            self.market_search_query.set(fill)
        finally:
            self.market_autocomplete_selecting = False
        self._hide_market_autocomplete()
        if run_search:
            self._run_market_search_async()
        else:
            self.market_search_entry.icursor("end")
            self.market_search_entry.focus_set()
        return True

    def _market_autocomplete_suggestions(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        q = self._market_autocomplete_key(query)
        if not q:
            return []
        q_compact = self._compact_autocomplete_key(q)
        q_initials = self._hangul_initials(q)
        ranked: list[tuple[float, int, str, dict[str, object]]] = []
        for candidate in self._market_autocomplete_candidate_cache():
            tokens = candidate.get("tokens", [])
            best = 0.0
            for token in tokens if isinstance(tokens, list) else []:
                best = max(best, self._market_autocomplete_score(q, q_compact, q_initials, str(token)))
            if best <= 0:
                continue
            source_bonus = 3 if candidate.get("source") == "item_wiki" else 0
            ranked.append((best + source_bonus, int(candidate.get("priority", 99)), str(candidate.get("display", "")), candidate))
        ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
        return [row[3] for row in ranked[:limit]]

    def _market_autocomplete_score(self, query: str, query_compact: str, query_initials: str, token: str) -> float:
        if not token:
            return 0.0
        token_compact = self._compact_autocomplete_key(token)
        token_initials = self._hangul_initials(token)
        if token == query:
            return 120.0
        if token.startswith(query):
            return 105.0 - min(len(token) - len(query), 40) * 0.1
        if token_compact.startswith(query_compact):
            return 98.0 - min(len(token_compact) - len(query_compact), 40) * 0.1
        if query in token:
            return 84.0 - min(token.find(query), 30) * 0.2
        if query_compact and query_compact in token_compact:
            return 78.0 - min(token_compact.find(query_compact), 30) * 0.2
        if query_initials and token_initials.startswith(query_initials):
            return 72.0
        if len(query) >= 3:
            ratio = difflib.SequenceMatcher(None, query, token).ratio()
            if ratio >= 0.72:
                return 48.0 + ratio * 20.0
        return 0.0

    def _market_autocomplete_candidate_cache(self) -> list[dict[str, object]]:
        item_dir = resolve_project_path(self.item_wiki_dir.get().strip() or "data/item_wiki")
        market_dir = resolve_project_path(self.market_wiki_dir.get().strip() or "data/market_wiki")
        item_index = item_dir / "_index.json"
        market_index = market_dir / "_index.json"
        cache_key = (
            str(item_index),
            item_index.stat().st_mtime if item_index.exists() else -1,
            str(market_index),
            market_index.stat().st_mtime if market_index.exists() else -1,
        )
        if self.market_autocomplete_cache_key == cache_key:
            return self.market_autocomplete_candidates
        records: dict[str, dict[str, object]] = {}
        self._collect_market_autocomplete_records(records, item_dir, "item_wiki", 0)
        self._collect_market_autocomplete_records(records, market_dir, "market_wiki", 1)
        candidates = [self._finalize_market_autocomplete_record(record) for record in records.values()]
        self.market_autocomplete_cache_key = cache_key
        self.market_autocomplete_candidates = [candidate for candidate in candidates if candidate]
        return self.market_autocomplete_candidates

    def _collect_market_autocomplete_records(self, records: dict[str, dict[str, object]], wiki_dir: Path, source: str, priority: int) -> None:
        index_path = wiki_dir / "_index.json"
        if not index_path.exists():
            return
        try:
            if index_path.stat().st_size > MAX_ITEM_WIKI_INDEX_BYTES:
                return
            payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        for bucket_name, label_key in (("by_ko", "ko"), ("by_en", "en"), ("by_slug", "slug_labels")):
            mapping = payload.get(bucket_name, {})
            if not isinstance(mapping, dict):
                continue
            for raw_label, filename in mapping.items():
                if not isinstance(filename, str):
                    continue
                slug = Path(filename).stem
                if not slug:
                    continue
                existing = records.get(slug)
                if existing is None or int(existing.get("priority", 99)) > priority:
                    existing = {
                        "source": source,
                        "priority": priority,
                        "slug": slug,
                        "filename": filename,
                        "ko": [],
                        "en": [],
                        "slug_labels": [],
                    }
                    records[slug] = existing
                label = str(raw_label).strip()
                if not label:
                    continue
                values = existing.get(label_key, [])
                if isinstance(values, list) and label not in values:
                    values.append(label)

    def _finalize_market_autocomplete_record(self, record: dict[str, object]) -> dict[str, object] | None:
        ko_labels = self._sorted_autocomplete_labels(record.get("ko", []))
        en_labels = self._sorted_autocomplete_labels(record.get("en", []))
        slug_labels = self._sorted_autocomplete_labels(record.get("slug_labels", []))
        primary = (ko_labels or en_labels or slug_labels or [str(record.get("slug", ""))])[0]
        secondary = ""
        if ko_labels and en_labels:
            secondary = en_labels[0]
        elif slug_labels and slug_labels[0] != primary:
            secondary = slug_labels[0]
        source_text = "두캇 DB" if record.get("source") == "item_wiki" else "마켓 Wiki"
        display = f"{primary} / {secondary} [{source_text}]" if secondary else f"{primary} [{source_text}]"
        tokens: set[str] = set()
        for label in ko_labels + en_labels + slug_labels + [str(record.get("slug", ""))]:
            key = self._market_autocomplete_key(label)
            if not key:
                continue
            tokens.add(key)
            tokens.add(key.replace("_", " "))
            tokens.add(self._compact_autocomplete_key(key))
            initials = self._hangul_initials(key)
            if initials:
                tokens.add(initials)
        if not tokens:
            return None
        return {
            "display": display,
            "fill": primary,
            "source": record.get("source", ""),
            "priority": record.get("priority", 99),
            "slug": record.get("slug", ""),
            "tokens": sorted(tokens),
        }

    def _sorted_autocomplete_labels(self, values: object) -> list[str]:
        if not isinstance(values, list):
            return []
        return sorted({str(value).strip() for value in values if str(value).strip()}, key=lambda value: (-len(value), value))

    def _market_autocomplete_key(self, value: str) -> str:
        return re.sub(r"\s+", " ", normalize_text(str(value).replace("_", " "))).strip()

    def _compact_autocomplete_key(self, value: str) -> str:
        return re.sub(r"[\s_]+", "", value)

    def _hangul_initials(self, value: str) -> str:
        initials = []
        choseong = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
        for char in value:
            code = ord(char)
            if 0xAC00 <= code <= 0xD7A3:
                initials.append(choseong[(code - 0xAC00) // 588])
            elif "ㄱ" <= char <= "ㅎ":
                initials.append(char)
        return "".join(initials)

    def _run_market_search_async(self) -> None:
        query = self.market_search_query.get().strip()
        if not query:
            self.market_search_status.set("검색어를 입력하세요.")
            return
        self._hide_market_autocomplete()
        self.market_search_status.set("마켓 검색 중...")
        self._set_market_search_text("검색 중...")
        threading.Thread(
            target=self._worker_market_search,
            args=(query, self.market_search_rank_mode.get(), self.market_search_rank_custom.get()),
            daemon=True,
        ).start()

    def _worker_market_search(self, query: str, rank_mode: str, rank_custom: str) -> None:
        try:
            match = self._find_market_search_item(query)
            entry = match.get("entry")
            if not isinstance(entry, dict):
                self.events.put(("market_search", {"status": "FAIL", "query": query, "error": "일치하는 마켓 아이템을 찾지 못했습니다."}))
                return
            slug = str(entry.get("market_slug") or entry.get("slug") or "").strip()
            if not slug:
                self.events.put(("market_search", {"status": "FAIL", "query": query, "error": "매칭 항목에 market_slug가 없습니다.", "match": match}))
                return
            rank_info = self._market_search_rank_info(entry, rank_mode, rank_custom)
            price = self._fetch_market_search_price(slug, rank_info.get("target_rank"))
            item_wiki_update = self._update_item_wiki_market_price(match, price)
            self.events.put(
                (
                    "market_search",
                    {
                        "status": "PASS" if price.get("order_count", 0) else "FAIL",
                        "query": query,
                        "match": match,
                        "rank": rank_info,
                        "price": price,
                        "item_wiki_update": item_wiki_update,
                    },
                )
            )
        except Exception as exc:
            self.events.put(("market_search", {"status": "FAIL", "query": query, "error": str(exc)}))

    def _find_market_search_item(self, query: str) -> dict[str, object]:
        item_match: dict[str, object] = {"method": "unmatched", "score": 0.0, "entry": None}
        try:
            item_wiki_dir = resolve_project_path(self.item_wiki_dir.get().strip() or "data/item_wiki")
            item_index = self._load_item_wiki_index(item_wiki_dir)
            item_match = self._match_item_wiki_text(query, item_wiki_dir, item_index)
        except Exception:
            item_match = {"method": "item_wiki_error", "score": 0.0, "entry": None}

        market_wiki_dir = resolve_project_path(str(self.config.section("data").get("market_wiki_dir", "data/market_wiki") or "data/market_wiki"))
        market_index = self._load_market_wiki_index(market_wiki_dir)
        item_entry = item_match.get("entry")
        if isinstance(item_entry, dict):
            slug = str(item_entry.get("market_slug") or item_entry.get("slug") or "").strip()
            market_entry = self._load_market_wiki_entry_by_slug(market_wiki_dir, market_index, slug)
            if market_entry:
                return {
                    "source": "item_wiki+market_wiki",
                    "method": item_match.get("method", "item_wiki"),
                    "score": item_match.get("score", 0.0),
                    "lookup_key": item_match.get("lookup_key", ""),
                    "entry": market_entry,
                    "item_wiki_entry": item_entry,
                    "item_wiki_filename": item_match.get("filename", ""),
                }
            return {
                "source": "item_wiki",
                "method": item_match.get("method", "item_wiki"),
                "score": item_match.get("score", 0.0),
                "lookup_key": item_match.get("lookup_key", ""),
                "entry": item_entry,
                "item_wiki_entry": item_entry,
                "item_wiki_filename": item_match.get("filename", ""),
            }
        market_match = self._match_market_wiki_text(query, market_wiki_dir, market_index)
        market_match["source"] = "market_wiki"
        return market_match

    def _load_market_wiki_index(self, market_wiki_dir: Path) -> dict[str, object]:
        index_path = market_wiki_dir / "_index.json"
        if not index_path.exists():
            raise RuntimeError(f"market_wiki 인덱스 없음: {index_path}")
        if index_path.stat().st_size > MAX_ITEM_WIKI_INDEX_BYTES:
            raise RuntimeError(f"market_wiki 인덱스가 너무 큼: {index_path}")
        payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise RuntimeError("market_wiki 인덱스 형식이 올바르지 않음")
        return payload

    def _match_market_wiki_text(self, raw_text: str, market_wiki_dir: Path, index_payload: dict[str, object]) -> dict[str, object]:
        lookups = [
            ("exact_ko", index_payload.get("by_ko", {})),
            ("exact_en", index_payload.get("by_en", {})),
            ("exact_slug", index_payload.get("by_slug", {})),
        ]
        candidates = self._ocr_lookup_candidates(raw_text)
        for key in candidates:
            for method, mapping in lookups:
                if not isinstance(mapping, dict):
                    continue
                filename = mapping.get(key)
                if isinstance(filename, str):
                    return {"method": method, "score": 1.0, "lookup_key": key, "filename": filename, "entry": self._load_market_wiki_entry(market_wiki_dir, filename)}
        fuzzy_sources = [
            ("fuzzy_ko", index_payload.get("by_ko", {})),
            ("fuzzy_en", index_payload.get("by_en", {})),
        ]
        for key in candidates:
            if len(key) < 4:
                continue
            for method, mapping in fuzzy_sources:
                if not isinstance(mapping, dict) or not mapping:
                    continue
                match = difflib.get_close_matches(key, list(mapping.keys()), n=1, cutoff=0.78)
                if not match:
                    continue
                filename = mapping.get(match[0])
                if isinstance(filename, str):
                    return {
                        "method": method,
                        "score": round(difflib.SequenceMatcher(None, key, match[0]).ratio(), 3),
                        "lookup_key": match[0],
                        "filename": filename,
                        "entry": self._load_market_wiki_entry(market_wiki_dir, filename),
                    }
        return {"method": "unmatched", "score": 0.0, "lookup_key": "", "filename": "", "entry": None}

    def _load_market_wiki_entry_by_slug(self, market_wiki_dir: Path, index_payload: dict[str, object], slug: str) -> dict[str, object] | None:
        if not slug:
            return None
        files = index_payload.get("files", {})
        filename = files.get(slug) if isinstance(files, dict) else None
        if not isinstance(filename, str):
            filename = f"{slug}.json"
        try:
            return self._load_market_wiki_entry(market_wiki_dir, filename)
        except Exception:
            return None

    def _load_market_wiki_entry(self, market_wiki_dir: Path, filename: str) -> dict[str, object]:
        path = _safe_item_wiki_entry_path(market_wiki_dir, filename)
        if path is None:
            raise RuntimeError(f"market_wiki 항목 경로가 폴더 밖임: {filename}")
        if not path.exists():
            raise RuntimeError(f"market_wiki 항목 없음: {path}")
        if path.stat().st_size > MAX_ITEM_WIKI_ENTRY_BYTES:
            raise RuntimeError(f"market_wiki 항목이 너무 큼: {path}")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"market_wiki 항목 형식 오류: {path}")
        return payload

    def _market_search_rank_info(self, entry: dict[str, object], rank_mode: str, rank_custom: str) -> dict[str, object]:
        max_rank = self._coerce_optional_int(entry.get("max_rank"))
        tags = entry.get("tags", [])
        ranked = max_rank is not None and max_rank > 0
        target_rank: int | None = None
        label = "랭크 없음"
        if ranked:
            if rank_mode == "최대 랭크":
                target_rank = max_rank
                label = f"최대 랭크 {max_rank}"
            elif rank_mode == "직접 입력":
                try:
                    custom_rank = int(rank_custom)
                except (TypeError, ValueError):
                    custom_rank = 0
                target_rank = max(0, min(max_rank, custom_rank))
                label = f"직접 랭크 {target_rank}"
            elif rank_mode == "전체 랭크":
                target_rank = None
                label = "전체 랭크"
            else:
                target_rank = 0
                label = "0랭크"
        return {
            "ranked": ranked,
            "max_rank": max_rank,
            "target_rank": target_rank,
            "label": label,
            "mode": rank_mode,
            "tags": tags if isinstance(tags, list) else [],
        }

    def _fetch_market_search_price(self, slug: str, target_rank: object) -> dict[str, object]:
        data_cfg = self.config.section("data")
        client = WarframeMarketClient(
            platform=str(data_cfg.get("platform", "pc")),
            language=str(data_cfg.get("market_language", "ko")),
            crossplay=bool(data_cfg.get("market_crossplay", True)),
            timeout=max(0.5, int(data_cfg.get("market_live_timeout_ms", 1500)) / 1000),
        )
        payload = client.top_orders(slug)
        orders = self._market_search_orders_from_payload(payload, target_rank)
        source = "warframe_market_v2_top"
        if target_rank is not None and not orders:
            original_timeout = client.timeout
            client.timeout = max(original_timeout, 5.0)
            try:
                full_payload = client.orders(slug)
                orders = self._market_search_orders_from_payload(full_payload, target_rank)
            finally:
                client.timeout = original_timeout
            source = "warframe_market_v2_orders"
        orders.sort(key=lambda row: float(row["platinum"]))
        return {
            "slug": slug,
            "status_filter": "ingame",
            "rank_filter": target_rank,
            "order_count": len(orders),
            "lowest_plat": orders[0]["platinum"] if orders else None,
            "orders": orders[:5],
            "source": source,
        }

    def _market_search_orders_from_payload(self, payload: dict[str, object], target_rank: object) -> list[dict[str, object]]:
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            sells = data.get("sell", [])
        elif isinstance(data, list):
            sells = data
        else:
            sells = []
        orders: list[dict[str, object]] = []
        for order in sells if isinstance(sells, list) else []:
            if not isinstance(order, dict):
                continue
            user = order.get("user", {})
            user_status = str(user.get("status", "")).lower() if isinstance(user, dict) else ""
            if user_status != "ingame":
                continue
            if str(order.get("type", "")).lower() != "sell" or order.get("visible") is False:
                continue
            order_rank = self._coerce_optional_int(order.get("rank"))
            if target_rank is not None and order_rank != int(target_rank):
                continue
            try:
                platinum = float(order["platinum"])
            except (KeyError, TypeError, ValueError):
                continue
            orders.append(
                {
                    "platinum": platinum,
                    "rank": order_rank,
                    "quantity": self._coerce_optional_int(order.get("quantity")),
                    "per_trade": self._coerce_optional_int(order.get("perTrade")),
                    "updated_at": str(order.get("updatedAt", "")),
                }
            )
        return orders

    def _update_item_wiki_market_price(self, match: object, price: dict[str, object]) -> dict[str, object]:
        if not isinstance(match, dict):
            return {"updated": False, "reason": "no_match"}
        item_entry = match.get("item_wiki_entry")
        if not isinstance(item_entry, dict):
            return {"updated": False, "reason": "not_item_wiki"}
        if self._coerce_optional_int(item_entry.get("ducats")) is None:
            return {"updated": False, "reason": "no_ducats"}
        lowest = price.get("lowest_plat") if isinstance(price, dict) else None
        if lowest is None:
            return {"updated": False, "reason": "no_price"}
        filename = str(match.get("item_wiki_filename", "")).strip()
        if not filename:
            return {"updated": False, "reason": "no_filename"}
        try:
            item_wiki_dir = resolve_project_path(self.item_wiki_dir.get().strip() or "data/item_wiki")
            path = _safe_item_wiki_entry_path(item_wiki_dir, filename)
            if path is None or not path.exists():
                return {"updated": False, "reason": "bad_path"}
            if path.stat().st_size > MAX_ITEM_WIKI_ENTRY_BYTES:
                return {"updated": False, "reason": "too_large"}
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                return {"updated": False, "reason": "bad_json"}
            plat_value = int(lowest) if isinstance(lowest, int) or (isinstance(lowest, float) and lowest.is_integer()) else float(lowest)
            today = time.strftime("%m_%d_%y")
            payload["plat"] = plat_value
            payload["plat_display"] = f"{self._format_number(plat_value)} p"
            payload["plat_date"] = today
            payload["plat_status"] = str(price.get("status_filter", "ingame"))
            payload["plat_order_count"] = int(price.get("order_count", 0) or 0)
            payload["plat_source"] = "warframe_market_v2_orders"
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(path)
            return {"updated": True, "filename": filename, "plat": plat_value, "plat_date": today}
        except Exception as exc:
            return {"updated": False, "reason": str(exc)}

    def _show_market_search_result(self, payload) -> None:
        if not isinstance(payload, dict):
            self.market_search_status.set("마켓 검색 실패")
            self._set_market_search_text(str(payload))
            return
        query = str(payload.get("query", ""))
        if payload.get("status") != "PASS":
            error = str(payload.get("error", "게임중 판매 주문을 찾지 못했습니다."))
            self.market_search_status.set(f"검색 실패: {error}")
            self._set_market_search_text(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        match = payload.get("match", {})
        entry = match.get("entry", {}) if isinstance(match, dict) else {}
        rank = payload.get("rank", {}) if isinstance(payload.get("rank", {}), dict) else {}
        price = payload.get("price", {}) if isinstance(payload.get("price", {}), dict) else {}
        name_kr = str(entry.get("name_kr", "")) if isinstance(entry, dict) else ""
        name_en = str(entry.get("name_en", "")) if isinstance(entry, dict) else ""
        slug = str(entry.get("market_slug") or entry.get("slug") or "") if isinstance(entry, dict) else ""
        lowest = price.get("lowest_plat")
        lowest_text = f"{self._format_number(lowest)} p" if lowest is not None else "-"
        order_count = int(price.get("order_count", 0) or 0)
        tags = entry.get("tags", []) if isinstance(entry, dict) else []
        tag_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else "-"
        lines = [
            f"매칭: {name_kr or name_en} / {name_en}",
            f"태그: {tag_text or '-'}",
            f"랭크: {rank.get('label', '-')} / maxRank: {rank.get('max_rank', '-')}",
            f"가격 기준: 게임중(ingame) 판매 주문",
            f"최저가: {lowest_text}",
            f"주문 수: {order_count}",
            "",
            "상위 주문:",
        ]
        for index, order in enumerate(price.get("orders", []) if isinstance(price.get("orders", []), list) else [], start=1):
            rank_text = "-" if order.get("rank") is None else str(order.get("rank"))
            quantity = "-" if order.get("quantity") is None else str(order.get("quantity"))
            per_trade = "-" if order.get("per_trade") is None else str(order.get("per_trade"))
            lines.append(
                f"{index}. {self._format_number(order.get('platinum'))} p / rank {rank_text} / 수량 {quantity} / perTrade {per_trade}"
            )
        lines.extend(
            [
                "",
                f"slug: {slug}",
                f"매칭 출처: {match.get('source', '-')} / {match.get('method', '-')} / score {match.get('score', '-')}",
            ]
        )
        item_wiki_update = payload.get("item_wiki_update")
        if isinstance(item_wiki_update, dict) and item_wiki_update.get("updated"):
            self.market_search_status.set(
                f"{name_kr or name_en}: {lowest_text} ({rank.get('label', '-')}, ingame {order_count}건, item_wiki 저장됨)"
            )
        else:
            self.market_search_status.set(f"{name_kr or name_en}: {lowest_text} ({rank.get('label', '-')}, ingame {order_count}건)")
        self._set_market_search_text("\n".join(lines))

    def _set_market_search_text(self, text: str) -> None:
        self.market_search_text.configure(state="normal")
        self.market_search_text.delete("1.0", "end")
        self.market_search_text.insert("1.0", text)
        self.market_search_text.configure(state="disabled")

    def _coerce_optional_int(self, value: object) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _obs_output_test_text(self, entry: dict[str, object] | None, price: object, raw_text: str) -> str:
        return format_item_wiki_reward_text(entry, price, raw_text)

    def _obs_entry_display_name(self, entry: dict[str, object]) -> str:
        return str(entry.get("name_kr") or entry.get("name_en") or entry.get("slug") or "unknown").replace("_", " ").strip()

    def _compact_ocr_label(self, raw_text: str) -> str:
        value = " ".join(line.strip() for line in raw_text.splitlines() if line.strip())
        return value[:40]

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "result":
                    self._show_result(payload)
                elif kind == "stage":
                    self._show_stage(payload)
                elif kind == "obs_websocket":
                    self._show_obs_websocket_result(payload)
                elif kind == "obs_source_rects":
                    self._show_obs_source_rects_result(payload)
                elif kind == "obs_text_outputs":
                    self._show_obs_text_outputs_result(payload)
                elif kind == "obs_output_test":
                    self._show_obs_output_test_result(payload)
                elif kind == "obs_source_capture":
                    self._show_obs_source_capture_result(payload)
                elif kind == "obs_source_ocr":
                    self._show_obs_source_ocr_result(payload)
                elif kind == "market_search":
                    self._show_market_search_result(payload)
                elif kind == "auto_detect":
                    self.auto_detect_busy = False
                    if isinstance(payload, dict):
                        self.last_detector_payload = payload
                        confidence = payload.get("confidence", 0.0)
                        self.status_var.set(f"자동 감지: {confidence:.2f}" if isinstance(confidence, float) else "자동 감지")
                        if payload.get("detected"):
                            self.last_auto_trigger_ms = int(time.monotonic() * 1000)
                            self._stage_log("DETECT", f"자동 감지됨: {payload.get('reason', '')}")
                            self._run_pipeline_async("auto")
                        else:
                            now = int(time.monotonic() * 1000)
                            if now - self.last_auto_idle_log_ms >= 2000:
                                self.last_auto_idle_log_ms = now
                                self._stage_log(
                                    "DETECT",
                                    f"자동 대기: 신뢰도={float(confidence or 0):.2f}; {payload.get('reason', '')}",
                                )
                    self._refresh_indicators()
                elif kind == "auto_detect_error":
                    self.auto_detect_busy = False
                    self._stage_log("DETECT", f"오류: 자동 감지 실패: {payload}")
                elif kind == "hotkey_trigger":
                    if self.controller.busy:
                        self._stage_log("HOTKEY", "단축키 무시됨: 파이프라인이 이미 실행 중", level="WARNING")
                        continue
                    self._run_pipeline_async(str(payload or "hotkey"))
                elif kind == "hotkey_busy":
                    self.last_hotkey_status = "busy"
                    self.hotkey_manager.status = "busy"
                    self._stage_log("HOTKEY", "단축키 무시됨: 파이프라인이 이미 실행 중", level="WARNING")
                elif kind == "log":
                    channel, message = payload if isinstance(payload, tuple) else ("LOG", str(payload))
                    self._stage_log(str(channel), str(message))
                else:
                    self._set_busy_ui(False)
                    self._stage_log("ERR", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _show_obs_websocket_result(self, payload) -> None:
        self._set_busy_ui(False)
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        if isinstance(payload, dict) and payload.get("status") == "connected":
            self.obs_connected = True
            self.last_obs_info = dict(payload)
            message = (
                f"OBS 연결됨: {payload.get('host')}:{payload.get('port')} / "
                f"현재 장면 {payload.get('current_program_scene_name', '-')}"
            )
            self.obs_status.set(message)
            self._stage_log("OBS", message, level="SUCCESS")
            self._refresh_home_dashboard()
            after_connect = str(payload.get("after_connect") or "")
            if after_connect in {"input", "setup"}:
                self.root.after(50, lambda: self._fetch_obs_source_rects_async("input"))
            return
        self.obs_connected = False
        self.obs_auto_setup_pending = False
        self.last_obs_info = {}
        error = payload.get("error", "원인 불명") if isinstance(payload, dict) else str(payload)
        message = f"OBS 연결 실패: {error}"
        self.obs_status.set(message)
        self._stage_log("OBS", message, level="ERROR")
        self._refresh_home_dashboard()

    def _show_obs_source_rects_result(self, payload) -> None:
        self._set_busy_ui(False)
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        if not isinstance(payload, dict) or payload.get("status") != "connected":
            self.obs_connected = False
            error = payload.get("error", "원인 불명") if isinstance(payload, dict) else str(payload)
            source_kind = str(payload.get("source_kind", "input")) if isinstance(payload, dict) else "input"
            if source_kind != "input":
                self.home_output_status.set("T1~T4는 좌표 확인 없이 텍스트 출력만 사용")
                self._stage_log("OBS", "T 출력소스는 좌표 확인을 건너뜀")
                self._refresh_home_dashboard()
                return
            self.obs_auto_setup_pending = False
            message = f"OBS 소스 좌표 확인 실패: {error}"
            if hasattr(self, "home_input_status"):
                self.home_input_status.set("B1~B4 좌표 확인 실패")
            self._stage_log("OBS", message, level="ERROR")
            self._refresh_home_dashboard()
            return
        self.obs_connected = True
        self.last_obs_info = dict(payload)
        source_kind = str(payload.get("source_kind", "input"))
        if source_kind != "input":
            self.home_output_status.set("T1~T4는 좌표 확인 없이 텍스트 출력만 사용")
            self._stage_log("OBS", "T 출력소스 좌표 결과는 사용하지 않음")
            self._refresh_home_dashboard()
            return
        names = [str(name) for name in payload.get("source_names", [])]
        rects = payload.get("rects", {})
        missing = [str(name) for name in payload.get("missing", [])]
        ordered = self._ordered_obs_rects(names, rects)
        if len(ordered) != 4 or missing:
            self.obs_auto_setup_pending = False
            message = f"OBS 소스 누락: {', '.join(missing) if missing else '좌표 4개를 만들 수 없음'}"
            if source_kind == "input":
                self.home_input_status.set(message)
            else:
                self.home_output_status.set(message)
            self._stage_log("OBS", message, level="ERROR")
            self._refresh_home_dashboard()
            return
        obs_cfg = self.config.section("obs_websocket")
        previous = obs_cfg.get("browser_source_rects", [])
        if not isinstance(previous, list) or len(previous) != 4:
            previous = self._roi_slot_rect_payload_or_empty()
        if previous and not self._same_rects(previous, ordered):
            self._notify_rect_change("OBS input", previous, ordered)
        for row_vars, rect in zip(self.roi_slot_rects, ordered):
            for key in ("x", "y", "w", "h"):
                row_vars[key].set(str(rect[key]))
        self.config.set_value("obs_websocket", "browser_source_rects", self._source_rect_config(names, ordered))
        self._apply_gui_to_runtime()
        try:
            self.config.save()
        except Exception as exc:
            self._stage_log("CONFIG", f"OBS B1~B4 좌표 자동 저장 실패: {exc}", level="ERROR")
        else:
            self._stage_log("CONFIG", "OBS B1~B4 좌표 자동 저장됨", level="SUCCESS")
        self.home_input_status.set(f"B1~B4 좌표 적용됨 / 장면: {payload.get('current_program_scene_name', '-')}")
        self._stage_log("OBS", "OBS input B1~B4 좌표를 입력 / 좌표에 적용함", level="SUCCESS")
        if self.obs_auto_setup_pending:
            self.obs_auto_setup_pending = False
            self.root.after(150, self._test_obs_outputs_async)
        self.gui_dirty = False
        self._refresh_profile_title()
        self._refresh_home_dashboard()

    def _show_obs_text_outputs_result(self, payload) -> None:
        if not isinstance(payload, dict):
            self._stage_log("OBS", f"OBS T 출력 결과 오류: {payload}", level="ERROR")
            return
        is_clear = bool(payload.get("is_clear", False))
        status = str(payload.get("status", "failed"))
        updated = [str(name) for name in payload.get("updated", [])]
        failed = payload.get("failed", {})
        if status in {"updated", "partial"}:
            self.obs_connected = True
            if is_clear:
                self.result_obs_text_output_active = False
            if is_clear:
                message = f"OBS T 출력 지움: {', '.join(updated) if updated else '-'}"
            else:
                self.result_obs_text_output_active = True
                message = f"OBS T 출력 갱신: {', '.join(updated) if updated else '-'}"
            if failed:
                message += f" / 실패 {', '.join(str(name) for name in failed)}"
            if int(payload.get("retry_count", 0) or 0) > 0:
                message += " / 재시도 1회"
            self.home_output_status.set(message)
            self._stage_log("OBS", message, level="SUCCESS" if status == "updated" else "WARNING")
            if not is_clear:
                source_names = [str(name) for name in payload.get("text_by_source", {}).keys()]
                self._schedule_obs_text_clear(source_names, int(payload.get("clear_after_ms", 0) or 0))
            self._refresh_home_dashboard()
            return
        error = str(payload.get("error", "원인 불명"))
        if is_clear:
            self.home_output_status.set("OBS T 출력 지움 실패")
            self._stage_log("OBS", f"OBS T 출력 지움 실패: {error}", level="ERROR")
        else:
            self.result_obs_text_output_active = False
            self.home_output_status.set("OBS T 출력 실패")
            self._stage_log("OBS", f"OBS T 출력 실패: {error}", level="ERROR")

    def _show_obs_output_test_result(self, payload) -> None:
        self._set_busy_ui(False)
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        if not isinstance(payload, dict):
            self.home_output_status.set("출력테스트 실패")
            self._stage_log("OBS", f"출력테스트 결과 오류: {payload}", level="ERROR")
            return
        status = str(payload.get("status", "FAIL"))
        final_update = payload.get("final_update", {})
        updated = [str(name) for name in final_update.get("updated", [])] if isinstance(final_update, dict) else []
        failed = final_update.get("failed", {}) if isinstance(final_update, dict) else {}
        recognized = int(payload.get("recognized_count", 0) or 0)
        if status == "PASS":
            self.obs_connected = True
            self.result_obs_text_output_active = True
            message = f"출력테스트 완료: 인식 {recognized}/4, 갱신 {len(updated)}/4"
            if failed:
                message += f", 실패 {len(failed)}"
            self.home_output_status.set(message)
            self.status_var.set(message)
            self._stage_log("OBS", message, level="SUCCESS" if not failed else "WARNING")
            final_text = payload.get("final_text_by_source", {})
            source_names = [str(name) for name in final_text.keys()] if isinstance(final_text, dict) else updated
            self._schedule_obs_text_clear(source_names, self._obs_text_clear_after_ms())
            self._refresh_home_dashboard()
            return
        error = str(payload.get("error", "원인 불명"))
        self.result_obs_text_output_active = False
        message = f"출력테스트 실패: {error}"
        self.home_output_status.set(message)
        self.status_var.set(message)
        self._stage_log("OBS", message, level="ERROR")
        self._refresh_home_dashboard()

    def _show_obs_source_capture_result(self, payload) -> None:
        self._set_busy_ui(False)
        if not isinstance(payload, dict):
            self._stage_log("OCR", f"OBS 소스 캡쳐 결과 오류: {payload}", level="ERROR")
            return
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        if payload.get("status") == "captured":
            capture_path = str(payload.get("capture_path", ""))
            source_name = str(payload.get("source_name", ""))
            self.last_obs_capture_path = capture_path
            self.last_obs_capture_source_name = source_name
            self._stage_log("OCR", f"OBS 소스 캡쳐 저장됨: {source_name} -> {capture_path}", level="SUCCESS")
            return
        error = str(payload.get("error", "원인 불명"))
        self._stage_log("OCR", f"OBS 소스 캡쳐 실패: {error}", level="ERROR")

    def _show_obs_source_ocr_result(self, payload) -> None:
        self._set_busy_ui(False)
        if not isinstance(payload, dict):
            self._stage_log("OCR", f"OBS 소스 OCR 결과 오류: {payload}", level="ERROR")
            return
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", self._obs_source_ocr_viewer_text(payload))
        capture_payload = payload.get("capture", {})
        if not isinstance(capture_payload, dict):
            capture_payload = {}
        capture_path = str(payload.get("capture_path") or capture_payload.get("capture_path", ""))
        source_name = str(payload.get("source_name") or capture_payload.get("source_name", ""))
        if capture_path:
            self.last_obs_capture_path = capture_path
        if source_name:
            self.last_obs_capture_source_name = source_name
        if payload.get("status") == "ready":
            self.last_obs_ocr_payload = dict(payload)
            self._stage_log("OCR", f"OBS 소스 OCR 완료: {source_name}", level="SUCCESS")
            return
        error = str(payload.get("error", "원인 불명"))
        self._stage_log("OCR", f"OBS 소스 OCR 실패: {error}", level="ERROR")

    def _obs_source_ocr_viewer_text(self, payload: dict[str, object]) -> str:
        if payload.get("status") != "ready":
            return json.dumps(payload, ensure_ascii=False, indent=2)
        lines = [
            f"OCR OBS 소스: {payload.get('source_name', '-')}",
            f"캡쳐 파일: {payload.get('capture_path', '-')}",
            f"이미지 크기: {payload.get('image_size', '-')}",
            "",
            "OCR 결과",
        ]
        ocr_rows = payload.get("ocr", [])
        if isinstance(ocr_rows, list):
            for row in ocr_rows:
                if not isinstance(row, dict):
                    continue
                slot = row.get("slot_index", "?")
                raw = str(row.get("raw_text", "") or "").strip() or "(빈 값)"
                confidence = row.get("confidence", 0)
                error = str(row.get("error", "") or "")
                crop_path = str(row.get("crop_path", "") or "")
                suffix = f" / 오류={error}" if error else ""
                lines.append(f"{slot}번: {raw} / 신뢰도={confidence}{suffix}")
                if crop_path:
                    lines.append(f"  crop: {crop_path}")
        lines.extend(["", "상세 JSON", json.dumps(payload, ensure_ascii=False, indent=2)])
        return "\n".join(lines)

    def _ordered_obs_rects(self, names: list[str], rects: object) -> list[dict[str, int]]:
        if not isinstance(rects, dict):
            return []
        ordered: list[dict[str, int]] = []
        for name in names:
            raw = rects.get(name)
            if not isinstance(raw, dict):
                return []
            try:
                ordered.append(
                    {
                        "x": int(raw["x"]),
                        "y": int(raw["y"]),
                        "w": int(raw["w"]),
                        "h": int(raw["h"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                return []
        return ordered

    def _source_rect_config(self, names: list[str], rects: list[dict[str, int]]) -> list[dict[str, object]]:
        return [
            {
                "source": name,
                "x": rect["x"],
                "y": rect["y"],
                "w": rect["w"],
                "h": rect["h"],
            }
            for name, rect in zip(names, rects)
        ]

    def _roi_slot_rect_payload_or_empty(self) -> list[dict[str, int]]:
        try:
            return self._roi_slot_rect_payload()
        except Exception:
            return []

    def _same_rects(self, before: object, after: list[dict[str, int]]) -> bool:
        if not isinstance(before, list) or len(before) != len(after):
            return False
        for old, new in zip(before, after):
            if not isinstance(old, dict):
                return False
            try:
                old_tuple = (int(old["x"]), int(old["y"]), int(old["w"]), int(old["h"]))
                new_tuple = (int(new["x"]), int(new["y"]), int(new["w"]), int(new["h"]))
            except (KeyError, TypeError, ValueError):
                return False
            if old_tuple != new_tuple:
                return False
        return True

    def _notify_rect_change(self, label: str, before: object, after: list[dict[str, int]]) -> None:
        lines = [f"{label} 좌표가 마지막 값과 다릅니다."]
        if isinstance(before, list):
            for index, (old, new) in enumerate(zip(before, after), start=1):
                if not isinstance(old, dict):
                    continue
                try:
                    old_text = f"{int(old['x'])},{int(old['y'])},{int(old['w'])},{int(old['h'])}"
                except (KeyError, TypeError, ValueError):
                    old_text = "알 수 없음"
                new_text = f"{new['x']},{new['y']},{new['w']},{new['h']}"
                if old_text != new_text:
                    lines.append(f"{index}번: {old_text} -> {new_text}")
        message = "\n".join(lines[:6])
        messagebox.showinfo("OBS 좌표 변경", message)

    def _show_result(self, result) -> None:
        self.last_result = result
        append_result = self.reward_ledger.append_result(result, trigger=str(getattr(result, "trigger", "")), result_date=self._result_date())
        if append_result.duplicate:
            self._stage_log("RESULT", "자동 감지 중복 결과라 보상 DB에는 추가하지 않음")
        else:
            self._stage_log("RESULT", f"보상 DB 누적: {append_result.added_count}개 추가")
        self._refresh_result_table()
        self._refresh_result_details()
        self.overlay_text.delete("1.0", "end")
        self.overlay_text.insert("1.0", result.overlay_payload)
        avoid_rect = self._to_overlay_rect(getattr(result.detector, "reward_panel_rect", None))
        self._apply_overlay_window(result.overlay_payload, avoid_rect, result_output=True)
        self._update_obs_text_outputs_async(result)
        self._mark_result_output_block()
        self.status_var.set(f"결과 준비됨: {result.total_ms}ms")
        self._set_busy_ui(False)
        self._refresh_indicators(result)
        self._refresh_log()

    def _mark_result_output_block(self) -> None:
        overlay_ms = 0
        try:
            if self.overlay_enabled.get() and self.overlay_mode.get() in {"window", "console"}:
                overlay_ms = max(0, int(self.overlay_clear_ms.get() or 0))
        except (TypeError, ValueError, tk.TclError):
            overlay_ms = 0
        obs_ms = 0
        try:
            if not self.one_pc_mode_active and self.obs_enabled.get():
                obs_ms = self._obs_text_clear_after_ms()
        except tk.TclError:
            obs_ms = 0
        delay_ms = max(RESULT_OUTPUT_AUTO_SAFEGUARD_MS, overlay_ms + RESULT_OUTPUT_AUTO_SAFEGUARD_MS, obs_ms + RESULT_OUTPUT_AUTO_SAFEGUARD_MS)
        self.result_output_block_until_ms = max(self.result_output_block_until_ms, int(time.monotonic() * 1000) + delay_ms)
        self.last_auto_trigger_ms = int(time.monotonic() * 1000)
        self._stage_log("DETECT", f"결과 출력 후 자동감지 안전 대기 예약: {delay_ms // 1000}s")

    def _result_date(self) -> str:
        return time.strftime("%m_%d_%y")

    def _refresh_result_table(self) -> None:
        if not hasattr(self, "result_table"):
            return
        for row in self.result_table.get_children():
            self.result_table.delete(row)
        rows = self.reward_ledger.filtered(
            received=bool(self.result_filter_received.get()) if hasattr(self, "result_filter_received") else False,
            sell=bool(self.result_filter_sell.get()) if hasattr(self, "result_filter_sell") else False,
            use=bool(self.result_filter_use.get()) if hasattr(self, "result_filter_use") else False,
        )
        for row in rows:
            self.result_table.insert(
                "",
                "end",
                iid=row.id,
                values=(
                    row.number,
                    row.received,
                    row.date,
                    row.item,
                    self._format_number(row.ducats) if row.ducats is not None else "",
                    self._format_number(row.plat) if row.plat is not None else "",
                    row.sell,
                    row.use,
                ),
            )

    def _clear_result_filters(self) -> None:
        self.result_filter_received.set(False)
        self.result_filter_sell.set(False)
        self.result_filter_use.set(False)
        self._refresh_result_table()

    def _refresh_result_details(self) -> None:
        self.details_text.delete("1.0", "end")
        if self.last_result is None:
            return
        self.details_text.insert(
            "1.0",
            "\n".join(
                (
                    f"{r.slot_index}: 원문={r.raw_ocr} | 정규화={r.normalized_text} | "
                    f"매칭={r.matched_name} | 점수={r.match_score} | 방식={r.match_method} | 경고={r.warning or '-'}"
                )
                for r in self.last_result.rewards
            ),
        )

    def _on_result_right_click(self, event) -> None:
        row_id = self.result_table.identify_row(event.y)
        if row_id:
            self.result_table.selection_set(row_id)
            self.result_table.focus(row_id)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="삭제", command=self._delete_selected_result_row)
        menu.tk_popup(event.x_root, event.y_root)

    def _delete_selected_result_row(self) -> None:
        selection = self.result_table.selection()
        if not selection:
            self._stage_log("RESULT", "삭제할 보상 행을 먼저 선택해야 함", level="WARNING")
            return
        for row_id in selection:
            self.reward_ledger.delete(str(row_id))
        self._refresh_result_table()
        self._refresh_result_details()
        self._stage_log("RESULT", "선택한 보상 행 삭제됨", level="SUCCESS")

    def _on_result_double_click(self, event) -> None:
        row_id = self.result_table.identify_row(event.y)
        column_id = self.result_table.identify_column(event.x)
        if not row_id or not column_id:
            return
        columns = list(self.result_table["columns"])
        try:
            column_name = columns[int(column_id.lstrip("#")) - 1]
        except (ValueError, IndexError):
            return
        if column_name not in {"received", "sell", "use"}:
            return
        self._edit_result_cell(row_id, column_name)

    def _edit_result_cell(self, row_id: str, column_name: str) -> None:
        if self.result_cell_editor is not None:
            self.result_cell_editor.destroy()
            self.result_cell_editor = None
        columns = list(self.result_table["columns"])
        column_index = columns.index(column_name)
        bbox = self.result_table.bbox(row_id, f"#{column_index + 1}")
        if not bbox:
            return
        x, y, width, height = bbox
        values = list(self.result_table.item(row_id, "values"))
        current = str(values[column_index]) if column_index < len(values) else ""
        editor = ttk.Entry(self.result_table)
        editor.insert(0, current)
        editor.select_range(0, "end")
        editor.focus_set()
        editor.place(x=x, y=y, width=width, height=height)
        self.result_cell_editor = editor

        def commit(_event=None) -> None:
            if self.result_cell_editor is None:
                return
            value = editor.get().strip()
            if not is_result_memo_value_valid(value):
                self._stage_log("RESULT", f"입력 오류: {self._result_choice_label(column_name)} 칸은 텍스트만 입력 가능", level="ERROR")
                editor.focus_set()
                return
            values = list(self.result_table.item(row_id, "values"))
            while len(values) < len(columns):
                values.append("")
            values[column_index] = value
            self.reward_ledger.update_notes(
                str(row_id),
                received=value if column_name == "received" else None,
                sell=value if column_name == "sell" else None,
                use=value if column_name == "use" else None,
            )
            self.result_table.item(row_id, values=values)
            editor.destroy()
            self.result_cell_editor = None
            self._refresh_result_table()
            self._refresh_result_details()
            self._stage_log("RESULT", f"{values[0]}번 {self._result_choice_label(column_name)} 값 입력: {value or '-'}", level="SUCCESS")

        def cancel(_event=None) -> None:
            if self.result_cell_editor is not None:
                self.result_cell_editor.destroy()
                self.result_cell_editor = None

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", cancel)

    def _result_choice_label(self, column_name: str) -> str:
        return {"received": "수령", "sell": "판매", "use": "사용"}.get(column_name, column_name)

    def _safe_slot_from_values(self, values: object) -> int | None:
        if not isinstance(values, (list, tuple)) or not values:
            return None
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return None

    def _update_obs_text_outputs_async(self, result) -> None:
        if self.one_pc_mode_active:
            self.result_obs_text_output_active = False
            self._stage_log("OBS", "T 출력 건너뜀: 1PC 모드에서 일반 오버레이만 사용", level="INFO")
            return
        if not self.obs_enabled.get():
            self._stage_log("OBS", "T 출력 건너뜀: OBS 연결이 꺼져 있음", level="WARNING")
            return
        if not self.obs_connected:
            self._stage_log("OBS", "OBS 연결상태가 Off지만 저장된 접속 정보로 T 출력 갱신을 시도함", level="WARNING")
        try:
            host = self.obs_host.get().strip()
            port = int(self.obs_port.get())
            timeout_ms = int(self.obs_timeout.get())
            password = self._obs_password_for_connection("OBS")
        except Exception as exc:
            self._stage_log("OBS", f"오류: OBS 출력 설정값이 올바르지 않음: {exc}")
            return
        text_by_source = self._obs_text_output_payload(result.rewards)
        if not text_by_source:
            self._stage_log("OBS", "T 출력 건너뜀: 활성화된 출력 소스가 없음", level="WARNING")
            return
        self.result_obs_text_output_active = True
        self.status_var.set("OBS T 출력 갱신 중")
        clear_after_ms = self._obs_text_clear_after_ms()
        threading.Thread(
            target=self._worker_obs_text_outputs,
            args=(host, port, password, self._obs_text_update_timeout_seconds(timeout_ms), text_by_source, False, clear_after_ms),
            daemon=True,
        ).start()

    def _obs_text_update_timeout_seconds(self, timeout_ms: int) -> float:
        return min(max(0.5, timeout_ms / 1000), 1.25)

    def _obs_text_clear_after_ms(self) -> int:
        try:
            value = int(self.config.section("obs_websocket").get("text_clear_after_ms", 5000))
        except (TypeError, ValueError):
            value = 5000
        return max(0, value)

    def _schedule_obs_text_clear(self, source_names: list[str], clear_after_ms: int) -> None:
        if self.obs_text_clear_after_id is not None:
            self.root.after_cancel(self.obs_text_clear_after_id)
            self.obs_text_clear_after_id = None
        names = [name.strip() for name in source_names if name.strip()]
        if clear_after_ms <= 0 or not names:
            return
        self.obs_text_clear_after_id = self.root.after(clear_after_ms, lambda: self._clear_obs_text_outputs_async(names))
        self._stage_log("OBS", f"{clear_after_ms}ms 뒤 T 출력 자동 지움 예약")

    def _clear_obs_text_outputs_async(self, source_names: list[str]) -> None:
        self.obs_text_clear_after_id = None
        if not self.obs_enabled.get():
            return
        try:
            host = self.obs_host.get().strip()
            port = int(self.obs_port.get())
            timeout_ms = int(self.obs_timeout.get())
            password = self._obs_password_for_connection("OBS")
        except Exception as exc:
            self._stage_log("OBS", f"오류: OBS T 지움 설정값이 올바르지 않음: {exc}", level="ERROR")
            return
        text_by_source = {name: "" for name in source_names if name.strip()}
        if not text_by_source:
            return
        threading.Thread(
            target=self._worker_obs_text_outputs,
            args=(host, port, password, self._obs_text_update_timeout_seconds(timeout_ms), text_by_source, True, 0),
            daemon=True,
        ).start()

    def _obs_text_output_payload(self, rewards) -> dict[str, str]:
        rewards_by_slot = {int(reward.slot_index): reward for reward in rewards}
        slot_widths = self._obs_input_slot_widths()
        text_by_source: dict[str, str] = {}
        seen_sources: set[str] = set()
        for index, source_var in enumerate(self.obs_output_sources, start=1):
            if index - 1 < len(self.obs_output_source_enabled) and not self.obs_output_source_enabled[index - 1].get():
                continue
            source_name = source_var.get().strip() or f"T{index}"
            source_key = source_name.lower()
            if source_key in seen_sources:
                self._stage_log("OBS", "오류: 활성화된 T 출력소스 이름이 중복됨", level="ERROR")
                return {}
            seen_sources.add(source_key)
            reward = rewards_by_slot.get(index)
            text_by_source[source_name] = self._obs_reward_text(reward, slot_widths.get(index))
        return text_by_source

    def _obs_reward_text(self, reward, slot_width_px: int | None = None) -> str:
        return format_obs_reward_text(reward, slot_width_px)

    def _obs_input_slot_widths(self) -> dict[int, int]:
        rects = self._ordered_obs_rects_from_list(self.config.section("obs_websocket").get("browser_source_rects", []))
        if len(rects) != 4:
            try:
                rects = self._roi_slot_rect_payload()
            except Exception:
                rects = []
        widths: dict[int, int] = {}
        for index, rect in enumerate(rects, start=1):
            try:
                widths[index] = int(rect.get("w", 0))
            except (AttributeError, TypeError, ValueError):
                continue
        return widths

    def _format_number(self, value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "0"
        if number.is_integer():
            return str(int(number))
        return f"{number:.1f}".rstrip("0").rstrip(".")

    def _correct_match(self) -> None:
        if self.last_result is None:
            self._stage_log("MATCH", "오류: 선택할 결과가 없음")
            return
        selection = self.result_table.selection()
        if not selection:
            self._stage_log("MATCH", "오류: 보상 행을 먼저 선택해야 함")
            return
        values = self.result_table.item(selection[0], "values")
        slot_index = int(values[0])
        reward = next((row for row in self.last_result.rewards if row.slot_index == slot_index), None)
        if reward is None:
            self._stage_log("MATCH", "오류: 선택한 보상을 찾을 수 없음")
            return
        item_id = simpledialog.askstring("매칭 수정", "아이템 ID 입력", initialvalue=reward.matched_item_id or "")
        if not item_id:
            return
        path_value = self.config.section("matching").get("correction_store_path", "data/corrections.json")
        path = resolve_project_path(str(path_value))
        store = CorrectionStore(path)
        try:
            store.set(reward.normalized_text, item_id, overwrite=False)
        except ValueError:
            if not messagebox.askyesno("매칭 수정", "이미 수정값이 있습니다. 덮어쓸까요?"):
                return
            store.set(reward.normalized_text, item_id, overwrite=True)
        store.save()
        self._stage_log("MATCH", f"수정값 저장됨: {reward.normalized_text} -> {item_id}")

    def _copy_ocr(self) -> None:
        if self.last_result is None:
            payload = self.last_obs_ocr_payload
            text = str(payload.get("raw_ocr", "")) if isinstance(payload, dict) else ""
            if not text.strip():
                text = self.details_text.get("1.0", "end").strip()
            if not text:
                self._stage_log("OCR", "오류: 복사할 OCR 결과가 없음")
                return
        else:
            text = "\n".join(f"{r.slot_index}: {r.raw_ocr}" for r in self.last_result.rewards)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._stage_log("OCR", "OCR 원문 복사됨")

    def _copy_overlay(self) -> None:
        text = self.overlay_text.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._stage_log("OVERLAY", "오버레이 페이로드 복사됨")

    def _show_overlay_window_now(self) -> None:
        try:
            self.overlay_enabled.set(True)
            if self.overlay_mode.get() == "disabled":
                self.overlay_mode.set("window")
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("OVERLAY", f"오류: 오버레이 설정값이 올바르지 않음: {exc}")
            return
        payload = self._current_overlay_payload()
        if payload is None:
            return
        self.overlay_text.delete("1.0", "end")
        self.overlay_text.insert("1.0", payload)
        if self.obs_capture_overlay_window is not None:
            self.obs_capture_overlay_window.clear()
            self.obs_capture_overlay_window.hide()
        if self.overlay_clear_after_id is not None:
            self.root.after_cancel(self.overlay_clear_after_id)
            self.overlay_clear_after_id = None
        opacity = float(self.overlay_opacity.get() or 0.92)
        if self.overlay_window is None:
            self.overlay_window = OverlayWindow(
                self.root,
                click_through=bool(self.overlay_click_through.get()),
                opacity=opacity,
            )
        else:
            self.overlay_window.set_click_through(bool(self.overlay_click_through.get()))
            self.overlay_window.set_opacity(opacity)
        x, y, w, h = self._resolve_overlay_position(None)
        self.overlay_window.show(payload, x=x, y=y, w=w, h=h, topmost=bool(self.overlay_topmost.get()))
        mode_label = OVERLAY_MODE_LABELS.get(self.overlay_mode.get(), self.overlay_mode.get())
        self._stage_log("OVERLAY", f"오버레이 창을 띄움: {mode_label}")
        self._refresh_home_toggle_buttons()

    def _toggle_overlay_adjust_window(self) -> None:
        if self.overlay_adjust_window is not None and self.overlay_adjust_window.winfo_exists():
            self._save_overlay_adjust_window()
            return
        self._open_overlay_adjust_window()

    def _open_overlay_adjust_window(self) -> None:
        try:
            self.overlay_enabled.set(True)
            if self.overlay_mode.get() == "disabled":
                self.overlay_mode.set("window")
            self.overlay_position.set("custom")
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("OVERLAY", f"오류: 오버레이 위치 조절 시작 실패: {exc}", level="ERROR")
            return
        x, y, w, h = self._resolve_overlay_position(None)
        window = tk.Toplevel(self.root)
        window.title("OBS prime 오버레이 위치 조절")
        window.configure(bg="black")
        window.resizable(True, True)
        window.geometry(f"{max(120, w)}x{max(80, h)}+{max(0, x)}+{max(0, y)}")
        window.protocol("WM_DELETE_WINDOW", self._save_overlay_adjust_window)
        window.bind("<Configure>", self._on_overlay_adjust_configure)
        self.overlay_adjust_window = window
        if self.overlay_adjust_button_text is not None:
            self.overlay_adjust_button_text.set("저장")
        self.root.after(50, self._sync_overlay_adjust_rect)
        self._stage_log("OVERLAY", "오버레이 위치 조절창을 띄움")

    def _on_overlay_adjust_configure(self, event) -> None:
        if self.overlay_adjust_window is None or event.widget is not self.overlay_adjust_window:
            return
        self._sync_overlay_adjust_rect()

    def _sync_overlay_adjust_rect(self) -> None:
        window = self.overlay_adjust_window
        if window is None or not window.winfo_exists():
            return
        try:
            geometry = window.geometry()
            match = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geometry)
            if not match:
                return
            w, h, x, y = (int(match.group(index)) for index in range(1, 5))
        except (tk.TclError, ValueError):
            return
        for variable, value in [
            (self.overlay_x, x),
            (self.overlay_y, y),
            (self.overlay_w, w),
            (self.overlay_h, h),
        ]:
            text = str(value)
            if variable.get() != text:
                variable.set(text)
        if self.overlay_position.get() != "custom":
            self.overlay_position.set("custom")

    def _save_overlay_adjust_window(self) -> None:
        self._sync_overlay_adjust_rect()
        if self.overlay_adjust_window is not None:
            try:
                self.overlay_adjust_window.destroy()
            except tk.TclError:
                pass
            self.overlay_adjust_window = None
        if self.overlay_adjust_button_text is not None:
            self.overlay_adjust_button_text.set("오버레이 위치 직접 조절")
        try:
            self.overlay_enabled.set(True)
            if self.overlay_mode.get() == "disabled":
                self.overlay_mode.set("window")
            self.overlay_position.set("custom")
            self._apply_gui_to_runtime()
            self.config.save()
        except Exception as exc:
            self._stage_log("OVERLAY", f"오류: 오버레이 위치 저장 실패: {exc}", level="ERROR")
            return
        self.gui_dirty = False
        self._refresh_profile_title()
        self._stage_log("OVERLAY", f"오버레이 위치 저장됨: {self.overlay_x.get()},{self.overlay_y.get()},{self.overlay_w.get()},{self.overlay_h.get()}", level="SUCCESS")

    def _reset_overlay_window_position(self) -> None:
        monitor_x, monitor_y, monitor_w, monitor_h = self._current_monitor_work_area()
        try:
            width = max(120, min(int(self.overlay_w.get() or 620), monitor_w))
            height = max(80, min(int(self.overlay_h.get() or 180), monitor_h))
        except (TypeError, ValueError):
            width, height = self._overlay_layout_size(self.overlay_layout.get() or "horizontal")
            width = max(120, min(width, monitor_w))
            height = max(80, min(height, monitor_h))
        x = monitor_x + max(0, (monitor_w - width) // 2)
        y = monitor_y + max(0, (monitor_h - height) // 2)
        self.overlay_enabled.set(True)
        if self.overlay_mode.get() == "disabled":
            self.overlay_mode.set("window")
        self.overlay_position.set("custom")
        self.overlay_x.set(str(x))
        self.overlay_y.set(str(y))
        self.overlay_w.set(str(width))
        self.overlay_h.set(str(height))
        try:
            self._apply_gui_to_runtime()
            self.config.save()
        except Exception as exc:
            self._stage_log("OVERLAY", f"오류: 오버레이 위치 초기화 저장 실패: {exc}", level="ERROR")
            return
        self.gui_dirty = False
        self._refresh_profile_title()
        self._show_overlay_window_now()
        self._stage_log("OVERLAY", f"오버레이 창 위치 초기화: {width}x{height}+{x}+{y}")

    def _current_monitor_work_area(self) -> tuple[int, int, int, int]:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("left", wintypes.LONG),
                        ("top", wintypes.LONG),
                        ("right", wintypes.LONG),
                        ("bottom", wintypes.LONG),
                    ]

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", RECT),
                        ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD),
                    ]

                monitor_default_to_nearest = 2
                hwnd = self.root.winfo_id()
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), monitor_default_to_nearest)
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    work = info.rcWork
                    return work.left, work.top, max(1, work.right - work.left), max(1, work.bottom - work.top)
            except Exception:
                pass
        return 0, 0, max(1, self.root.winfo_screenwidth()), max(1, self.root.winfo_screenheight())

    def _set_overlay_layout(self, layout: str) -> None:
        layout = "vertical" if layout == "vertical" else "horizontal"
        width, height = self._overlay_layout_size(layout)
        self.overlay_enabled.set(True)
        if self.overlay_mode.get() == "disabled":
            self.overlay_mode.set("window")
        self.overlay_layout.set(layout)
        self.overlay_position.set("custom")
        self.overlay_w.set(str(width))
        self.overlay_h.set(str(height))
        if self.overlay_adjust_window is not None and self.overlay_adjust_window.winfo_exists():
            self.overlay_adjust_window.geometry(
                f"{width}x{height}+{int(self.overlay_x.get() or 0)}+{int(self.overlay_y.get() or 0)}"
            )
        try:
            self._apply_gui_to_runtime()
            self.config.save()
        except Exception as exc:
            self._stage_log("OVERLAY", f"오류: 오버레이 레이아웃 저장 실패: {exc}", level="ERROR")
            return
        payload = self._current_overlay_payload()
        if payload:
            self.overlay_text.delete("1.0", "end")
            self.overlay_text.insert("1.0", payload)
        self.gui_dirty = False
        self._refresh_profile_title()
        label = "세로" if layout == "vertical" else "가로"
        self._stage_log("OVERLAY", f"오버레이 {label}모드 저장됨", level="SUCCESS")

    def _overlay_layout_size(self, layout: str) -> tuple[int, int]:
        if layout == "vertical":
            return 420, 594
        return 900, 180

    def _show_obs_capture_overlay_window_now(self) -> None:
        if self.one_pc_mode_active:
            self._stage_log("OVERLAY", "1PC 모드에서는 OBS용 창 대신 일반 오버레이 창을 사용")
            self._show_overlay_window_now()
            return
        try:
            self.overlay_enabled.set(True)
            if self.overlay_mode.get() == "disabled":
                self.overlay_mode.set("window")
            self._apply_gui_to_runtime()
        except Exception as exc:
            self._stage_log("OVERLAY", f"오류: 오버레이 설정값이 올바르지 않음: {exc}")
            return
        payload = self._current_overlay_payload(force_mode="window")
        if payload is None:
            return
        self.overlay_text.delete("1.0", "end")
        self.overlay_text.insert("1.0", payload)
        if self.obs_capture_overlay_window is None:
            self.obs_capture_overlay_window = ObsCaptureOverlayWindow(self.root)
        x, y, w, h = self._resolve_overlay_position(None)
        self.obs_capture_overlay_window.show(payload, x=x, y=y, w=w, h=h)
        self._stage_log("OVERLAY", "OBS 창 캡쳐용 오버레이 창을 띄움: OBS prime OBS Overlay Source")

    def _current_overlay_payload(self, force_mode: str | None = None) -> str | None:
        if self.last_result is not None:
            try:
                return build_overlay_provider(self._selected_overlay_payload_mode(force_mode), self.config.section("overlay")).render(self.last_result.rewards)
            except Exception as exc:
                self._stage_log("OVERLAY", f"오류: 오버레이 페이로드 생성 실패: {exc}", level="ERROR")
                return None
        payload = self.overlay_text.get("1.0", "end").strip()
        if not payload:
            try:
                stage = self.controller.run_stage("overlay_test")
                payload = str(stage.get("payload", "")).strip()
            except Exception as exc:
                self._stage_log("OVERLAY", f"오류: 오버레이 미리보기 생성 실패: {exc}")
                return None
        return payload or "OBS prime Overlay\n대기 중"

    def _selected_overlay_payload_mode(self, force_mode: str | None = None) -> str:
        mode = force_mode or self.overlay_mode.get()
        return mode if mode in {"console", "window"} else "window"

    def _resolve_overlay_position(self, avoid_rect: OverlayRect | None) -> tuple[int, int, int, int]:
        x = int(self.overlay_x.get() or 0)
        y = int(self.overlay_y.get() or 0)
        w = int(self.overlay_w.get() or 620)
        h = int(self.overlay_h.get() or 180)
        custom_rect = OverlayRect(x=x, y=y, w=w, h=h)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        def clamp_to_screen(rect: OverlayRect) -> OverlayRect:
            cw = max(1, min(rect.w, screen_w))
            ch = max(1, min(rect.h, screen_h))
            cx = min(max(0, rect.x), max(0, screen_w - cw))
            cy = min(max(0, rect.y), max(0, screen_h - ch))
            return OverlayRect(cx, cy, cw, ch)

        candidates = {
            "top-left": clamp_to_screen(OverlayRect(20, 20, w, h)),
            "top-right": clamp_to_screen(OverlayRect(max(0, screen_w - w - 20), 20, w, h)),
            "bottom-left": clamp_to_screen(OverlayRect(20, max(0, screen_h - h - 40), w, h)),
            "bottom-right": clamp_to_screen(OverlayRect(max(0, screen_w - w - 20), max(0, screen_h - h - 40), w, h)),
            "custom": clamp_to_screen(custom_rect),
        }
        if avoid_rect is None:
            chosen = candidates.get(self.overlay_position.get(), candidates["custom"])
            return chosen.x, chosen.y, chosen.w, chosen.h
        selected = self.overlay_position.get()
        ordered_positions = [selected] if selected in candidates else []
        ordered_positions += [position for position in ["top-right", "top-left", "bottom-left", "bottom-right", "custom"] if position != selected]

        for position in ordered_positions:
            candidate = candidates[position]
            if not self._overlay_overlaps(candidate, avoid_rect):
                return candidate.x, candidate.y, candidate.w, candidate.h
        self._stage_log("OVERLAY", "오버레이 위치가 보상 패널과 겹쳐 대체 위치 사용")
        fallback = candidates.get(self.overlay_position.get(), candidates["custom"])
        return fallback.x, fallback.y, fallback.w, fallback.h

    def _overlay_overlaps(self, first: OverlayRect, second: OverlayRect) -> bool:
        return not (
            first.x + first.w <= second.x
            or second.x + second.w <= first.x
            or first.y + first.h <= second.y
            or second.y + second.h <= first.y
        )

    def _apply_overlay_window(self, payload: str, avoid_rect: OverlayRect | None = None, result_output: bool = False) -> None:
        if self.one_pc_mode_active:
            previous_loading = self._loading_config
            try:
                self._loading_config = True
                self._set_one_pc_mode_variables()
            finally:
                self._loading_config = previous_loading
            avoid_rect = None
        mode = self.overlay_mode.get()
        clear_ms = int(self.overlay_clear_ms.get() or 0)
        if not self.overlay_enabled.get() or mode not in {"window", "console"}:
            if self.overlay_clear_after_id is not None:
                self.root.after_cancel(self.overlay_clear_after_id)
                self.overlay_clear_after_id = None
            if result_output:
                self.result_overlay_output_active = False
            self._clear_overlay_window()
            return
        opacity = float(self.overlay_opacity.get() or 0.92)
        x, y, w, h = self._resolve_overlay_position(avoid_rect)
        if self.one_pc_mode_active and self.obs_capture_overlay_window is not None and self.obs_capture_overlay_window.is_visible():
            self.obs_capture_overlay_window.clear()
            self.obs_capture_overlay_window.hide()
        obs_capture_visible = bool(self.obs_capture_overlay_window is not None and self.obs_capture_overlay_window.is_visible())
        normal_overlay_visible = bool(self.overlay_window is not None and self.overlay_window.is_visible())
        if obs_capture_visible:
            self.obs_capture_overlay_window.show(payload, x=x, y=y, w=w, h=h)
        if obs_capture_visible and not normal_overlay_visible:
            if clear_ms > 0:
                if self.overlay_clear_after_id is not None:
                    self.root.after_cancel(self.overlay_clear_after_id)
                self.overlay_clear_after_id = self.root.after(clear_ms, self._clear_overlay_payload)
            return
        if self.overlay_clear_after_id is not None:
            self.root.after_cancel(self.overlay_clear_after_id)
            self.overlay_clear_after_id = None
        if self.overlay_window is None:
            self.overlay_window = OverlayWindow(
                self.root,
                click_through=bool(self.overlay_click_through.get()),
                opacity=opacity,
            )
        else:
            self.overlay_window.set_click_through(bool(self.overlay_click_through.get()))
            self.overlay_window.set_opacity(opacity)

        self.overlay_window.show(payload, x=x, y=y, w=w, h=h, topmost=bool(self.overlay_topmost.get()))
        if result_output:
            self.result_overlay_output_active = True
        if self.one_pc_mode_active:
            self._stage_log("OVERLAY", "1PC 일반 오버레이 출력 갱신")
        if clear_ms > 0:
            self.overlay_clear_after_id = self.root.after(clear_ms, self._clear_overlay_payload)

    def _to_overlay_rect(self, value: object) -> OverlayRect | None:
        if isinstance(value, OverlayRect):
            return value
        if isinstance(value, dict):
            try:
                return OverlayRect(
                    x=int(value["x"]),
                    y=int(value["y"]),
                    w=int(value["w"]),
                    h=int(value["h"]),
                )
            except (TypeError, ValueError, KeyError):
                return None
        if hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "w") and hasattr(value, "h"):
            try:
                return OverlayRect(
                    x=int(getattr(value, "x")),
                    y=int(getattr(value, "y")),
                    w=int(getattr(value, "w")),
                    h=int(getattr(value, "h")),
                )
            except (TypeError, ValueError, AttributeError):
                return None
        return None

    def _clear_overlay_payload(self) -> None:
        self.overlay_text.delete("1.0", "end")
        self.result_overlay_output_active = False
        self._clear_overlay_window()
        self._refresh_home_toggle_buttons()
        self.overlay_clear_after_id = None

    def _clear_overlay_window(self) -> None:
        self.result_overlay_output_active = False
        if self.overlay_window is not None:
            self.overlay_window.clear()
            self.overlay_window.hide()
        if self.obs_capture_overlay_window is not None:
            self.obs_capture_overlay_window.clear()
            self.obs_capture_overlay_window.hide()
        self.overlay_clear_after_id = None

    def _clear_overlay(self) -> None:
        if self.overlay_clear_after_id is not None:
            self.root.after_cancel(self.overlay_clear_after_id)
            self.overlay_clear_after_id = None
        self.overlay_text.delete("1.0", "end")
        self._clear_overlay_window()

    def _open_crop(self) -> None:
        if self.last_result is None:
            payload = self.last_obs_ocr_payload
            if not isinstance(payload, dict):
                self._stage_log("OCR", "오류: 선택할 결과가 없음")
                return
            slot_index = simpledialog.askinteger("크롭 열기", "열 슬롯 번호 (1-4)", minvalue=1, maxvalue=4)
            if not slot_index:
                return
            ocr_rows = payload.get("ocr", [])
            if not isinstance(ocr_rows, list):
                self._stage_log("OCR", "오류: OCR crop 결과가 없음")
                return
            row = None
            for item in ocr_rows:
                if not isinstance(item, dict):
                    continue
                try:
                    current_slot = int(item.get("slot_index", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if current_slot == slot_index:
                    row = item
                    break
            crop_path = str(row.get("crop_path", "")) if isinstance(row, dict) else ""
            if not crop_path:
                self._stage_log("OCR", "오류: 선택한 보상에 크롭 파일이 없음")
                return
            self._open_project_file(crop_path, "OCR")
            return
        selection = self.result_table.selection()
        if not selection:
            self._stage_log("OCR", "오류: 보상 행을 먼저 선택해야 함")
            return
        slot_index = int(self.result_table.item(selection[0], "values")[0])
        reward = next((row for row in self.last_result.rewards if row.slot_index == slot_index), None)
        if not reward or not reward.crop_path:
            self._stage_log("OCR", "오류: 선택한 보상에 크롭 파일이 없음")
            return
        self._open_project_file(reward.crop_path, "OCR")

    def _open_project_file(self, path: str, stage: str) -> None:
        try:
            target = resolve_project_path(path)
        except ValueError as exc:
            self._stage_log(stage, f"오류: 프로젝트 밖 파일은 열 수 없음: {exc}", level="ERROR")
            return
        if not target.exists():
            self._stage_log(stage, f"오류: 파일이 없음: {target}", level="ERROR")
            return
        os.startfile(str(target))
        self._stage_log(stage, f"열림: {target}")

    def _open_db_folder(self) -> None:
        data_dir = resolve_project_path("data")
        os.startfile(str(data_dir))
        self._stage_log("DB", f"열림: {data_dir}")

    def _open_item_wiki_folder(self) -> None:
        self._open_configured_data_path(self.item_wiki_dir.get().strip() or "data/item_wiki", "두캇 DB", directory=True)

    def _open_market_wiki_folder(self) -> None:
        self._open_configured_data_path(self.market_wiki_dir.get().strip() or "data/market_wiki", "마켓 Wiki", directory=True)

    def _open_price_cache_folder(self) -> None:
        self._open_configured_data_path(self.price_db_path.get().strip() or "data/market_cache/warframe_market_prices.json", "가격 캐시", directory=False)

    def _open_configured_data_path(self, raw_path: str, label: str, directory: bool) -> None:
        try:
            target = resolve_project_path(raw_path)
        except Exception as exc:
            self._stage_log("DB", f"{label} 경로 오류: {exc}", "ERROR")
            return
        open_target = target if directory else target.parent
        if not open_target.exists():
            self._stage_log("DB", f"{label} 경로 없음: {open_target}", "WARNING")
            return
        os.startfile(str(open_target))
        self._stage_log("DB", f"{label} 열림: {open_target}")

    def _refresh_database_page_status(self) -> None:
        if not hasattr(self, "database_item_status"):
            return
        self.database_item_status.set(self._item_wiki_status_text())
        self.database_market_status.set(self._market_wiki_status_text())
        self.database_price_status.set(self._price_cache_status_text())

    def _item_wiki_status_text(self) -> str:
        try:
            status = item_wiki_version(self.item_wiki_dir.get().strip() or "data/item_wiki")
            if status.get("status") == "ready":
                return (
                    f"두캇 DB: {status.get('version', '-')} 버전 / "
                    f"{status.get('count', 0)}개 / {status.get('index_path', '-')}"
                )
            return f"두캇 DB: {status.get('text', '확인 실패')}"
        except Exception as exc:
            return f"두캇 DB: 확인 실패 - {exc}"

    def _market_wiki_status_text(self) -> str:
        try:
            index_path = resolve_project_path(self.market_wiki_dir.get().strip() or "data/market_wiki") / "_index.json"
            if not index_path.exists():
                return f"마켓 Wiki: 인덱스 없음 / {index_path}"
            payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                return f"마켓 Wiki: 인덱스 형식 오류 / {index_path}"
            updated_at = str(payload.get("updated_at", "-"))
            return (
                f"마켓 Wiki: {payload.get('version', '-')} 버전 / "
                f"{payload.get('count', 0)}개 / 갱신 {updated_at} / {index_path}"
            )
        except Exception as exc:
            return f"마켓 Wiki: 확인 실패 - {exc}"

    def _price_cache_status_text(self) -> str:
        try:
            path = resolve_project_path(self.price_db_path.get().strip() or "data/market_cache/warframe_market_prices.json")
            if not path.exists():
                return f"가격 캐시: 파일 없음 / {path}"
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                return f"가격 캐시: 형식 오류 / {path}"
            prices = payload.get("prices", [])
            count = len(prices) if isinstance(prices, list) else 0
            statuses = payload.get("statuses", [])
            status_text = ", ".join(str(value) for value in statuses) if isinstance(statuses, list) else "-"
            return (
                f"가격 캐시: {count}개 / 상태 {status_text or '-'} / "
                f"갱신 {payload.get('updated_at', '-')} / {path}"
            )
        except Exception as exc:
            return f"가격 캐시: 확인 실패 - {exc}"

    def _open_debug_folder(self) -> None:
        debug_dir = resolve_project_path("debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(debug_dir))
        self._stage_log("PIPE", f"열림: {debug_dir}")

    def _show_stage(self, payload) -> None:
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        if payload.get("stage") == "detector_test":
            self.last_detector_payload = payload
            if payload.get("slot_rects"):
                self._load_roi_from_payload(payload)
        if "payload" in payload:
            self.overlay_text.delete("1.0", "end")
            self.overlay_text.insert("1.0", payload["payload"])
            self._apply_overlay_window(payload["payload"])
        if payload.get("stage") == "db_test":
            self.last_db_summary = (
                f"{payload.get('item_count', 0)}개/"
                f"오래된 가격 {payload.get('stale_price_count', 0)}개/"
                f"{payload.get('oldest_price_age_hours', 0)}h"
            )
        if payload.get("stage") == "market_price_update":
            self.last_db_summary = (
                f"마켓 갱신 {payload.get('updated_count', 0)}개/"
                f"실패 {payload.get('failure_count', 0)}개"
            )
        if payload.get("stage") == "market_api_probe":
            self.last_db_summary = (
                f"마켓 검증 {payload.get('status', '?')}/"
                f"{payload.get('lowest_plat_display', '-')}"
            )
        if payload.get("stage") == "ducat_db_update":
            self.last_db_summary = (
                f"두캇 DB {payload.get('version', '?')}/"
                f"{payload.get('item_count', 0)}개"
            )
        if payload.get("stage") == "market_wiki_update":
            self.last_db_summary = (
                f"마켓 Wiki {payload.get('version', '?')}/"
                f"{payload.get('item_count', payload.get('count', 0))}개"
            )
        if payload.get("stage") == "ocr_check":
            self.last_ocr_summary = self._ocr_check_summary(payload)
            self.controller.log.add("OCR", "SUCCESS" if payload.get("status") == "ready" else "ERROR", self.last_ocr_summary)
            self.status_var.set(self.last_ocr_summary)
        else:
            self.status_var.set(f"단계 완료: {self._stage_label(str(payload.get('stage', '?')))}")
        self._set_busy_ui(False)
        if payload.get("stage") in {"db_test", "market_api_probe", "ducat_db_update", "market_wiki_update"}:
            self._refresh_database_page_status()
        self._refresh_indicators()
        self._refresh_log()

    def _load_roi_from_payload(self, payload: dict[str, object]) -> None:
        rects = payload.get("slot_rects")
        if not isinstance(rects, list):
            return
        if len(rects) != 4:
            self._stage_log("ROI", "감지 결과의 보상칸 개수가 올바르지 않음")
            return
        for row_vars, rect in zip(self.roi_slot_rects, rects):
            if not isinstance(rect, dict):
                continue
            row_vars["x"].set(str(rect.get("x", "")))
            row_vars["y"].set(str(rect.get("y", "")))
            row_vars["w"].set(str(rect.get("w", "")))
            row_vars["h"].set(str(rect.get("h", "")))

    def _toggle_obs_password_visibility(self) -> None:
        self.obs_password_visible.set(not self.obs_password_visible.get())
        self._apply_obs_password_visibility()

    def _apply_obs_password_visibility(self) -> None:
        if self.obs_password_entry is None:
            return
        visible = bool(self.obs_password_visible.get())
        self.obs_password_entry.configure(show="" if visible else "*")
        if self.obs_password_toggle is not None:
            self.obs_password_toggle.configure(text="숨김" if visible else "👁")

    def _clear_obs_password(self) -> None:
        self.obs_password.set("")
        self.config.set_value("obs_websocket", "password_dpapi", "")
        self._loaded_obs_password = ""
        self._obs_password_decrypt_error = ""
        self.obs_status.set("OBS 비밀번호 비움")
        self._stage_log("OBS", "OBS WebSocket 비밀번호를 비움")

    def _obs_password_for_connection(self, stage: str) -> str:
        current_obs_password = self.obs_password.get().strip()
        if current_obs_password != self.obs_password.get():
            self.obs_password.set(current_obs_password)
        if self._obs_password_decrypt_error and not current_obs_password:
            message = "저장된 OBS 비밀번호 복호화 실패: OBS 연결 페이지에서 비밀번호를 다시 입력하고 저장하세요"
            self.obs_status.set(message)
            self._stage_log(stage, message, level="ERROR")
            raise RuntimeError(message)
        return current_obs_password

    def _save_obs_ocr_source(self) -> None:
        try:
            self._apply_gui_to_runtime()
            self.config.save()
        except Exception as exc:
            self._stage_log("OCR", f"오류: OCR OBS 소스 저장 실패: {exc}", level="ERROR")
            return
        self.gui_dirty = False
        self._refresh_profile_title()
        self._stage_log("OCR", f"OCR OBS 소스 저장됨: {self.obs_ocr_source.get().strip() or '이미지'}", level="SUCCESS")

    def _bind_dirty_traces(self) -> None:
        for variable in self._tracked_config_vars():
            variable.trace_add("write", self._mark_gui_dirty)

    def _tracked_config_vars(self) -> list[tk.Variable]:
        variables: list[tk.Variable] = [
            self.capture_mode,
            self.capture_monitor_index,
            self.sample_path,
            self.auto_enabled,
            self.auto_interval,
            self.auto_cooldown,
            self.auto_threshold,
            self.auto_preset,
            self.auto_min_ocr_slots,
            self.roi_preset,
            self.roi_scale,
            self.hotkey_enabled,
            self.hotkey_global,
            self.hotkey_combo,
            self.hotkey_debounce,
            self.ocr_provider,
            self.ocr_language,
            self.ocr_timeout,
            self.ocr_min_confidence,
            self.ocr_preprocessing_preset,
            self.db_fixture,
            self.price_db_path,
            self.item_wiki_dir,
            self.market_wiki_dir,
            self.market_live_enabled,
            self.market_live_timeout,
            self.market_cache_same_day_only,
            self.sample_set_dir,
            self.match_confident,
            self.match_usable,
            self.match_uncertain,
            self.alias_learning,
            self.overlay_mode,
            self.overlay_layout,
            self.overlay_enabled,
            self.overlay_position,
            self.overlay_topmost,
            self.overlay_click_through,
            self.overlay_x,
            self.overlay_y,
            self.overlay_w,
            self.overlay_h,
            self.overlay_opacity,
            self.overlay_clear_ms,
            self.obs_enabled,
            self.obs_host,
            self.obs_port,
            self.obs_timeout,
            self.obs_ocr_source,
            self.obs_password,
        ]
        variables.extend(self.slot_labels)
        variables.extend(self.obs_input_sources)
        variables.extend(self.obs_output_sources)
        variables.extend(self.obs_output_source_enabled)
        for slot_row in self.roi_slot_rects:
            variables.extend(slot_row[key] for key in ("x", "y", "w", "h"))
        return variables

    def _mark_gui_dirty(self, *_args: object) -> None:
        if self._loading_config:
            return
        self.gui_dirty = True
        self._refresh_profile_title()
        self._refresh_indicators()

    def _stage_log(self, channel: str, message: str, level: str = "INFO") -> None:
        self.controller.log.add(channel, level, message)
        self._refresh_log()
        self.status_var.set(message)
        self._refresh_indicators()

    def _refresh_profile_title(self) -> None:
        markers: list[str] = []
        if self.gui_dirty:
            markers.append("편집중")
        if self.config.dirty:
            markers.append("미저장")
        suffix = f" [{' / '.join(markers)}]" if markers else ""
        self.profile_label.configure(text=f"OBS prime{suffix}")

    def _refresh_indicators(self, result=None) -> None:
        ocr = OCR_PROVIDER_LABELS.get(self.ocr_provider.get(), self.ocr_provider.get() or "?")
        auto = "on" if self.auto_running else "off"
        hotkey = self._display_hotkey_combo()
        db_date = self._ducat_db_update_date()
        indicator_text = (
            f"[현재 OCR 엔진 : {ocr}] "
            f"[자동 감지 : {auto}] "
            f"[현재 핫키 : {hotkey}] "
            f"[ducats DB 갱신 : {db_date}]"
        )
        self.indicator_var.set(indicator_text)
        self.home_indicator_var.set(indicator_text.replace("] [ducats DB 갱신", "]\n[ducats DB 갱신", 1))
        self._refresh_home_dashboard()

    def _refresh_home_dashboard(self) -> None:
        if not hasattr(self, "home_obs_status"):
            return
        websocket_state = "On" if self.obs_connected else "Off"
        scene = str(self.last_obs_info.get("current_program_scene_name", "")) if self.last_obs_info else ""
        suffix = f" / 현재 장면: {scene}" if scene else ""
        self.home_obs_status.set(f"WebSocket: {websocket_state}{suffix}")
        provider = OCR_PROVIDER_LABELS.get(self.ocr_provider.get(), self.ocr_provider.get() or "?")
        language = self.ocr_language.get() or "?"
        self.home_ocr_status.set(f"엔진: {provider} / 언어: {language}")
        hotkey = self._display_hotkey_combo()
        self.home_hotkey_status.set(f"현재 기기의 핫키는 '{hotkey}' 조합입니다")
        try:
            interval_ms = int(self.auto_interval.get())
        except (TypeError, ValueError):
            interval_ms = 0
        try:
            min_slots = int(self.auto_min_ocr_slots.get())
        except (TypeError, ValueError):
            min_slots = 2
        interval_text = self._format_seconds(interval_ms)
        auto_state = "on" if self.auto_running else "off"
        self.home_auto_status.set(
            f"현재 자동감지가 {auto_state} 입니다. {interval_text}마다 갱신합니다.\n"
            f"자동감지는 {min_slots}명의 사용자가 감지되면 작동합니다."
        )
        wiki_status = item_wiki_version(self.item_wiki_dir.get().strip() or "data/item_wiki")
        database_text = str(wiki_status.get("text", "현재 데이터 베이스는 미구축 상태입니다."))
        self.home_database_status.set(database_text.replace("버전입니다. ", "버전입니다.\n", 1))
        self._refresh_home_toggle_buttons()

    def _display_hotkey_combo(self) -> str:
        combo = (self.hotkey_combo.get() or "미설정").strip()
        return combo.replace("+", " + ") if combo else "미설정"

    def _ducat_db_update_date(self) -> str:
        wiki_status = item_wiki_version(self.item_wiki_dir.get().strip() or "data/item_wiki")
        version = str(wiki_status.get("version", "")).strip()
        if not version:
            return "-"
        match = re.match(r"^(\d{2})-(\d{2})-(\d{2})$", version)
        if match:
            return f"{match.group(2)}_{match.group(3)}"
        match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", version)
        if match:
            return f"{match.group(2)}_{match.group(3)}"
        return version.replace("-", "_")

    def _format_seconds(self, milliseconds: int) -> str:
        seconds = max(0, milliseconds) / 1000
        if seconds.is_integer():
            return f"{int(seconds)}초"
        return f"{seconds:.2f}".rstrip("0").rstrip(".") + "초"

    def _ocr_check_summary(self, payload: dict[str, object]) -> str:
        provider = str(payload.get("provider", "?"))
        status = str(payload.get("status", "?"))
        languages = payload.get("available_languages", [])
        if isinstance(languages, list):
            language_text = ",".join(str(item) for item in languages)
        else:
            language_text = "-"
        if status == "ready":
            return f"OCR 준비됨: {provider} ({language_text})"
        error = str(payload.get("error", "원인 불명"))
        return f"OCR 사용 불가: {provider} - {error}"

    def _stage_label(self, stage: str) -> str:
        return STAGE_LABELS.get(stage, stage)

    def _hotkey_status_label(self, status: str) -> str:
        return HOTKEY_STATUS_LABELS.get(status, status or "?")

    def _set_busy_ui(self, busy: bool) -> None:
        self.ui_busy = busy
        state = "disabled" if busy else "normal"
        nav_buttons = set(getattr(self, "nav_buttons", {}).values())
        for widget in self._walk_widgets(self.root):
            if widget.winfo_class() == "TButton":
                if widget in nav_buttons:
                    continue
                try:
                    widget.configure(state=state)
                except tk.TclError:
                    pass

    def _toggle_dark_mode(self) -> None:
        self.dark_mode = not self.dark_mode
        self.config.set_value("ui", "dark_mode", self.dark_mode)
        self.gui_dirty = True
        self._refresh_profile_title()
        self._apply_theme()

    def _apply_theme(self) -> None:
        palette = self._theme_palette()
        self.dark_mode_button_text.set("라이트모드" if self.dark_mode else "다크모드")
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(bg=palette["bg"])
        base_styles = (
            "TFrame",
            "TLabel",
            "TLabelframe",
            "TLabelframe.Label",
            "TCheckbutton",
            "TRadiobutton",
            "TScrollbar",
        )
        for style_name in base_styles:
            self.style.configure(style_name, background=palette["bg"], foreground=palette["fg"])
        self.style.configure("TButton", background=palette["button_bg"], foreground=palette["fg"], bordercolor=palette["border"])
        self.style.map(
            "TButton",
            background=[("active", palette["button_active"]), ("disabled", palette["disabled_bg"])],
            foreground=[("disabled", palette["disabled_fg"])],
        )
        self.style.configure("TEntry", fieldbackground=palette["input_bg"], foreground=palette["fg"], insertcolor=palette["fg"])
        self.style.configure("TCombobox", fieldbackground=palette["input_bg"], foreground=palette["fg"], arrowcolor=palette["fg"])
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["input_bg"])],
            foreground=[("readonly", palette["fg"])],
        )
        self.style.configure(
            "Treeview",
            background=palette["input_bg"],
            fieldbackground=palette["input_bg"],
            foreground=palette["fg"],
            bordercolor=palette["border"],
        )
        self.style.configure("Treeview.Heading", background=palette["button_bg"], foreground=palette["fg"])
        self._apply_theme_to_widgets(self.root, palette)

    def _theme_palette(self) -> dict[str, str]:
        if self.dark_mode:
            return {
                "bg": "#202124",
                "fg": "#e8eaed",
                "input_bg": "#2b2d31",
                "button_bg": "#303236",
                "button_active": "#3a3d42",
                "border": "#5f6368",
                "disabled_bg": "#2a2b2f",
                "disabled_fg": "#8a8d91",
                "log_bg": "#050505",
                "log_fg": "#70ff8b",
                "select_bg": "#3f6db5",
            }
        return {
            "bg": "#f0f0f0",
            "fg": "#000000",
            "input_bg": "#ffffff",
            "button_bg": "#f0f0f0",
            "button_active": "#e5e5e5",
            "border": "#c0c0c0",
            "disabled_bg": "#f0f0f0",
            "disabled_fg": "#777777",
            "log_bg": "#000000",
            "log_fg": "#00ff66",
            "select_bg": "#0078d7",
        }

    def _apply_theme_to_widgets(self, widget, palette: dict[str, str]) -> None:
        for child in self._walk_widgets(widget):
            cls = child.winfo_class()
            try:
                if isinstance(child, tk.Text):
                    if child is getattr(self, "log_text", None) or child is getattr(self, "overlay_text", None):
                        child.configure(bg=palette["log_bg"], fg=palette["log_fg"], insertbackground=palette["log_fg"])
                    else:
                        child.configure(
                            bg=palette["input_bg"],
                            fg=palette["fg"],
                            insertbackground=palette["fg"],
                            selectbackground=palette["select_bg"],
                        )
                elif isinstance(child, tk.Listbox):
                    child.configure(
                        bg=palette["input_bg"],
                        fg=palette["fg"],
                        selectbackground=palette["select_bg"],
                        selectforeground=palette["fg"],
                    )
                elif isinstance(child, tk.Button):
                    child.configure(
                        bg=palette["button_bg"],
                        fg=palette["fg"],
                        activebackground=palette["button_active"],
                        activeforeground=palette["fg"],
                    )
                elif cls in {"Frame", "Canvas", "Labelframe"}:
                    child.configure(bg=palette["bg"])
                elif cls == "Label":
                    child.configure(bg=palette["bg"], fg=palette["fg"])
                elif cls == "Entry":
                    child.configure(bg=palette["input_bg"], fg=palette["fg"], insertbackground=palette["fg"])
            except tk.TclError:
                pass

    def _walk_widgets(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk_widgets(child)

    def _refresh_log(self) -> None:
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(self.controller.log.tail()))

    def _on_close(self) -> None:
        try:
            self._apply_gui_to_runtime()
        except Exception:
            pass
        self._stop_auto()
        if self.overlay_clear_after_id is not None:
            self.root.after_cancel(self.overlay_clear_after_id)
        if self.obs_text_clear_after_id is not None:
            self.root.after_cancel(self.obs_text_clear_after_id)
            self.obs_text_clear_after_id = None
        self._clear_overlay_window()
        if self.overlay_window is not None:
            self.overlay_window.destroy()
        if self.obs_capture_overlay_window is not None:
            self.obs_capture_overlay_window.destroy()
        if self.overlay_adjust_window is not None:
            try:
                self.overlay_adjust_window.destroy()
            except tk.TclError:
                pass
            self.overlay_adjust_window = None
        backend = self.hotkey_manager.backend
        if backend is not None:
            try:
                backend.unregister()
            except Exception:
                pass
        if self.config.dirty:
            choice = messagebox.askyesnocancel("OBS prime", "닫기 전에 설정 변경사항을 저장할까요?")
            if choice is None:
                return
            if choice:
                check = self.controller.run_stage("config_check")
                if check.get("status") != "PASS":
                    self._stage_log("CONFIG", f"오류: 설정 검증 실패: {_config_check_message(check)}", level="ERROR")
                    return
                self.config.save()
        self.root.destroy()


def _config_check_message(payload: dict[str, object]) -> str:
    messages: list[str] = []
    for key in ("missing_sections", "missing_fields", "value_errors"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            messages.extend(str(item) for item in value)
    return "; ".join(messages) if messages else "알 수 없는 설정 오류"


def _should_retry_obs_text_update(result: object) -> bool:
    if not isinstance(result, dict) or result.get("status") != "failed":
        return False
    error = str(result.get("error", "")).lower()
    return "timed out" in error or "timeout" in error






