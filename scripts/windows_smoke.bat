@echo off
setlocal
call "%~dp0..\.venv\Scripts\activate.bat"
if errorlevel 1 exit /b 1
python "%~dp0windows_smoke.py" %*
exit /b %errorlevel%
