@echo off
REM ============================================================
REM  bili-scribe launcher (ASCII-only on purpose).
REM  All Chinese user-facing text lives in Python, where the
REM  encoding is actually controllable. Encoding Chinese inside a
REM  .bat is a portability trap: it breaks on non-Chinese Windows.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto run

echo.
echo ================================================================
echo   bili-scribe - first run, setting up
echo ================================================================
echo.
echo [1/3] Creating virtual environment ...

where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -m venv ".venv"
goto after_venv

:try_python
where python >nul 2>nul
if errorlevel 1 goto no_python
python -m venv ".venv"

:after_venv
if not exist "%VENV_PY%" (
  echo.
  echo [X] Could not create the virtual environment.
  echo     Python 3.10+ is required: https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo [2/3] Installing dependencies ...
"%VENV_PY%" -m pip install --upgrade pip -q
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [X] Dependency installation failed. Check your network, then run this file again.
  echo.
  pause
  exit /b 1
)

echo [3/3] Setup complete. Starting ...
echo.

:run
if not exist "config.yaml" if exist "config.example.yaml" copy /y "config.example.yaml" "config.yaml" >nul

"%VENV_PY%" run.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo bili-scribe exited with code %RC%.
  echo Run "python run.py --check" for a detailed self-check.
  echo.
  pause
)
exit /b %RC%

:no_python
echo.
echo [X] Python not found on this machine.
echo.
echo     1. Download Python 3.10 or newer:
echo        https://www.python.org/downloads/
echo     2. During installation, CHECK the box "Add python.exe to PATH".
echo     3. Run this file again.
echo.
pause
exit /b 1
