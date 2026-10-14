@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 设置开机自启（HKCU，当前用户级，不需要管理员权限）
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=python"

"%VPY%" "%~dp0setup_autostart.py" --enable
echo.
pause
