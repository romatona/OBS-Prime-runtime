from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "obs_prime"
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
SAMPLES_DIR = PROJECT_ROOT / "samples"
DEBUG_DIR = PROJECT_ROOT / "debug"


def resolve_project_path(path: str | Path, base: Path | None = None) -> Path:
    """Resolve a path string against PROJECT_ROOT and keep it inside the project."""
    return resolve_path(path, base=base or PROJECT_ROOT, root=PROJECT_ROOT)


def resolve_path(
    path: str | Path,
    base: Path | None = None,
    root: Path | None = None,
    *,
    allow_external: bool = False,
) -> Path:
    """Resolve a path and reject escapes from root unless explicitly allowed."""
    base_path = (base or PROJECT_ROOT).resolve()
    root_path = (root or PROJECT_ROOT).resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base_path / candidate
    resolved = candidate.resolve()
    if not allow_external and not is_within_path(resolved, root_path):
        raise ValueError(f"path escapes project root: {path}")
    return resolved


def is_within_project(path: str | Path) -> bool:
    return is_within_path(Path(path).expanduser().resolve(), PROJECT_ROOT.resolve())


def is_within_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_project_dirs() -> None:
    for path in [
        CONFIG_DIR,
        DATA_DIR,
        DATA_DIR / "fixtures",
        DATA_DIR / "market_cache",
        DATA_DIR / "item_wiki",
        DATA_DIR / "market_wiki",
        SAMPLES_DIR / "reward_screens",
        DEBUG_DIR,
        PROJECT_ROOT / "presets" / "detector",
        PROJECT_ROOT / "presets" / "roi",
        PROJECT_ROOT / "presets" / "ocr",
    ]:
        path.mkdir(parents=True, exist_ok=True)
