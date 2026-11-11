@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PY=%USERPROFILE%\miniconda3\envs\cosyvoice\python.exe
if not exist "%PY%" set PY=%USERPROFILE%\anaconda3\envs\cosyvoice\python.exe
if not exist "%PY%" (
  echo 找不到 cosyvoice 这个 conda 环境。
  echo 先按 README 里「自带一份 CosyVoice2」把环境建好，再回来双击本文件。
  pause
  exit /b 1
)

if not exist ".cosyvoice-src\pretrained_models\CosyVoice2-0.5B\cosyvoice2.yaml" (
  echo 模型权重没下全，缺 .cosyvoice-src\pretrained_models\CosyVoice2-0.5B\cosyvoice2.yaml
  pause
  exit /b 1
)

echo 启动 CosyVoice2（首次加载 15～25 秒，之后常驻显存）…
echo 停止：直接关掉这个窗口
echo.

"%PY%" tts_cosy_server.py --port 8891 --preload
pause
