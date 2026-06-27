from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import Rect
from .paths import resolve_project_path


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def pass_(cls, warnings: list[str] | None = None) -> "ValidationResult":
        return cls(True, [], warnings or [])

    @classmethod
    def fail(cls, errors: list[str], warnings: list[str] | None = None) -> "ValidationResult":
        return cls(False, errors, warnings or [])


def validate_threshold(value: str | float) -> ValidationResult:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return ValidationResult.fail(["임계값은 숫자여야 함"])
    if not 0.0 <= threshold <= 1.0:
        return ValidationResult.fail(["임계값은 0.0부터 1.0 사이여야 함"])
    return ValidationResult.pass_()


def validate_positive_int(value: str | int, name: str) -> ValidationResult:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return ValidationResult.fail([f"{name}은 정수여야 함"])
    if parsed <= 0:
        return ValidationResult.fail([f"{name}은 양수여야 함"])
    return ValidationResult.pass_()


def validate_int_range(value: str | int, name: str, minimum: int, maximum: int) -> ValidationResult:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return ValidationResult.fail([f"{name}은 정수여야 함"])
    if not minimum <= parsed <= maximum:
        return ValidationResult.fail([f"{name}은 {minimum}부터 {maximum} 사이여야 함"])
    return ValidationResult.pass_()


def validate_rects_in_bounds(rects: list[Rect], width: int, height: int) -> ValidationResult:
    errors: list[str] = []
    for index, rect in enumerate(rects, start=1):
        if rect.x < 0 or rect.y < 0:
            errors.append(f"{index}번 칸의 x/y가 음수임")
        if rect.w <= 0 or rect.h <= 0:
            errors.append(f"{index}번 칸의 w/h가 양수가 아님")
        if rect.x + rect.w > width or rect.y + rect.h > height:
            errors.append(f"{index}번 칸이 화면 범위를 벗어남")
    return ValidationResult.fail(errors) if errors else ValidationResult.pass_()


def validate_existing_file(path: str, name: str) -> ValidationResult:
    if not path:
        return ValidationResult.fail([f"{name}이 필요함"])
    try:
        target = resolve_project_path(path)
    except ValueError as exc:
        return ValidationResult.fail([f"{name} 경로가 프로젝트 밖임: {exc}"])
    if not target.exists():
        return ValidationResult.fail([f"{name}이 존재하지 않음: {path}"])
    return ValidationResult.pass_()


def validate_existing_dir(path: str, name: str) -> ValidationResult:
    if not path:
        return ValidationResult.fail([f"{name}이 필요함"])
    try:
        p = resolve_project_path(path)
    except ValueError as exc:
        return ValidationResult.fail([f"{name} 경로가 프로젝트 밖임: {exc}"])
    if not p.exists():
        return ValidationResult.fail([f"{name}이 존재하지 않음: {path}"])
    if not p.is_dir():
        return ValidationResult.fail([f"{name}은 폴더여야 함: {path}"])
    return ValidationResult.pass_()


def validate_capture_config(capture_cfg: dict) -> ValidationResult:
    mode = str(capture_cfg.get("mode", "sample_image"))
    if mode == "sample_image":
        sample_path = str(capture_cfg.get("sample_image_path", ""))
        if not sample_path:
            return ValidationResult.pass_(["샘플 경로가 비어 있어 가상 MVP 샘플 사용"])
        return validate_existing_file(sample_path, "샘플 이미지")
    if mode == "screen":
        monitor_cfg = capture_cfg.get("monitor_index", 0)
        try:
            monitor = int(monitor_cfg)
        except (TypeError, ValueError):
            return ValidationResult.fail(["모니터 번호는 정수여야 함"])
        if monitor < 0:
            return ValidationResult.fail(["모니터 번호는 0 이상이어야 함"])
        return ValidationResult.pass_()
    return ValidationResult.fail([f"지원하지 않는 캡처 모드: {mode}"])
