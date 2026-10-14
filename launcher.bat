@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem readaloud:// 协议的落地脚本：网页点「一键启动」时 Windows 会执行这里。
rem 只做一件事 —— 找到 Python，把动作转交给 launcher.py。

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"

if not defined PY (
  echo [x] 没找到 Python。
  echo     请先双击 install.bat 完成一次安装。
  timeout /t 10 >nul
  exit /b 1
)

"%PY%" "%~dp0launcher.py" "%~1"
if errorlevel 1 (
  echo.
  echo [!] 启动失败 —— 详情见 launcher.log / server.log
  echo     若为首次使用，请先双击 install.bat。
  timeout /t 10 >nul
)
endlocal
