@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".hotkey.pid" (
  echo [i] 没有找到运行中的热键守护
  exit /b 0
)
set /p PID=<.hotkey.pid
echo [.] 结束热键守护 PID %PID%
taskkill /F /PID %PID% >nul 2>&1
del /q ".hotkey.pid" >nul 2>&1
echo [√] 已停止
