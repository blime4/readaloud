"""本机大模型 TTS（CosyVoice2 版）：OpenAI 兼容的 /v1/audio/speech。

和 tts_qwen_server.py 同一套约定，模型不同：
CosyVoice2-0.5B 是 flow matching 架构、非自回归，4060 Ti 上实测
RTF 0.89～1.05（Qwen3-TTS 是 3～7），逐句预取能跟上 —— 这是接它进来的原因。

音色来源
--------
CosyVoice2-0.5B **不带预置音色**（list_available_spks() 是空的），
它只有 zero-shot 克隆，所以每个音色都是一对「参考音频 + 原话」：

  1. 默认音色「默认女声」= 仓库自带的 asset/zero_shot_prompt.wav；
  2. 自己的音色：往 models/cosyvoice-voices/ 丢两个文件
         我的声音.wav   （3～10 秒干净人声，采样率 ≥16k，别超过 30 秒）
         我的声音.txt   （wav 里念的原话，一字不差）
     之后 voice 写 "我的声音" 就用它读书。

参考音频的特征只算一次（add_zero_shot_spk 缓存在内存里），
换音色不用重启，改 wav 才要重启。

启动
----
    conda activate cosyvoice
    python tts_cosy_server.py --port 8891 --preload

依赖见 install-cosy.bat / README「自带一份 CosyVoice2」一节。
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
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / ".cosyvoice-src"
MODEL_DIR = SRC_DIR / "pretrained_models" / "CosyVoice2-0.5B"
VOICE_DIR = BASE_DIR / "models" / "cosyvoice-voices"
WETEXT_DIR = BASE_DIR / "models" / "wetext"
DEFAULT_WAV = SRC_DIR / "asset" / "zero_shot_prompt.wav"
DEFAULT_VOICE = "默认女声"
DEFAULT_PROMPT_TEXT = "希望你以后能够做的比我还好呦。"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(SRC_DIR / "third_party" / "Matcha-TTS") not in sys.path:
    sys.path.insert(0, str(SRC_DIR / "third_party" / "Matcha-TTS"))


def patch_wetext_offline():
    """wetext 默认用 modelscope 的 snapshot_download 拉 fst，那接口在这是 403。

    我们提前把 fst 放到 models/wetext/，把下载函数指过去即可。
    没有 fst 也能跑，只是「10 点」不会读成「十点」。
    """
    if not (WETEXT_DIR / "zh" / "tn" / "tagger.fst").exists():
        return False
    try:
        import wetext.wetext as _wetext

        _wetext.snapshot_download = lambda *a, **k: str(WETEXT_DIR)
        return True
    except Exception:                                     # noqa: BLE001
        return False


patch_wetext_offline()

from flask import Flask, Response, jsonify, request  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

log = logging.getLogger("cosyvoice")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

_state = {
    "engine": None,
    "loading": False,
    "error": "",
    "load_seconds": 0.0,
    "spoken": 0,
    "sample_rate": 24000,
    "voices": {},        # 名字 -> {"wav": Path, "text": str, "ready": bool}
}
_lock = threading.Lock()          # 单卡，合成串行
_engine_lock = threading.Lock()


def voice_catalog() -> dict:
    """扫一遍音色：默认那个 + models/cosyvoice-voices 下的 wav/txt 配对。"""
    out = {}
    if DEFAULT_WAV.exists():
        out[DEFAULT_VOICE] = {"wav": DEFAULT_WAV, "text": DEFAULT_PROMPT_TEXT}
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    for wav in sorted(VOICE_DIR.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.exists():
            log.warning("跳过 %s：缺同名 .txt（克隆要参考音频的原话）", wav.name)
            continue
        text = txt.read_text(encoding="utf-8").strip()
        if not text:
            log.warning("跳过 %s：%s 是空的", wav.name, txt.name)
            continue
        out[wav.stem] = {"wav": wav, "text": text}
    return out


def load_engine(path: str = None, fp16: bool = False):
    """懒加载。失败时把原因留在 _state['error'] 里，接口直接透给前端。"""
    if _state["engine"] is not None:
        return _state["engine"], ""
    with _engine_lock:
        if _state["engine"] is not None:
            return _state["engine"], ""
        path = path or str(MODEL_DIR)
        _state["loading"] = True
        started = time.time()
        try:
            if not (Path(path) / "cosyvoice2.yaml").exists():
                raise RuntimeError(f"模型目录不完整（缺 cosyvoice2.yaml）：{path}")
            import torch
            from cosyvoice.cli.cosyvoice import CosyVoice2

            if not torch.cuda.is_available():
                raise RuntimeError("没有可用的 CUDA 显卡（CPU 上 RTF 会到十几，没法朗读）。")
            log.info("加载 CosyVoice2（%s，fp16=%s）…", path, fp16)
            engine = CosyVoice2(path, load_jit=False, load_trt=False, fp16=fp16)
            _state["engine"] = engine
            _state["sample_rate"] = int(engine.sample_rate)
            _state["load_seconds"] = round(time.time() - started, 1)
            _state["voices"] = voice_catalog()
            log.info("加载完成 %.1fs，采样率 %d，音色 %s，文本前端 %r",
                     _state["load_seconds"], _state["sample_rate"],
                     sorted(_state["voices"]), engine.frontend.text_frontend)
            return engine, ""
        except Exception as error:                        # noqa: BLE001
            _state["error"] = f"{type(error).__name__}: {error}"
            log.exception("加载失败")
            return None, _state["error"]
        finally:
            _state["loading"] = False


def prepare_voice(name: str) -> str:
    """确保这个音色的参考特征已在 spk2info 里，返回可直接用的 zero_shot_spk_id。"""
    engine = _state["engine"]
    if name not in _state["voices"]:
        _state["voices"] = voice_catalog()
    info = _state["voices"].get(name)
    if info is None:
        raise KeyError(name)
    if info.get("ready"):
        return name
    import soundfile as sf

    meta = sf.info(str(info["wav"]))
    seconds = meta.frames / float(meta.samplerate or 1)
    if seconds > 30:
        raise RuntimeError(
            f"参考音频 {info['wav'].name} 有 {seconds:.1f} 秒，CosyVoice 最多吃 30 秒，"
            "截到 3～10 秒更稳。")
    if meta.samplerate < 16000:
        raise RuntimeError(
            f"参考音频 {info['wav'].name} 采样率只有 {meta.samplerate}，至少要 16000。")
    started = time.time()
    # 注意：这里必须传路径字符串，frontend 内部会用 24k/16k 各读一次
    engine.add_zero_shot_spk(info["text"], str(info["wav"]), name)
    info["ready"] = True
    log.info("音色「%s」参考特征就绪（%.2fs，参考音频 %.1fs）",
             name, time.time() - started, seconds)
    return name


def to_mp3(wav_bytes: bytes, sample_rate: int, speed: float = 1.0) -> bytes:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0",
            "-ar", str(sample_rate), "-ac", "1"]
    if abs(speed - 1.0) > 0.01:
        args += ["-filter:a", f"atempo={max(0.5, min(2.0, speed)):.3f}"]
    args += ["-b:a", "72k", "-f", "mp3", "pipe:1"]
    done = subprocess.run(args, input=wav_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=180)
    if done.returncode != 0:
        raise RuntimeError("ffmpeg 转 mp3 失败："
                           + done.stderr.decode("utf-8", "replace")[:200])
    return done.stdout


def wav_bytes(tensor, sample_rate: int) -> bytes:
    import soundfile as sf

    audio = tensor.detach().cpu().float().numpy().squeeze()
    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="WAV")
    return buffer.getvalue()


@app.get("/health")
def health():
    engine = _state["engine"]
    return jsonify({
        "ok": engine is not None,
        "engine": "cosyvoice2",
        "model": "CosyVoice2-0.5B",
        "model_dir": str(MODEL_DIR),
        "loading": _state["loading"],
        "error": _state["error"],
        "load_seconds": _state["load_seconds"],
        "spoken": _state["spoken"],
        "sample_rate": _state["sample_rate"],
        "text_frontend": (engine.frontend.text_frontend if engine else ""),
        "voices": sorted(_state["voices"]) if engine else sorted(voice_catalog()),
    })


@app.post("/warmup")
def warmup():
    engine, error = load_engine()
    if engine is None:
        return jsonify({"ok": False, "error": error}), 500
    return jsonify({"ok": True, "load_seconds": _state["load_seconds"],
                    "voices": sorted(_state["voices"])})


@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [
        {"id": "CosyVoice2-0.5B", "object": "model", "owned_by": "funaudiollm",
         "ready": _state["engine"] is not None},
    ]})


@app.get("/v1/audio/voices")
@app.get("/v1/voices")
def voices():
    if not _state["voices"]:
        _state["voices"] = voice_catalog()
    rows = []
    for name, info in _state["voices"].items():
        rows.append({
            "id": name,
            "name": name,
            "description": ("零样本克隆 · 参考音频 "
                            + str(info["wav"].name if isinstance(info["wav"], Path)
                                  else info["wav"])),
        })
    return jsonify({"object": "list", "voices": rows})


@app.post("/v1/audio/speech")
def speech():
    body = request.get_json(silent=True) or {}
    text = str(body.get("input") or body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "input 为空"}), 400
    if len(text) > 3000:
        text = text[:3000]

    engine, error = load_engine()
    if engine is None:
        return jsonify({"ok": False, "error": error}), 500

    wanted = str(body.get("voice") or "").strip()
    available = sorted(_state["voices"] or voice_catalog())
    name = wanted.split("@", 1)[0] if wanted else DEFAULT_VOICE
    if name not in available:
        # 兜底：引擎里残留的 Qwen 音色名之类，不要直接把朗读打断
        log.warning("没有音色「%s」，改用「%s」（可选：%s）",
                    wanted, available[0] if available else DEFAULT_VOICE, available)
        name = DEFAULT_VOICE if DEFAULT_VOICE in available else (available[0] if available else "")

    try:
        speed = float(body.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))
    fmt = str(body.get("response_format") or "mp3").lower()

    started = time.time()
    try:
        import torch

        with _lock:
            spk_id = prepare_voice(name)
            chunks = [item["tts_speech"] for item in engine.inference_zero_shot(
                text, "", "", zero_shot_spk_id=spk_id, stream=False, speed=speed)]
        if not chunks:
            return jsonify({"ok": False, "error": "模型没有产出音频。"}), 502
        full = chunks[0] if len(chunks) == 1 else torch.cat(chunks, dim=1)
    except KeyError:
        return jsonify({"ok": False, "error": f"没有音色「{name}」。",
                        "available": available}), 400
    except Exception as error:                            # noqa: BLE001
        log.exception("合成失败")
        return jsonify({"ok": False, "error": f"{type(error).__name__}: {error}"}), 502

    try:
        raw = wav_bytes(full, _state["sample_rate"])
        if fmt in ("wav", "pcm"):
            payload, mimetype = raw, "audio/wav"
        else:
            payload, mimetype = to_mp3(raw, _state["sample_rate"]), "audio/mpeg"
    except Exception as error:                            # noqa: BLE001
        return jsonify({"ok": False, "error": str(error)}), 500

    took = time.time() - started
    audio_sec = full.shape[-1] / max(1, _state["sample_rate"])
    _state["spoken"] += 1
    log.info("合成 %d 字 / %s / %.1f 秒音频 → %.1f 秒（RTF %.2f）",
             len(text), name, audio_sec, took, took / max(0.1, audio_sec))
    resp = Response(payload, mimetype=mimetype)
    resp.headers["X-ReadAloud-Synth-Seconds"] = f"{took:.2f}"
    # HTTP 头只能 latin-1，音色名是中文，百分号编码后再放
    resp.headers["X-ReadAloud-Voice"] = quote(name, safe="")
    resp.headers["X-ReadAloud-Audio-Seconds"] = f"{audio_sec:.2f}"
    return resp


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"ok": False, "error": "请求体太大"}), 413


def main() -> int:
    parser = argparse.ArgumentParser(description="CosyVoice2 的 OpenAI 兼容服务")
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default=str(MODEL_DIR))
    parser.add_argument("--fp16", action="store_true",
                        help="半精度（实测在这张卡上更慢，默认关）")
    parser.add_argument("--preload", action="store_true")
    args = parser.parse_args()
    if args.preload:
        _, error = load_engine(args.model, args.fp16)
        if error:
            log.error("预加载失败：%s", error)
    log.info("CosyVoice2 服务 http://%s:%s/  （模型 %s）", args.host, args.port, args.model)
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
