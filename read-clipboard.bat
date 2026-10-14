@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 朗读当前剪贴板内容（也会把结果推给已打开的网页播放器）
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=python"

start "" /min "%VPY%" "%~dp0clip_read.py" %*
