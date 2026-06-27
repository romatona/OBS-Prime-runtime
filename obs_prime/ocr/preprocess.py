from __future__ import annotations


def describe_preprocess_preset(preset_id: str) -> str:
    if preset_id == "default-korean-ui":
        return "grayscale, contrast boost, sharpen for Korean UI text"
    return f"missing or custom preprocessing preset: {preset_id}"


def preprocess_slot_image(image, preset_id: str = "default-korean-ui"):
    if image is None or preset_id != "default-korean-ui":
        return image
    try:
        from PIL import ImageEnhance, ImageFilter
    except Exception:
        return image
    gray = image.convert("L")
    boosted = ImageEnhance.Contrast(gray).enhance(1.8)
    return boosted.filter(ImageFilter.SHARPEN)
