from __future__ import annotations


def preserve_current_option(current: str, options: list[str], label: str) -> tuple[list[str], str]:
    """Preserve a saved GUI value even when refreshed options do not contain it."""
    cleaned = [option for option in options if option]
    if not current:
        return cleaned, ""
    if current in cleaned:
        return cleaned, ""
    return [current, *cleaned], f"{label} 누락: 저장된 값 '{current}'을 보존함"
