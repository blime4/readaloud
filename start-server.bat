@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 启动本机引擎并打开工作台（等同于网页上的「一键启动」）
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"

if not defined PY (
  echo [x] 没找到 Python。
  echo     首次使用请先双击 install.bat。
  pause
  exit /b 1
)

"%PY%" "%~dp0launcher.py" "readaloud://open"
if errorlevel 1 pause
endlocal
