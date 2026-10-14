@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VPY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%VPY%" set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=pythonw"

if exist "%~dp0.hotkey.pid" (
  echo [i] 热键守护已在运行，先停掉旧的...
  call "%~dp0stop-hotkey.bat" >nul 2>&1
  timeout /t 1 /nobreak >nul
)

echo [.] 启动全局热键守护...
start "" "%VPY%" "%~dp0hotkey.py"
timeout /t 2 /nobreak >nul
echo.
echo    Ctrl + Alt + R   朗读剪贴板
echo    Ctrl + Alt + S   总结剪贴板并朗读
echo    Ctrl + Alt + W   润色剪贴板并朗读
echo    Ctrl + Alt + X   打开朗读工作台
echo    Ctrl + Alt + Q   停止朗读
echo.
echo [√] 已启动，日志见 hotkey.log
