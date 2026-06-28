from __future__ import annotations


def describe_preprocess_preset(preset_id: str) -> str:
    if preset_id == "default-korean-ui":
        return "grayscale, autocontrast, 2x upscale, contrast boost, sharpen for Korean UI text"
    return f"missing or custom preprocessing preset: {preset_id}"


def preprocess_slot_image(image, preset_id: str = "default-korean-ui"):
    if image is None or preset_id != "default-korean-ui":
        return image
    try:
        from PIL import ImageEnhance, ImageFilter, ImageOps
    except Exception:
        return image
    gray = image.convert("L")
    scaled = gray.resize((max(1, gray.width * 2), max(1, gray.height * 2)))
    normalized = ImageOps.autocontrast(scaled, cutoff=1)
    boosted = ImageEnhance.Contrast(normalized).enhance(2.2)
    sharpened = boosted.filter(ImageFilter.UnsharpMask(radius=1.2, percent=160, threshold=3))
    return sharpened.filter(ImageFilter.SHARPEN)
