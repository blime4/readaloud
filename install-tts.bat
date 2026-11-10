@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo  安装本机大模型 TTS（Qwen3-TTS 1.7B）
echo  需要：NVIDIA 显卡 8GB 以上显存 + Python 3.10+
echo ================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] 没找到 python，请先安装 Python 并加入 PATH
  pause
  exit /b 1
)

rem 复用系统里已有的 torch，省掉 3GB 下载；装不上的话删掉这个目录重跑即可
if not exist .tts-venv\Scripts\python.exe (
  echo [1/3] 建独立环境（复用系统 torch）
  python -m venv --system-site-packages .tts-venv
  if errorlevel 1 ( echo [X] 建环境失败 & pause & exit /b 1 )
) else (
  echo [1/3] 环境已存在，跳过
)

echo [2/3] 安装 qwen-tts
set HF_ENDPOINT=https://hf-mirror.com
.tts-venv\Scripts\python.exe -m pip install -U qwen-tts soundfile
if errorlevel 1 ( echo [X] 安装失败 & pause & exit /b 1 )

echo [3/3] 下载模型权重（约 4.5GB，慢的话耐心等）
.tts-venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download as s; s('Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', local_dir='models/Qwen3-TTS-12Hz-1.7B-CustomVoice')"
if errorlevel 1 ( echo [X] 下载失败，检查网络后重跑本脚本 & pause & exit /b 1 )

echo.
echo 装好了。之后双击 start-tts.bat 启动服务，地址 http://127.0.0.1:8890
pause
