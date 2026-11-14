@echo off
rem ===========================================================================
rem  setup.bat - one-time setup for this PC. Safe to run again at any time.
rem    1. finds Python 3.11 (installs it with winget if missing) and creates .venv
rem    2. installs/updates requirements.txt
rem    3. creates the gitignored data folders and fetches the CC0 music bed
rem    4. creates or completes .env (scripts\env_wizard.py asks for secrets, hidden)
rem    5. self-test: imports, ffmpeg, required .env values, dashboard port
rem    6. creates a "GTA6 Dashboard" shortcut on the desktop
rem  It never starts the posting runner/scheduler or the Telegram bot.
rem  Options: --no-pause (used by start-dashboard.bat), --update (used by update.bat)
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title GTA6 setup

set "NOPAUSE="
set "MODE=setup"
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--no-pause" set "NOPAUSE=1"
if /i "%~1"=="--update" set "MODE=update"
shift
goto :parse_args
:args_done

rem Relative paths on purpose: every launcher runs from the repo root, and this avoids quoting bugs.
set "VENV_PY=.venv\Scripts\python.exe"
set "RESULT=PASS"
set "PROBLEMS="
set "MANUAL="

echo.
echo  GTA6-Creativity %MODE%
echo  -------------------------------------------------------------
call scripts\win\refresh_path.bat

rem ---- 1. Python and .venv ---------------------------------------------------
echo.
echo [1/6] Python 3.11 virtual environment (.venv)
if not exist "%VENV_PY%" goto :venv_missing
"%VENV_PY%" -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1
if not errorlevel 1 goto :venv_ok
echo       .venv is broken or uses an unsupported Python version. Recreating it.
rmdir /s /q ".venv"
:venv_missing
call :find_python
if defined BASE_PY goto :make_venv
echo       No Python 3.11 or 3.12 found. Installing Python 3.11 with winget, about a minute...
call :install_python
call scripts\win\refresh_path.bat
call :find_python
if defined BASE_PY goto :make_venv
call :fail "Python 3.11 is not installed"
set "MANUAL=%MANUAL% [Install Python 3.11 from https://www.python.org/downloads/release/python-3119/ then run setup.bat again]"
goto :summary
:make_venv
echo       Creating .venv with: %BASE_PY%
%BASE_PY% -m venv .venv
if exist "%VENV_PY%" goto :venv_ok
call :fail "could not create .venv"
goto :summary
:venv_ok
"%VENV_PY%" -c "import platform; print('      OK: Python', platform.python_version())"

rem ---- 2. Python packages ----------------------------------------------------
echo.
echo [2/6] Python packages from requirements.txt (first run downloads a few hundred MB)
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  call :fail "pip install -r requirements.txt"
  goto :folders
)
rem yt-dlp has to stay current or YouTube downloads break; everything else stays as installed.
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade yt-dlp
echo       OK
rem The repo doesn't use Playwright or Node, so there are no browsers or npm packages to install.

rem ---- 3. Data folders and assets --------------------------------------------
:folders
echo.
echo [3/6] Data folders and assets
for %%D in (media\downloads media\cuts media\audio media\renders media\previews media\subtitles media\dry_run assets\models assets\music logs) do if not exist "%%D\" mkdir "%%D"
echo       OK: media\, assets\models\, logs\ (all gitignored)
"%VENV_PY%" scripts\fetch_assets.py >logs\fetch_assets.log 2>&1
if errorlevel 1 (
  echo       [WARN] Asset check failed. Details: logs\fetch_assets.log
) else (
  echo       OK: assets verified. Details: logs\fetch_assets.log
)

rem ---- 4. .env ---------------------------------------------------------------
echo.
echo [4/6] Settings and secrets (.env)
if "%MODE%"=="update" (
  "%VENV_PY%" scripts\env_wizard.py --new-only
) else (
  "%VENV_PY%" scripts\env_wizard.py
)

rem ---- 5. Self-test ----------------------------------------------------------
echo.
echo [5/6] Self-test
"%VENV_PY%" scripts\launcher_tools.py selftest
if errorlevel 1 set "RESULT=FAIL"
"%VENV_PY%" -c "import shutil,sys; sys.exit(0 if shutil.which('ffmpeg') else 1)" >nul 2>&1
if errorlevel 1 set "MANUAL=%MANUAL% [Install FFmpeg: winget install Gyan.FFmpeg -- or download a full build from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH]"

rem ---- 6. Desktop shortcut (skipped silently if it fails) ---------------------
echo.
echo [6/6] Desktop shortcut
set "GTA6_TARGET=%~dp0start-dashboard.bat"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "try { $d = [Environment]::GetFolderPath('Desktop'); $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'GTA6 Dashboard.lnk')); $s.TargetPath = $env:GTA6_TARGET; $s.WorkingDirectory = Split-Path -Parent $env:GTA6_TARGET; $s.Description = 'Open the GTA6 pipeline dashboard'; $s.Save(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 echo       OK: "GTA6 Dashboard" on your desktop

:summary
echo.
echo  =============================================================
if "%RESULT%"=="PASS" (
  echo   SETUP: PASS
  echo   Next: double-click start-dashboard.bat or the "GTA6 Dashboard" desktop shortcut.
) else (
  echo   SETUP: FAIL - see the [FAIL] lines above.
)
if defined PROBLEMS echo   Failed steps:%PROBLEMS%
if defined MANUAL echo   To do by hand:%MANUAL%
echo  =============================================================
echo.
set "RC=0"
if not "%RESULT%"=="PASS" set "RC=1"
if not defined NOPAUSE pause
endlocal & exit /b %RC%


rem ---- subroutines ---------------------------------------------------------------

:fail
echo       [FAIL] %~1
set "RESULT=FAIL"
set "PROBLEMS=%PROBLEMS% [%~1]"
exit /b 0

:find_python
rem Sets BASE_PY to a Python 3.11 or 3.12 command (PLAN.md: Kokoro's wheels don't install on 3.14).
set "BASE_PY="
for %%V in (3.11 3.12) do if not defined BASE_PY call :try_python py -%%V
for %%D in ("%LOCALAPPDATA%\Programs\Python\Python311" "%ProgramFiles%\Python311" "%LOCALAPPDATA%\Programs\Python\Python312" "%ProgramFiles%\Python312") do if not defined BASE_PY if exist "%%~D\python.exe" call :try_python "%%~D\python.exe"
if not defined BASE_PY call :try_python python
exit /b 0

:try_python
%* -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,12) else 1)" <nul >nul 2>&1
if not errorlevel 1 set "BASE_PY=%*"
exit /b 0

:install_python
where winget >nul 2>&1
if errorlevel 1 (
  echo       winget is not available on this PC.
  exit /b 1
)
rem Per-user install with no py launcher and no PATH change: needs no admin rights, so no UAC prompt.
winget install --id Python.Python.3.11 -e --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity --override "/quiet /norestart InstallAllUsers=0 Include_launcher=0 InstallLauncherAllUsers=0 PrependPath=0 Include_test=0 Shortcuts=0"
exit /b 0
