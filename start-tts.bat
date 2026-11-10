@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist .tts-venv\Scripts\python.exe (
  echo 还没装环境，先双击 install-tts.bat
  pause
  exit /b 1
)

set HF_ENDPOINT=https://hf-mirror.com
echo 启动 Qwen3-TTS（首次加载约 10 秒，之后常驻显存）…
echo 停止：直接关掉这个窗口
echo.

.tts-venv\Scripts\python.exe tts_qwen_server.py --port 8890 --preload
pause
