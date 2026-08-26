@echo off
chcp 65001 >nul
setlocal
title YouMi Agent GUI

rem =============================================================
rem  YouMi Agent GUI launcher
rem
rem  Usage:
rem    gui\start_gui.bat                  Normal start (real LLM engine)
rem    gui\start_gui.bat --mock           Mock mode (preview UI without LLM)
rem    gui\start_gui.bat --port 9000      Custom port (also supports --host)
rem
rem  Notes:
rem    - Python priority: env YOUMI_PYTHON > local fallback path > PATH
rem    - Auto-installs missing dependencies (aiohttp + engine deps)
rem    - Opens default browser ~2s after start; set YOUMI_GUI_NO_BROWSER=1
rem      to disable
rem    - Default port 8766, overridable by env YOUMI_GUI_PORT or --port
rem =============================================================

rem -- Local fallback interpreter (edit to your own python.exe path)
set "PY_FALLBACK=D:\ANACONDA\envs\AI\python.exe"

rem -- Locate repo root (this script lives in gui\)
cd /d "%~dp0.."

rem ---------- 1/3 pick Python interpreter ----------
set "PY=%YOUMI_PYTHON%"
if defined PY goto py_found
if exist "%PY_FALLBACK%" set "PY=%PY_FALLBACK%"
if defined PY goto py_found
where python >nul 2>nul
if errorlevel 1 goto no_python
set "PY=python"

:py_found
echo [1/3] Python : %PY%

rem ---------- 2/3 dependency check (auto-install if missing) ----------
"%PY%" -c "import aiohttp, pydantic, yaml, httpx, websockets" >nul 2>nul
if errorlevel 1 goto install_deps
echo [2/3] Dependencies OK
goto run

:install_deps
echo [2/3] Missing dependencies, installing (requires network)...
"%PY%" -m pip install -r gui\requirements.txt pydantic pyyaml httpx websockets
if errorlevel 1 goto pip_fail

rem ---------- 3/3 start server ----------
:run
set "PORT=8766"
if defined YOUMI_GUI_PORT set "PORT=%YOUMI_GUI_PORT%"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--port" set "PORT=%~2"
if /i "%~1"=="--port" shift
shift
goto parse_args
:args_done
if not defined PORT set "PORT=8766"

echo [3/3] GUI running at http://127.0.0.1:%PORT%
echo        Press Ctrl+C in this window to stop. Add --mock to preview without LLM.
echo.

rem -- open default browser after ~2s
if not defined YOUMI_GUI_NO_BROWSER (
    start "" /min cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:%PORT%"
)

rem -- run server in foreground, logs print to this window
"%PY%" -m gui %*
set "EXITCODE=%ERRORLEVEL%"

echo.
echo GUI exited (code %EXITCODE%).
pause
exit /b %EXITCODE%

:no_python
echo.
echo [ERROR] No usable Python interpreter found:
echo        1. Install Python 3.10+ and add it to PATH, or
echo        2. Set env variable YOUMI_PYTHON to your python.exe, or
echo        3. Edit PY_FALLBACK in this file.
pause
exit /b 1

:pip_fail
echo.
echo [ERROR] Dependency install failed. Check network, or run manually:
echo        pip install -r gui\requirements.txt pydantic pyyaml httpx websockets
pause
exit /b 1
