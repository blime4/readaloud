@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY set "PY=python"

"%PY%" "%~dp0launcher.py" "readaloud://stop"
timeout /t 3 /nobreak >nul
endlocal
