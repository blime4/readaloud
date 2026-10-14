@echo off
chcp 65001 >nul
setlocal
set "PYW=C:\Users\bo\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"
cd /d "%~dp0"

if not exist "%PYW%" (
  echo [x] 找不到 Python 环境: %PYW%
  pause
  exit /b 1
)

if exist "%~dp0.hotkey.pid" (
  echo [i] 热键守护似乎已在运行，先停止旧的...
  call "%~dp0stop-hotkey.bat" >nul 2>&1
  timeout /t 1 /nobreak >nul
)

echo [.] 启动全局热键守护...
start "" "%PYW%" "%~dp0hotkey.py"
timeout /t 2 /nobreak >nul
echo.
echo    Ctrl + Alt + R   朗读剪贴板
echo    Ctrl + Alt + S   总结剪贴板并朗读
echo    Ctrl + Alt + W   润色剪贴板并朗读
echo    Ctrl + Alt + X   打开朗读工作台
echo    Ctrl + Alt + Q   停止朗读
echo.
echo [√] 已启动，日志见 hotkey.log
