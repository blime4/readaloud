@echo off
chcp 65001 >nul
setlocal
set "DIR=%~dp0"
set "PYW=C:\Users\bo\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"

echo 设置开机自启（仅当前用户，随时可用 disable-autostart.bat 取消）...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ReadAloudServer /t REG_SZ /d "\"%PYW%\" \"%DIR%server.py\"" /f >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ReadAloudHotkey /t REG_SZ /d "\"%PYW%\" \"%DIR%hotkey.py\"" /f >nul
echo [√] 已写入：
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ReadAloudServer
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ReadAloudHotkey
echo.
echo 下次登录后朗读服务与全局热键会自动在后台运行。
pause
