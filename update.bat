@echo off
rem ===========================================================================
rem  update.bat - get the latest code and keep this PC's setup in step with it.
rem    1. git pull (fast-forward only) on the branch you're on
rem    2. setup.bat --update: updates packages, asks only for .env settings
rem       that are new since last time, runs the self-test
rem  It never starts the posting runner/scheduler or the Telegram bot.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title GTA6 update

call scripts\win\refresh_path.bat
where git >nul 2>&1
if errorlevel 1 goto :no_git

set "BRANCH="
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
echo.
echo Updating branch %BRANCH% from GitHub...

rem git can replace this very file while it runs. cmd reads a ( ) block completely before
rem running it, so the pull and the hand-off to the freshly pulled setup.bat sit in one block.
(
  git pull --ff-only
  if errorlevel 1 goto :pull_failed
  call "%~dp0setup.bat" --update
  exit /b
)

:pull_failed
echo.
echo  [FAIL] git pull didn't work, so nothing was changed. Common causes:
echo         - no internet connection
echo         - files in this folder were edited locally; ask for help before discarding them
echo         - the branch has no upstream on GitHub
echo.
pause
exit /b 1

:no_git
echo.
echo  [FAIL] git isn't installed. Install it with: winget install Git.Git
echo         then double-click update.bat again.
echo.
pause
exit /b 1
