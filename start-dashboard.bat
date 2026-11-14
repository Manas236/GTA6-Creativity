@echo off
rem ===========================================================================
rem  start-dashboard.bat - double-click this to open the GTA6 dashboard.
rem    - first time on a PC (no .venv or no .env): runs setup.bat first
rem    - dashboard already running: just opens the browser, no second copy
rem    - otherwise starts the dashboard in this window (logs stay visible here)
rem      and opens the browser as soon as it answers
rem  It never starts the posting runner/scheduler or the Telegram bot.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title GTA6 Dashboard
set "VENV_PY=.venv\Scripts\python.exe"

call scripts\win\refresh_path.bat

if not exist "%VENV_PY%" goto :run_setup
if not exist ".env" goto :run_setup
goto :ready

:run_setup
echo This PC isn't set up yet, so setup.bat runs first.
call "%~dp0setup.bat" --no-pause
title GTA6 Dashboard
if exist "%VENV_PY%" goto :ready
echo.
echo  [FAIL] Setup couldn't create the Python environment. Fix what the messages above say,
echo         then double-click start-dashboard.bat again.
pause
exit /b 1

:ready
if not exist ".env" echo  [WARN] No .env yet. The dashboard works without it; run setup.bat to add the pipeline's keys.
set "PORT=8765"
for /f "delims=" %%P in ('%VENV_PY% scripts\launcher_tools.py port') do set "PORT=%%P"
set "URL=http://127.0.0.1:%PORT%/"

%VENV_PY% scripts\launcher_tools.py port-status --port %PORT%
if errorlevel 11 goto :port_taken
if errorlevel 10 goto :already_running

call .venv\Scripts\activate.bat
rem Opens the browser once the server answers; runs in the background with no window.
start "" .venv\Scripts\pythonw.exe scripts\launcher_tools.py wait-open --port %PORT%

echo.
echo  =============================================================================
echo   GTA6 dashboard: %URL%
echo.
echo   Don't run the scheduler on two machines at once.
echo   This window runs only the dashboard. It does not start the posting runner,
echo   the scheduler or the Telegram bot: one bot token polled from two PCs gives
echo   Telegram 409 Conflict, and two schedulers would double-post.
echo.
echo   Leave this window open while you use the dashboard. Close it to stop it.
echo  =============================================================================
echo.
%VENV_PY% -m dashboard --port %PORT%
echo.
echo The dashboard has stopped. If that was unexpected, the error is above.
pause
exit /b 0

:already_running
echo The dashboard is already running. Opening %URL%
start "" "%URL%"
timeout /t 3 /nobreak >nul
exit /b 0

:port_taken
echo.
echo  [FAIL] Port %PORT% is used by another program, not the dashboard.
echo         Close that program, or put another port in .env, for example DASHBOARD_PORT=8766,
echo         then double-click start-dashboard.bat again.
pause
exit /b 1
