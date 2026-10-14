@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ReadAloud 安装

echo ============================================
echo    ReadAloud 本机朗读引擎 · 一键安装
echo ============================================
echo   装好之后，朗读 / 总结 / 润色 全部在浏览器里操作。
echo.

rem ---------- 1/4 找 Python ----------
set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  echo [x] 没找到 Python。
  echo     请先安装 Python 3.10+ ：https://www.python.org/downloads/
  echo     安装时记得勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
echo [1/4] Python: %PY%
"%PY%" -V

rem ---------- 2/4 虚拟环境 ----------
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [2/4] 创建独立运行环境 .venv ...
  "%PY%" -m venv "%~dp0.venv"
) else (
  echo [2/4] 运行环境已存在，跳过
)
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [x] 虚拟环境创建失败。
  pause
  exit /b 1
)

rem ---------- 3/4 依赖 ----------
echo [3/4] 安装依赖（edge-tts / flask / requests）...
"%VPY%" -m pip install -q --upgrade pip
"%VPY%" -m pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo     默认镜像没装成，改用官方 PyPI 重试...
  "%VPY%" -m pip install -q -i https://pypi.org/simple -r "%~dp0requirements.txt"
)
if errorlevel 1 (
  echo [x] 依赖安装失败 —— 多半是网络问题，挂上代理再试一次。
  pause
  exit /b 1
)
"%VPY%" -c "import flask, requests, edge_tts; print('      依赖就绪')"

rem ---------- 4/4 协议 ----------
echo [4/4] 注册 readaloud:// 协议（让网页能一键启动它）...
"%VPY%" "%~dp0setup_protocol.py" --install

echo.
echo ============================================
echo    安装完成
echo ============================================
echo   · 现在可以在网页上点「一键启动本机引擎」
echo   · 也可以随时双击 start-server.bat 手动启动
echo.
set /p ASK=现在就启动并打开工作台吗？(Y/n)
if /i "%ASK%"=="n" goto :done
"%VPY%" "%~dp0launcher.py" "readaloud://open"

:done
echo.
pause
