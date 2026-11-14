@echo off
cd /d "%~dp0"
setlocal
if exist ".venv" goto launch
call "%~dp0setup.bat"
if errorlevel 1 exit /b 1
:launch
if not exist ".venv\Scripts\activate.bat" goto failed
call ".venv\Scripts\activate.bat"
if errorlevel 1 goto failed
start "" /b powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8765'"
python -m dashboard
if errorlevel 1 goto failed
exit /b 0
:failed
echo Dashboard failed. Run setup.bat if dependencies are missing.
pause
exit /b 1
