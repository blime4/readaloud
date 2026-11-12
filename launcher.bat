@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ---------------------------------------------------------------------
rem  readaloud:// protocol landing script -- also the double-click starter.
rem  Python lookup order: project .venv -> remembered python.path -> PATH.
rem  (PATH 里常有个 WindowsApps 的商店占位 python.exe，选中它只会打开
rem   微软商店，所以这里显式跳过。)
rem ---------------------------------------------------------------------
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0python.path" set /p PY=<"%~dp0python.path"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul ^| findstr /v /i WindowsApps') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"
if defined PY set "PY=!PY:pythonw.exe=python.exe!"

if not defined PY goto nopython

set "ACT=%~1"
if not defined ACT set "ACT=readaloud://open"

"!PY!" "%~dp0launcher.py" "!ACT!"
if errorlevel 1 goto failed
if "%~1"=="" timeout /t 3 >nul
exit /b 0

:nopython
echo [x] Python not found. Please run install.bat once.
timeout /t 15 >nul
exit /b 1

:failed
echo.
echo [!] Startup failed -- see launcher.log / server.log
echo     First time here? Run install.bat once.
timeout /t 15 >nul
exit /b 1
