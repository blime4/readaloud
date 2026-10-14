@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" /min "C:\Users\bo\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "%~dp0clip_read.py" %*
