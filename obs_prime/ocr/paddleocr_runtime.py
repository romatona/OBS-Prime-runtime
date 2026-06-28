from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from importlib import metadata
import os
from collections.abc import Iterator

from ..paths import PROJECT_ROOT


@dataclass(frozen=True)
class PaddleOcrRuntimeStatus:
    status: str
    package_version: str
    paddle_version: str
    language: str
    ocr_version: str
    detection_model: str
    recognition_model: str
    error: str = ""


def probe_paddleocr_v5(language: str = "kor+eng") -> PaddleOcrRuntimeStatus:
    package_version = _package_version("paddleocr")
    paddle_version = _package_version("paddlepaddle")
    with paddle_cache_environment():
        try:
            from paddleocr import PaddleOCR
        except Exception as exc:
            return PaddleOcrRuntimeStatus(
                "unavailable",
                package_version,
                paddle_version,
                _paddle_language(language),
                "PP-OCRv5",
                "PP-OCRv5_mobile_det",
                "korean_PP-OCRv5_mobile_rec",
                str(exc),
            )
        try:
            _ = PaddleOCR(
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )
        except TypeError:
            try:
                _ = PaddleOCR(lang=_paddle_language(language), use_angle_cls=False, show_log=False)
            except Exception as exc:
                return _unavailable(package_version, paddle_version, language, exc)
        except Exception as exc:
            return _unavailable(package_version, paddle_version, language, exc)
    return PaddleOcrRuntimeStatus(
        "ready",
        package_version,
        paddle_version,
        _paddle_language(language),
        "PP-OCRv5",
        "PP-OCRv5_mobile_det",
        "korean_PP-OCRv5_mobile_rec",
    )


def _unavailable(package_version: str, paddle_version: str, language: str, exc: Exception) -> PaddleOcrRuntimeStatus:
    return PaddleOcrRuntimeStatus(
        "unavailable",
        package_version,
        paddle_version,
        _paddle_language(language),
        "PP-OCRv5",
        "PP-OCRv5_mobile_det",
        "korean_PP-OCRv5_mobile_rec",
        str(exc),
    )


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def _paddle_language(language: str) -> str:
    parts = {part.strip().lower() for part in str(language or "").replace(",", "+").split("+") if part.strip()}
    if "korean" in parts or "kor" in parts or "ko" in parts:
        return "korean"
    return "korean"


def configure_paddle_cache() -> None:
    cache_root = PROJECT_ROOT / "runtime" / "paddle"
    cache_root.mkdir(parents=True, exist_ok=True)
    defaults = {
        "PADDLE_HOME": str(cache_root),
        "PADDLEOCR_HOME": str(cache_root / "ocr"),
        "PADDLEX_HOME": str(cache_root / "paddlex"),
        "XDG_CACHE_HOME": str(cache_root / "xdg"),
        "HOME": str(cache_root / "home"),
        "USERPROFILE": str(cache_root / "home"),
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    }
    for key, value in defaults.items():
        os.environ[key] = value


@contextmanager
def paddle_cache_environment() -> Iterator[None]:
    keys = [
        "PADDLE_HOME",
        "PADDLEOCR_HOME",
        "PADDLEX_HOME",
        "XDG_CACHE_HOME",
        "HOME",
        "USERPROFILE",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK",
    ]
    previous = {key: os.environ.get(key) for key in keys}
    configure_paddle_cache()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
