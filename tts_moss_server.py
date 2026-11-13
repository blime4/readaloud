"""MOSS-TTS-Nano 适配层：把官方 ONNX 推理包成 OpenAI 兼容的 /v1/audio/speech。

朗读引擎（server.py）只认一个约定：POST /v1/audio/speech，收
{model, input, voice, response_format, speed}，回一段音频二进制。
Nano 官方的 `moss-tts-nano serve` 是给浏览器 demo 用的 FastAPI，端口
18083、请求格式没公开；这里直接调它自己的 OnnxTtsRuntime Python API，
顺便把参考音频（voice cloning）、并发锁、wav/mp3 输出都收在一处。

用 moss-nano 环境跑（Python 3.12 + numpy / onnxruntime / soundfile / flask，
不需要 torch —— 见下面 _load_reference_numpy 的说明）：
    .\\miniconda3\\envs\\moss-nano\\python.exe tts_moss_server.py --port 8892 --preload

音色（1.12.1 起）：
    Nano 的 ONNX 权重里自带 18 把官方内置音色（manifest 的 builtin_voices，
    参考音频已预编码成 prompt_audio_codes），传 voice 名字即可切换，不额外耗时的
    参考音频编码；中文 6 把里有「说书」「深夜电台」这类专为长篇朗读准备的声线。
    另外扫描 assets/voices/*.wav，用户丢一段 3~15 秒真人录音进去就是一把新音色。

环境变量：
    MOSS_NANO_DIR      Nano 仓库目录（默认项目同级的 moss-tts-nano）
    MOSS_VOICES_DIR    自定义参考音频目录（默认 assets/voices）
    MOSS_DEFAULT_VOICE 默认音色 id（默认 Lingyu · 深夜电台）
    MOSS_THREADS       onnxruntime 线程数（默认 4）
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import wave
from pathlib import Path

from flask import Flask, Response, jsonify, request

BASE = Path(__file__).resolve().parent
NANO_DIR = Path(os.environ.get("MOSS_NANO_DIR") or (BASE.parent / "moss-tts-nano")).resolve()
VOICES_DIR = Path(os.environ.get("MOSS_VOICES_DIR") or (BASE / "assets" / "voices")).resolve()
LEGACY_REF = (BASE / "assets" / "reference_zh.wav").resolve()
THREADS = int(os.environ.get("MOSS_THREADS") or 4)
# 本机的 CUDA 12 DLL 由 cosyvoice 环境的 torch 自带（没装独立 CUDA Toolkit）。
# ONNX Runtime 的 CUDA provider 要能 LoadLibrary 到它们，PATH 与 add_dll_directory
# 都得给，且必须在 import onnxruntime 之前 —— 实测只给其中一个会报 126。
CUDA_DLL = (Path.home() / "miniconda3" / "envs" / "cosyvoice" / "Lib"
            / "site-packages" / "torch" / "lib")
if CUDA_DLL.is_dir():
    os.environ["PATH"] = str(CUDA_DLL) + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(str(CUDA_DLL))
    except (AttributeError, OSError):
        pass
MODEL = "MOSS-TTS-Nano-100M-ONNX"
# 中文 6 把里「深夜电台」最贴长篇听书，且延续原来的女声（不擅自换成男声）。
DEFAULT_VOICE = (os.environ.get("MOSS_DEFAULT_VOICE") or "Lingyu").strip()
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_GROUP_CN = {"Chinese Male": "中文男声", "Chinese Female": "中文女声",
             "English Male": "英文男声", "English Female": "英文女声",
             "Japanese Female": "日文女声", "Japanese Male": "日文男声"}


def _catalog() -> list[dict]:
    """官方内置音色 + 旧版兼容音色 + 用户自建的克隆音色。"""
    rows: list[dict] = []

    # 1) 官方内置：manifest 里的 prompt_audio_codes 是预编码好的，选它不用每句
    #    重跑 codec_encode（实测省约 1.2s/句），音色也比二次生成的克隆干净。
    for mf in sorted((NANO_DIR / "models").glob("**/browser_poc_manifest.json")):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:                                           # noqa: BLE001
            continue
        for v in data.get("builtin_voices") or []:
            vid = str(v.get("voice") or "").strip()
            if not vid or any(r["id"] == vid for r in rows):
                continue
            disp = str(v.get("display_name") or vid)
            tag = disp.split(" ", 1)[-1] if disp[:3] in ("CN ", "EN ", "JP ") else disp
            group = str(v.get("group") or "")
            rows.append({"id": vid, "group": group, "ref": None, "builtin": True,
                         "name": f"{tag} · {_GROUP_CN.get(group, group or '其他')}（{vid}）"})
        if rows:
            break

    # 2) 1.12.0 的老配置仍写 readaloud-zh，留一把兼容音色免得切过来报错。
    if LEGACY_REF.is_file():
        rows.append({"id": "readaloud-zh", "group": "自定义", "ref": LEGACY_REF,
                     "builtin": False, "name": "ReadAloud 女声 · 旧版克隆"})

    # 3) 自定义：往 assets/voices/ 丢 3~15 秒干净人声 wav，刷新服务即多一把音色。
    #    空目录不进 git，服务起来时顺手建好，用户不用自己 mkdir。
    try:
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if VOICES_DIR.is_dir():
        for w in sorted(VOICES_DIR.glob("*.wav")):
            rows.append({"id": "ref:" + w.stem, "group": "自定义", "ref": w,
                         "builtin": False, "name": f"自定义 · {w.stem}"})
    return rows


CATALOG = _catalog()


def _resolve_voice(vid: str) -> dict:
    """未知/已删除的音色静默回退默认，别让整本书因为一个旧配置停在那句。"""
    want = str(vid or "").strip().lower()
    if want:
        for row in CATALOG:
            if row["id"].lower() == want:
                return row
        log.warning("未知音色 %s，回退默认", vid)
    for row in CATALOG:
        if row["id"].lower() == DEFAULT_VOICE.lower():
            return row
    return CATALOG[0] if CATALOG else {"id": "", "name": "", "ref": None,
                                       "builtin": True, "group": ""}

sys.path.insert(0, str(NANO_DIR))

# onnx_tts_runtime.py 顶层 import torch/torchaudio，但 ONNX 推理路径里它们只用于
# 读参考音频（_load_reference_audio）。本机参考音频已按 codec 要求备成 48kHz
# 立体声，下面注入两个空壳模块并用 numpy 版替换那个方法 —— 于是整个服务
# 不需要装 200MB+ 的 torch，纯 numpy + onnxruntime + soundfile 就够。
if "torch" not in sys.modules:
    _t = types.ModuleType("torch")
    _t.float32 = "float32"
    sys.modules["torch"] = _t
    _ta = types.ModuleType("torchaudio")
    _ta.functional = types.SimpleNamespace(resample=lambda w, a, b: w)
    sys.modules["torchaudio"] = _ta


def _load_reference_numpy(self, path):
    import numpy as np
    import soundfile as sf
    data, sr = sf.read(str(Path(path).expanduser().resolve()),
                       dtype="float32", always_2d=True)     # (frames, channels)
    wav = np.ascontiguousarray(data.T)                     # (channels, frames)
    cfg = self.codec_meta["codec_config"]
    target_sr, target_ch = int(cfg["sample_rate"]), int(cfg["channels"])
    if sr != target_sr:
        n = max(1, int(round(wav.shape[1] * target_sr / sr)))
        idx = np.linspace(0, wav.shape[1] - 1, n)
        wav = np.stack([np.interp(idx, np.arange(wav.shape[1]), ch) for ch in wav])
    if wav.shape[0] == 1 and target_ch > 1:
        wav = np.repeat(wav, target_ch, axis=0)
    elif wav.shape[0] > 1 and target_ch == 1:
        wav = wav.mean(axis=0, keepdims=True)
    return wav[None, ...].astype(np.float32, copy=False)   # (1, channels, frames)


app = Flask(__name__)
log = logging.getLogger("moss")

_runtime = None
_init_lock = threading.Lock()
_synth_lock = threading.Lock()      # ONNX session 不是线程安全的，串行最稳
_state = {"loading": False, "error": "", "load_seconds": 0.0, "done": 0,
          "backend": "", "provider": ""}


def _pick_provider() -> str:
    """auto = 能用 CUDA 就用（实测 RTF 0.9 vs CPU 2.3），否则退回 CPU。"""
    want = (os.environ.get("MOSS_EXEC_PROVIDER") or "auto").strip().lower()
    if want in ("cpu", "cuda"):
        return want
    if not CUDA_DLL.is_dir():
        return "cpu"
    try:
        import onnxruntime as ort
        ort.set_default_logger_severity(3)     # 压掉 Memcpy 节点的警告刷屏
        return "cuda" if "CUDAExecutionProvider" in ort.get_available_providers() else "cpu"
    except Exception:                                            # noqa: BLE001
        return "cpu"


def _make_runtime(provider: str):
    from onnx_tts_runtime import OnnxTtsRuntime
    # 用 numpy 版替换 torchaudio.load（见文件头说明）
    OnnxTtsRuntime._load_reference_audio = _load_reference_numpy
    rt = OnnxTtsRuntime(
        model_dir=None,                 # 用仓库默认的 models/（已放好 ONNX 权重）
        thread_count=THREADS,
        max_new_frames=int(os.environ.get("MOSS_MAX_FRAMES") or 375),
        do_sample=True,
        sample_mode="fixed",
        execution_provider=provider,
    )
    # 参考音频编码每句都要做一次（1.2s），音色不变时直接复用 codes。
    _orig_encode = rt.encode_reference_audio
    _ref_cache = {}

    def _encode_cached(path, _orig=_orig_encode, _cache=_ref_cache):
        key = str(path)
        if key not in _cache:
            _cache[key] = _orig(path)
        return _cache[key]

    rt.encode_reference_audio = _encode_cached
    return rt


def _runtime_get():
    global _runtime
    if _runtime is not None:
        return _runtime
    with _init_lock:
        if _runtime is not None:
            return _runtime
        t0 = time.time()
        _state.update(loading=True, error="")
        try:
            if not CATALOG:
                raise FileNotFoundError(
                    f"没有可用音色：既没读到 Nano manifest 内置音色（{NANO_DIR}/models），"
                    f"也找不到参考音频（{LEGACY_REF} 或 {VOICES_DIR}/*.wav）")
            provider = _pick_provider()
            try:
                _runtime = _make_runtime(provider)
            except Exception as e:                              # noqa: BLE001
                if provider != "cuda":
                    raise
                log.warning("CUDA 后端起不来，退回 CPU：%s", e)
                provider = "cpu"
                _runtime = _make_runtime("cpu")
            _state.update(provider=provider,
                          backend="onnx-cuda" if provider == "cuda" else "onnx-cpu",
                          load_seconds=round(time.time() - t0, 1))
            log.info("Nano ONNX 就绪：%s，用时 %.1fs，共 %d 把音色（默认 %s）",
                     _state["backend"], _state["load_seconds"], len(CATALOG),
                     DEFAULT_VOICE)
        except BaseException as e:                              # noqa: BLE001
            _state["error"] = f"{type(e).__name__}: {e}"
            log.exception("Nano 模型加载失败")
        finally:
            _state["loading"] = False
    return _runtime


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg") or ""
    if not exe:
        for cand in (r"C:\Users\bo\miniconda3\Library\bin\ffmpeg.exe",):
            if os.path.exists(cand):
                return cand
    return exe


def _wav_bytes(waveform, sample_rate: int) -> bytes:
    import numpy as np
    audio = np.asarray(waveform, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    pcm = np.round(np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(int(pcm.shape[1]))
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _convert(wav: bytes, fmt: str, speed: float) -> tuple[bytes, str]:
    """默认直接给 WAV；要 MP3 或调了语速才过一次 ffmpeg（很短，不额外训练）。"""
    if fmt != "mp3" and abs(speed - 1.0) < 0.01:
        return wav, "audio/wav"
    exe = _ffmpeg()
    if not exe:
        return wav, "audio/wav"
    args = [exe, "-y", "-loglevel", "error", "-f", "wav", "-i", "pipe:0"]
    if abs(speed - 1.0) >= 0.01:
        # atempo 单级只接受 0.5~2.0，我们界面里最多 ±30%，够用
        args += ["-filter:a", f"atempo={max(0.5, min(2.0, speed)):.3f}"]
    if fmt == "mp3":
        args += ["-codec:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1"]
        mime = "audio/mpeg"
    else:
        # 让 ffmpeg 只吐裸 PCM：它直接写 WAV 到管道时会把 data 长度写成
        # 0xFFFFFFFF（未知长度），浏览器和时长统计都容易算错。
        args += ["-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        mime = "audio/wav"
    try:
        r = subprocess.run(args, input=wav, capture_output=True, timeout=60,
                           creationflags=NO_WINDOW)
        if r.returncode == 0 and r.stdout:
            if fmt == "mp3":
                return r.stdout, mime
            with wave.open(io.BytesIO(wav)) as src:
                ch, sr, sw = src.getnchannels(), src.getframerate(), src.getsampwidth()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setnchannels(ch)
                out.setsampwidth(sw)
                out.setframerate(sr)
                out.writeframes(r.stdout)
            return buf.getvalue(), mime
        log.warning("ffmpeg 转码失败：%s", (r.stderr or b"")[:300])
    except Exception as e:                                      # noqa: BLE001
        log.warning("ffmpeg 不可用：%s", e)
    return wav, "audio/wav"


@app.get("/health")
def health():
    return jsonify({"ok": True, "engine": "moss-tts-nano", "model": MODEL,
                    "loading": bool(_state["loading"]), "error": _state["error"],
                    "ready": _runtime is not None, "load_seconds": _state["load_seconds"],
                    "done": _state["done"], "backend": _state["backend"] or "onnx-auto",
                    "provider": _state["provider"], "default_voice": DEFAULT_VOICE,
                    "voices": [r["id"] for r in CATALOG],
                    "voice_detail": [{k: r[k] for k in ("id", "name", "group", "builtin")}
                                     for r in CATALOG]})


@app.get("/v1/models")
def models():
    return jsonify({"object": "list", "data": [{"id": MODEL, "object": "model",
                                                "owned_by": "openmoss"}]})


@app.get("/v1/voices")
@app.get("/v1/audio/voices")
def voices():
    return jsonify({"voices": [{"id": r["id"], "name": r["name"],
                                "group": r["group"], "builtin": r["builtin"]}
                               for r in CATALOG]})


@app.post("/warmup")
def warmup():
    if _runtime_get() is None:
        return jsonify({"ok": False, "error": _state["error"]}), 500
    return jsonify({"ok": True, "load_seconds": _state["load_seconds"]})


@app.post("/v1/audio/speech")
def speech():
    body = request.get_json(silent=True) or {}
    text = str(body.get("input") or body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "input 为空"}), 400
    if len(text) > 2000:
        text = text[:2000]
    try:
        speed = float(body.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))
    fmt = str(body.get("response_format") or "wav").lower()
    if fmt not in ("wav", "mp3"):
        fmt = "wav"

    rt = _runtime_get()
    if rt is None:
        return jsonify({"ok": False, "error": f"模型没加载好：{_state['error']}"}), 500

    row = _resolve_voice(str(body.get("voice") or ""))
    if row["builtin"]:
        # 官方内置：传名字，runtime 直接用 manifest 里预编码好的 prompt codes
        clone = {"voice": row["id"], "prompt_audio_path": None}
    else:
        # 自定义克隆：runtime 现场编码参考音频（按路径缓存，一句只编一次）
        clone = {"voice": None, "prompt_audio_path": str(row["ref"])}

    t0 = time.time()
    try:
        with _synth_lock:
            result = rt.synthesize(
                text=text,
                **clone,
                sample_mode="fixed",
                do_sample=True,
                streaming=False,              # 实测比流式解码快 ~1.8 倍
                max_new_frames=375,
                voice_clone_max_text_tokens=75,
                enable_wetext=False,          # 不装 pynini：数字读法由主引擎的读音规整负责
                enable_normalize_tts_text=True,
            )
        wav = _wav_bytes(result["waveform"], int(result["sample_rate"]))
        data, mime = _convert(wav, fmt, speed)
        _state["done"] += 1
        log.info("合成 %d 字 %.1fs → %d KB（%s，音色 %s）", len(text),
                 time.time() - t0, len(data) // 1024, mime, row["id"])
        return Response(data, mimetype=mime)
    except Exception as e:                                      # noqa: BLE001
        log.exception("合成失败")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8892)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--preload", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.preload:
        _runtime_get()
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
