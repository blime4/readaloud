@echo off
chcp 65001 >nul
echo 取消开机自启...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ReadAloudServer /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ReadAloudHotkey /f >nul 2>&1
echo [√] 已取消（服务与热键仍可用脚本手动启动）
pause
