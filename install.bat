@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo [OBS prime] install/check started
echo [INFO] Default OCR engine: PaddleOCR v5 Korean
echo [INFO] Verified OCR stack: paddleocr 3.7.0 / paddlepaddle 3.3.1 / aiohttp 3.9.5

call :find_python
if not defined PYTHON_EXE (
  echo [ERROR] Python 3 was not found. Install Python 3.11+ and run install.bat again.
  exit /b 1
)
echo [OK] Python: %PYTHON_EXE%

call :configure_paddle_cache

"%PYTHON_EXE%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo [INFO] pip not found. Trying ensurepip...
  "%PYTHON_EXE%" -m ensurepip --upgrade
  if errorlevel 1 (
    echo [ERROR] pip setup failed.
    exit /b 1
  )
)

echo [INFO] Installing verified runtime packages from requirements.txt
"%PYTHON_EXE%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Python package install failed.
  exit /b 1
)

call :check_paddleocr
if errorlevel 1 (
  echo [ERROR] PaddleOCR stack check failed after install.
  echo [INFO] Try running this command manually:
  echo   "%PYTHON_EXE%" -m pip install -r requirements.txt
  exit /b 1
)

echo [INFO] Running OBS prime config check
"%PYTHON_EXE%" -m obs_prime --config-check
if errorlevel 1 (
  echo [ERROR] OBS prime config check failed.
  exit /b 1
)

echo [INFO] Running default OCR readiness check
"%PYTHON_EXE%" -m obs_prime --ocr-check
if errorlevel 1 (
  echo [ERROR] PaddleOCR readiness check failed.
  echo [INFO] The first run may need network access to download PP-OCRv5 model files.
  exit /b 1
)

echo [DONE] OBS prime install/check completed
exit /b 0

:find_python
set "PYTHON_EXE="
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
    set "PYTHON_EXE=%%P"
    exit /b 0
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do (
    set "PYTHON_EXE=%%P"
    exit /b 0
  )
)
exit /b 0

:check_paddleocr
"%PYTHON_EXE%" -c "import importlib.metadata as m; import paddleocr, paddle, aiohttp; expected={'paddleocr':'3.7.0','paddlepaddle':'3.3.1','aiohttp':'3.9.5'}; actual={name:m.version(name) for name in expected}; print('[OK] PaddleOCR stack: ' + ', '.join(f'{k} {v}' for k,v in actual.items())); raise SystemExit(0 if actual == expected else 1)"
exit /b %ERRORLEVEL%

:configure_paddle_cache
set "PADDLE_ROOT=%~dp0runtime\paddle"
set "PADDLE_HOME=%PADDLE_ROOT%"
set "PADDLEOCR_HOME=%PADDLE_ROOT%\ocr"
set "PADDLEX_HOME=%PADDLE_ROOT%\paddlex"
set "XDG_CACHE_HOME=%PADDLE_ROOT%\xdg"
set "HOME=%PADDLE_ROOT%\home"
set "USERPROFILE=%PADDLE_ROOT%\home"
set "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True"
if not exist "%PADDLE_HOME%" mkdir "%PADDLE_HOME%" >nul 2>nul
if not exist "%PADDLEOCR_HOME%" mkdir "%PADDLEOCR_HOME%" >nul 2>nul
if not exist "%PADDLEX_HOME%" mkdir "%PADDLEX_HOME%" >nul 2>nul
if not exist "%XDG_CACHE_HOME%" mkdir "%XDG_CACHE_HOME%" >nul 2>nul
if not exist "%HOME%" mkdir "%HOME%" >nul 2>nul
exit /b 0
