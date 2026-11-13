"""本机大模型 TTS：把 Qwen3-TTS 包成 OpenAI 兼容的 /v1/audio/speech。

为什么单独起一个进程
--------------------
朗读引擎（server.py）只认一个约定：往 /v1/audio/speech 发一行字，拿回一段音频。
把模型塞进引擎进程里会让两件事耦合 —— 显存和启动时间都归它管，引擎重启
就要重新加载 3.8 GB 权重。分开之后，模型这边挂着不动，引擎随便重启。

和 SGLang-Omni 的关系
---------------------
SGLang-Omni 提供同样形状的接口，吞吐更高，但它只支持 Linux（NVIDIA 走 Docker）。
这台机器是 Windows + 4060 Ti，装不上；所以先用官方 qwen-tts 原生推理，
接口保持一致 —— 以后换成 `sgl-omni serve` 时，只要把地址指过去就行。

启动
----
    .tts-venv/Scripts/python.exe tts_qwen_server.py --port 8890
首次请求才加载模型（约 10 秒），加载前 /health 会如实说"正在加载"。
"""

import argparse
import io
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ffmpeg 等子进程隐藏控制台，避免黑框闪烁。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 本地权重优先；真要联网时用 HF 官方 + 本机代理（hf-mirror 曾把大文件写一半卡住）
os.environ.setdefault("HF_ENDPOINT", "https://huggingface.co")

from flask import Flask, Response, jsonify, request   # noqa: E402

DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
BASE_DIR = Path(__file__).resolve().parent
LOCAL_MODEL = Path(os.environ.get("QWEN_TTS_MODEL_DIR")
                   or (BASE_DIR / "models" / "Qwen3-TTS-12Hz-1.7B-CustomVoice"))


def default_model() -> str:
    """本地有权重就用本地目录：HF 直连在这台机器上不通，能离线就别联网。"""
    return str(LOCAL_MODEL) if (LOCAL_MODEL / "config.json").exists() else DEFAULT_MODEL


LANGUAGES = ["Auto", "Chinese", "English", "Japanese", "Korean", "German",
             "French", "Russian", "Portuguese", "Spanish", "Italian"]

# 注意：模型 get_supported_speakers() 返回的是全小写 id（serena / uncle_fu …），
# 官方文档里的首字母大写只是展示写法。key 必须对齐小写，否则中文说明全丢、
# 请求里的 "Serena" 也匹配不上，会静默退回默认声线。
SPEAKER_LABELS = {
    "vivian": "明亮、略带锐感的年轻女声（中文）",
    "serena": "温暖、柔和的年轻女声（中文）",
    "uncle_fu": "低沉醇厚的成熟男声（中文）",
    "dylan": "年轻北京男声，清亮自然（中文）",
    "eric": "活泼成都男声，略带沙哑亮感（中文）",
    "ryan": "节奏感强的动态男声（英文）",
    "aiden": "阳光美式男声，中频清晰（英文）",
    "ono_anna": "俏皮日系女声，轻快灵动（日文）",
    "sohee": "温暖韩系女声，情感丰富（韩文）",
}
DEFAULT_VOICE = "serena"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

log = logging.getLogger("qwen-tts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_state = {
    "model": None,
    "model_path": default_model(),
    "loading": False,
    "error": "",
    "load_seconds": 0.0,
    "spoken": 0,
    "audio_seconds": 0.0,
    "synth_seconds": 0.0,
}
_model_lock = threading.Lock()      # 一块 GPU，合成串行，避免两句话抢显存


def load_model(path: str):
    """加载模型；已经加载过就直接返回。返回 (model, error)。"""
    if _state["model"] is not None:
        return _state["model"], ""
    with _model_lock:
        if _state["model"] is not None:
            return _state["model"], ""
        _state["loading"] = True
        _state["error"] = ""
        started = time.time()
        try:
            import torch
            from qwen_tts import Qwen3TTSModel

            if not torch.cuda.is_available():
                raise RuntimeError("没有可用的 CUDA 显卡，这个模型只能在 GPU 上跑。")
            log.info("加载 %s …", path)
            model = Qwen3TTSModel.from_pretrained(
                path, device_map="cuda:0", dtype=torch.bfloat16,
            )
            _state["model"] = model
            _state["model_path"] = path
            _state["load_seconds"] = round(time.time() - started, 1)
            log.info("加载完成，用了 %.1f 秒", _state["load_seconds"])
            return model, ""
        except Exception as error:                       # noqa: BLE001
            _state["error"] = f"{type(error).__name__}: {error}"
            log.exception("加载失败")
            return None, _state["error"]
        finally:
            _state["loading"] = False


def speakers() -> dict:
    """返回 {模型实际 id: 中文说明}。加载前用静态表兜底，面板才不会显示"没有音色"。"""
    model = _state["model"]
    names = None
    if model is not None:
        try:
            names = model.get_supported_speakers()
        except Exception:                                # noqa: BLE001
            names = None
    if not names:
        return dict(SPEAKER_LABELS)
    rows = {}
    for name in names:
        key = str(name).strip()
        rows[key] = SPEAKER_LABELS.get(key.lower(), "")
    return rows


def guess_language(text: str) -> str:
    """中英混排时按"中文字符占比"判语种，比让模型自己猜稳。"""
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if han and han >= latin * 0.15:
        return "Chinese"
    return "English" if latin else "Auto"


