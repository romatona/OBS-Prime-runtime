from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import PROJECT_ROOT


@dataclass(frozen=True)
class TesseractRuntimeStatus:
    status: str
    executable: str | None
    tessdata_prefix: str | None
    version: str | None
    available_languages: list[str]
    error: str


def configure_pytesseract(pytesseract_module, language: str = "kor+eng", timeout_ms: int = 2500) -> TesseractRuntimeStatus:
    runtime = probe_tesseract(language, timeout_ms)
    if runtime.executable:
        pytesseract_module.pytesseract.tesseract_cmd = runtime.executable
    if runtime.tessdata_prefix:
        os.environ["TESSDATA_PREFIX"] = runtime.tessdata_prefix
    _patch_pytesseract_hidden_subprocess(pytesseract_module)
    return runtime


def probe_tesseract(language: str = "kor+eng", timeout_ms: int = 2500) -> TesseractRuntimeStatus:
    executable = _find_tesseract_executable()
    tessdata_prefix = _find_tessdata_prefix(language)
    if not executable:
        return TesseractRuntimeStatus("unavailable", None, tessdata_prefix, None, [], "tesseract.exe not found")

    version_run = _run_tesseract([str(executable), "--version"], tessdata_prefix, timeout_ms)
    if version_run.returncode != 0:
        return TesseractRuntimeStatus(
            "unavailable",
            str(executable),
            tessdata_prefix,
            None,
            [],
            version_run.error or _compact_output(version_run.stderr or version_run.stdout),
        )
    version = _first_nonempty_line(version_run.stdout or version_run.stderr)

    langs_run = _run_tesseract([str(executable), "--list-langs"], tessdata_prefix, timeout_ms)
    if langs_run.returncode != 0:
        return TesseractRuntimeStatus(
            "unavailable",
            str(executable),
            tessdata_prefix,
            version,
            [],
            langs_run.error or _compact_output(langs_run.stderr or langs_run.stdout),
        )
    available = _parse_languages(langs_run.stdout or langs_run.stderr)
    requested = {part.strip() for part in language.split("+") if part.strip()}
    missing = sorted(requested - set(available))
    if missing:
        return TesseractRuntimeStatus(
            "unavailable",
            str(executable),
            tessdata_prefix,
            version,
            available,
            f"missing tesseract language data: {', '.join(missing)}",
        )
    return TesseractRuntimeStatus("ready", str(executable), tessdata_prefix, version, available, "")


@dataclass(frozen=True)
class _RunResult:
    returncode: int
    stdout: str
    stderr: str
    error: str = ""


def _run_tesseract(command: list[str], tessdata_prefix: str | None, timeout_ms: int) -> _RunResult:
    env = os.environ.copy()
    if tessdata_prefix:
        env["TESSDATA_PREFIX"] = tessdata_prefix
    timeout = max(0.5, timeout_ms / 1000)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
            **_windows_hidden_subprocess_kwargs(),
        )
        return _RunResult(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired:
        return _RunResult(1, "", "", f"tesseract probe timed out after {timeout_ms}ms")
    except OSError as exc:
        return _RunResult(1, "", "", str(exc))


def _find_tesseract_executable() -> Path | None:
    if os.name == "nt":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if not base:
                continue
            candidate = Path(base) / "Tesseract-OCR" / "tesseract.exe"
            if candidate.exists():
                return candidate
    env_value = os.environ.get("TESSERACT_CMD") or os.environ.get("TESSERACT_EXE")
    for value in [env_value, shutil.which("tesseract"), shutil.which("tesseract.exe")]:
        if value:
            path = Path(value)
            if path.is_absolute() and path.exists():
                return path
    return None


def _find_tessdata_prefix(language: str) -> str | None:
    project_runtime = PROJECT_ROOT / "runtime"
    project_tessdata = project_runtime / "tessdata"
    if _has_requested_language_data(project_tessdata, language):
        return str(project_tessdata)
    env_value = os.environ.get("TESSDATA_PREFIX")
    if env_value and _has_requested_language_data(Path(env_value), language):
        return env_value
    executable = _find_tesseract_executable()
    if executable:
        tessdata = executable.parent / "tessdata"
        if _has_requested_language_data(tessdata, language):
            return str(tessdata)
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        local_tessdata = Path(local_appdata) / "tesseract_ocr" / "tessdata"
        if _has_requested_language_data(local_tessdata, language):
            return str(local_tessdata)
    return None


def _has_requested_language_data(root: Path, language: str) -> bool:
    if not root.exists():
        return False
    requested = {part.strip() for part in language.split("+") if part.strip()}
    return all((root / f"{part}.traineddata").exists() for part in requested)


def _parse_languages(output: str) -> list[str]:
    languages: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("list of available languages"):
            continue
        languages.append(stripped)
    return sorted(set(languages))


def _first_nonempty_line(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _compact_output(output: str) -> str:
    return " ".join(output.split())[:500]


def _windows_hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def _patch_pytesseract_hidden_subprocess(pytesseract_module) -> None:
    if os.name != "nt":
        return
    module = getattr(pytesseract_module, "pytesseract", pytesseract_module)
    if getattr(module, "_obs_prime_hidden_subprocess_patch", False):
        return
    original = module.subprocess_args

    def subprocess_args(include_stdout=True):
        kwargs = original(include_stdout=include_stdout)
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = kwargs.get("startupinfo")
        if startupinfo is None:
            startupinfo = subprocess.STARTUPINFO()
            kwargs["startupinfo"] = startupinfo
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        return kwargs

    module.subprocess_args = subprocess_args
    module._obs_prime_hidden_subprocess_patch = True
