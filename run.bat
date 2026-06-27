@echo off
setlocal
cd /d "%~dp0"
set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "BUNDLED_PYW=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
set "TESSERACT_DIR=%ProgramFiles%\Tesseract-OCR"
if exist "%TESSERACT_DIR%\tesseract.exe" (
  set "TESSERACT_CMD=%TESSERACT_DIR%\tesseract.exe"
)
if not defined TESSDATA_PREFIX (
  if exist "%~dp0runtime\tessdata\kor.traineddata" if exist "%~dp0runtime\tessdata\eng.traineddata" (
    set "TESSDATA_PREFIX=%~dp0runtime\tessdata"
  ) else if exist "%LOCALAPPDATA%\tesseract_ocr\tessdata\kor.traineddata" if exist "%LOCALAPPDATA%\tesseract_ocr\tessdata\eng.traineddata" (
    set "TESSDATA_PREFIX=%LOCALAPPDATA%\tesseract_ocr\tessdata"
  )
)
if "%~1"=="" goto launch_gui
if /I "%~1"=="--gui" goto launch_gui
if not exist "%BUNDLED_PY%" goto local_python
"%BUNDLED_PY%" -m obs_prime %*
exit /b %ERRORLEVEL%

:launch_gui
if exist "%BUNDLED_PYW%" (
  start "OBS prime" /normal "%BUNDLED_PYW%" -m obs_prime --gui
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "OBS prime" /normal pythonw -m obs_prime --gui
  exit /b 0
)
if exist "%BUNDLED_PY%" (
  start "OBS prime" /min "%BUNDLED_PY%" -m obs_prime --gui
  exit /b 0
)
where python >nul 2>nul
if not errorlevel 1 (
  start "OBS prime" /min python -m obs_prime --gui
  exit /b 0
)
start "OBS prime" /min py -m obs_prime --gui
exit /b 0

:local_python
where python >nul 2>nul
if errorlevel 1 goto py_launcher
python -m obs_prime %*
exit /b %ERRORLEVEL%

:py_launcher
py -m obs_prime %*
exit /b %ERRORLEVEL%
