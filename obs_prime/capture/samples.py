from __future__ import annotations

from pathlib import Path

from ..models import CaptureFrame
from .providers import CaptureProvider
from ..paths import resolve_project_path

MAX_SAMPLE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_SAMPLE_IMAGE_PIXELS = 16_000_000


class SampleCaptureProvider(CaptureProvider):
    def __init__(self, sample_path: str | None = None) -> None:
        self.sample_path = sample_path or ""

    def capture(self) -> CaptureFrame:
        if self.sample_path:
            path = resolve_project_path(self.sample_path)
            if not path.exists():
                raise FileNotFoundError(f"sample image not found: {path}")
            width, height, image = _read_size(path)
            return CaptureFrame("sample_image", str(path), width, height, image)
        image = _build_virtual_reward_image()
        return CaptureFrame("virtual_sample", None, 1920, 1080, image)


def _read_size(path: Path) -> tuple[int, int, object | None]:
    _validate_image_file_size(path)
    try:
        from PIL import Image

        image = Image.open(path)
        validate_image_dimensions(image.width, image.height, "sample image")
        return image.width, image.height, image
    except Exception as exc:
        raise RuntimeError(f"sample image read failed: {path}") from exc


def _validate_image_file_size(path: Path) -> None:
    if path.stat().st_size > MAX_SAMPLE_IMAGE_BYTES:
        raise RuntimeError(f"sample image is too large: {path}")


def validate_image_dimensions(width: int, height: int, label: str = "image") -> None:
    if width <= 0 or height <= 0:
        raise RuntimeError(f"{label} has invalid dimensions: {width}x{height}")
    if width * height > MAX_SAMPLE_IMAGE_PIXELS:
        raise RuntimeError(f"{label} pixel count is too large: {width}x{height}")


def _build_virtual_reward_image():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    image = Image.new("RGB", (1920, 1080), (14, 16, 18))
    draw = ImageDraw.Draw(image)
    slots = [
        (248, 458, 299, 154, "Bronco Prime Barrel"),
        (622, 458, 299, 154, "Lex Prime Receiver"),
        (996, 458, 299, 154, "Forma Blueprint"),
        (1370, 458, 299, 154, "Glaive Prime Blade"),
    ]
    title_font = _load_font(ImageFont, 48)
    slot_font = _load_font(ImageFont, 38)
    draw.text((760, 382), "RELIC REWARD", fill=(240, 220, 120), font=title_font)
    for x, y, w, h, text in slots:
        draw.rounded_rectangle((x, y, x + w, y + h), radius=10, outline=(205, 180, 92), width=3, fill=(25, 28, 32))
        lines = _wrap_text(draw, text, slot_font, w - 32)
        start_y = y + max(12, int((h - len(lines) * 42) / 2))
        for offset, line in enumerate(lines):
            draw.text((x + 16, start_y + offset * 42), line, fill=(245, 245, 245), font=slot_font)
    return image


def _load_font(image_font_module, size: int):
    try:
        return image_font_module.load_default(size=size)
    except TypeError:
        return image_font_module.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def _text_width(draw, text: str, font) -> int:
    try:
        left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
        return right - left
    except Exception:
        return len(text) * 10
