@echo off
rem Called by setup.bat, start-dashboard.bat and update.bat. No setlocal: PATH must change in the caller.
rem Picks up tools installed after this window opened (winget, scoop) and ffmpeg in usual places.
rem The .ps1 is read as text and run as a script block, so the PowerShell execution policy doesn't block it.
set "GTA6_PATH_PS1=%~dp0refresh_path.ps1"
for /f "usebackq delims=" %%P in (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -Command "& ([scriptblock]::Create([IO.File]::ReadAllText($env:GTA6_PATH_PS1)))"`) do set "PATH=%%P"
set "GTA6_PATH_PS1="
exit /b 0
