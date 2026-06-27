from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import AppConfig

from .app_controller import PipelineController
from .capture.samples import SampleCaptureProvider
from .detect.auto_detector import AutoDetector
from .paths import resolve_project_path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
MAX_SAMPLE_SET_IMAGES = 200


def run_detector_functional(samples: str | None = None) -> int:
    if not samples:
        samples = str(AppConfig.load().section("diagnostics").get("sample_set_dir", "samples\\reward_screens"))
    sample_paths = _sample_images(samples)
    if not sample_paths:
        payload = {
            "status": "BLOCKED",
            "reason": "no sample images found",
            "samples": samples,
            "requested_samples": samples,
            "resolved_samples": _resolved_sample_path_text(samples),
            "required_action": "add positive/negative Warframe reward screenshots under samples\\reward_screens",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    detector = AutoDetector()
    results = []
    total_ms = 0
    for path in sample_paths:
        frame = SampleCaptureProvider(str(path)).capture()
        started = time.perf_counter()
        result = detector.detect(frame)
        elapsed = int((time.perf_counter() - started) * 1000)
        total_ms += elapsed
        results.append(
            {
                "sample": str(path),
                "detected": result.detected,
                "confidence": result.confidence,
                "preset_id": result.preset_id,
                "slot_rects": [rect.to_dict() for rect in result.slot_rects],
                "reason": result.reason,
                "elapsed_ms": elapsed,
            }
        )
    avg_ms = int(total_ms / len(results)) if results else 0
    payload = {"status": "PASS", "sample_count": len(results), "average_detection_ms": avg_ms, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if avg_ms <= 200 else 1


def run_ocr_functional(samples: str | None = None) -> int:
    if not samples:
        samples = str(AppConfig.load().section("diagnostics").get("sample_set_dir", "samples\\reward_screens"))
    sample_paths = _sample_images(samples)
    if not sample_paths:
        payload = {
            "status": "BLOCKED",
            "reason": "no sample images found",
            "samples": samples,
            "requested_samples": samples,
            "resolved_samples": _resolved_sample_path_text(samples),
            "required_action": "add reward-screen screenshots before real OCR functional validation",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    controller = PipelineController()
    results = []
    failures = []
    for path in sample_paths:
        result = controller.run_pipeline(trigger="sample", sample_path=str(path))
        high_conf = sum(1 for reward in result.rewards if reward.match_score >= 0.75)
        if len(result.rewards) != 4:
            failures.append(f"{path}: expected 4 reward slots")
        if high_conf < 3:
            failures.append(f"{path}: fewer than 3 accepted fuzzy/exact matches")
        results.append({"sample": str(path), "total_ms": result.total_ms, "high_confidence_or_usable": high_conf})
    payload = {"status": "PASS" if not failures else "FAIL", "sample_count": len(sample_paths), "failures": failures, "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def _sample_images(samples: str) -> list[Path]:
    try:
        root = resolve_project_path(samples)
    except ValueError:
        return []
    if not root.exists():
        return []
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
    return paths[:MAX_SAMPLE_SET_IMAGES]


def _resolved_sample_path_text(samples: str) -> str:
    try:
        return str(resolve_project_path(samples))
    except ValueError as exc:
        return f"BLOCKED: {exc}"
