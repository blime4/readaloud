@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=python"
"%VPY%" "%~dp0setup_protocol.py" --uninstall
echo.
pause
