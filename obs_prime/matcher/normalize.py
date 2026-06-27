from __future__ import annotations

import re
import unicodedata


CONFUSIONS = {
    "프라임 ": "프라임 ",
    "  ": " ",
    "|": "l",
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").strip().lower()
    for src, dst in CONFUSIONS.items():
        value = value.replace(src, dst)
    value = re.sub(r"[\[\]{}()<>:;,.!?]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()