def to_mp3(wav_bytes: bytes, sample_rate: int, speed: float = 1.0) -> bytes:
    """wav → mp3。语速用 atempo 调，比重新合成一遍便宜得多。"""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0"]
    if abs(speed - 1.0) > 0.01:
        args += ["-filter:a", f"atempo={max(0.5, min(2.0, speed)):.3f}"]
    args += ["-ar", str(sample_rate), "-ac", "1", "-b:a", "72k", "-f", "mp3", "pipe:1"]
    done = subprocess.run(args, input=wav_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=120,
                          creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError("ffmpeg 转 mp3 失败：" + done.stderr.decode("utf-8", "replace")[:200])
    return done.stdout


def wav_bytes(wavs, sample_rate: int) -> bytes:
    import soundfile as sf

    buffer = io.BytesIO()
    sf.write(buffer, wavs[0], sample_rate, format="WAV")
    return buffer.getvalue()


@app.get("/health")
def health():
    model = _state["model"]
    import torch
    gpu = {}
    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        gpu = {"name": torch.cuda.get_device_name(0),
               "vram_free_gb": round(free / 1024 ** 3, 1),
               "vram_total_gb": round(total / 1024 ** 3, 1)}
    return jsonify({
        "ok": model is not None,
        "engine": "qwen-tts",
        "model": _state["model_path"],
        "loading": _state["loading"],
        "error": _state["error"],
        "load_seconds": _state["load_seconds"],
        "spoken": _state["spoken"],
        "gpu": gpu,
    })


@app.post("/warmup")
def warmup():
    """面板上点「探测」时顺手把模型加载起来，省得第一句话等 10 秒。"""
    model, error = load_model(_state["model_path"])
    if model is None:
        return jsonify({"ok": False, "error": error}), 500
    return jsonify({"ok": True, "load_seconds": _state["load_seconds"]})


@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [
        {"id": _state["model_path"], "object": "model", "owned_by": "qwen",
         "ready": _state["model"] is not None},
    ]})


@app.get("/v1/audio/voices")
@app.get("/v1/voices")
def voices():
    rows = speakers()
    # name 里带上中文说明：主引擎 probe 只读 name 做下拉标签，读 id 做 value。
    return jsonify({"object": "list", "voices": [
        {"id": name, "name": f"{name} · {note}" if note else name,
         "description": note} for name, note in rows.items()
    ]})


@app.post("/v1/audio/speech")
def speech():
    body = request.get_json(silent=True) or {}
    text = str(body.get("input") or body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "input 为空"}), 400
    if len(text) > 4000:
        text = text[:4000]                       # 一句话不该有这么长，防止误传整本书

    model, error = load_model(str(body.get("model") or _state["model_path"]))
    if model is None:
        return jsonify({"ok": False, "error": error}), 500

    voice = str(body.get("voice") or DEFAULT_VOICE).strip()
    known = speakers()
    if voice not in known:
        # 面板里可能填的是首字母大写、中文描述，或别的引擎遗留的音色名
        low = voice.lower()
        voice = (next((k for k in known if k.lower() == low), "")
                 or next((k for k, v in known.items() if voice in v), "")
                 or (DEFAULT_VOICE if DEFAULT_VOICE in known else next(iter(known), DEFAULT_VOICE)))
    language = str(body.get("language") or "").strip() or guess_language(text)
    instruct = str(body.get("instruct") or "").strip()
    if language not in LANGUAGES:
        language = "Auto"
    try:
        speed = float(body.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))
    fmt = str(body.get("response_format") or "mp3").lower()

    started = time.time()
    try:
        with _model_lock:
            kwargs = {"text": text, "language": language, "speaker": voice}
            if instruct:
                kwargs["instruct"] = instruct
            wavs, sample_rate = model.generate_custom_voice(**kwargs)
    except Exception as error:                       # noqa: BLE001
        log.exception("合成失败")
        return jsonify({"ok": False, "error": f"{type(error).__name__}: {error}"}), 502

    try:
        raw = wav_bytes(wavs, sample_rate)
        if fmt in ("wav", "pcm"):
            payload, mimetype = raw, "audio/wav"
        else:
            payload, mimetype = to_mp3(raw, sample_rate, speed), "audio/mpeg"
    except Exception as error:                       # noqa: BLE001
        return jsonify({"ok": False, "error": str(error)}), 500

    took = time.time() - started
    _state["spoken"] += 1
    _state["synth_seconds"] += took
    _state["audio_seconds"] += len(text) / 4.2       # 粗略：中文约每秒 4.2 字
    log.info("合成 %d 字 / %s / %s → %.2f 秒（RTF %.2f）",
             len(text), voice, language, took, took / max(0.1, len(text) / 4.2))
    resp = Response(payload, mimetype=mimetype)
    resp.headers["X-ReadAloud-Synth-Seconds"] = f"{took:.2f}"
    return resp


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"ok": False, "error": "请求体太大"}), 413


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen3-TTS 的 OpenAI 兼容服务")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=None, help="HF 仓库名或本地目录，默认用 models/ 下的权重")
    parser.add_argument("--preload", action="store_true", help="启动时就加载模型")
    args = parser.parse_args()

    _state["model_path"] = args.model or default_model()
    if args.preload:
        _, error = load_model(_state["model_path"])
        if error:
            log.error("预加载失败：%s", error)

    log.info("Qwen3-TTS 服务 http://%s:%s/  （模型 %s）", args.host, args.port, _state["model_path"])
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
