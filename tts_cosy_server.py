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
import types
from pathlib import Path

# ffmpeg / nvidia-smi 等子进程一律隐藏控制台，避免运行中黑框闪烁。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
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
# 并发度：模型是自回归逐 token 出的，单条请求时 GPU 利用率只有三成多 ——
# 大部分时间耗在等 CPU 侧的取样和 kernel 启动上。同时跑两条能让 GPU 有活干，
# 吞吐接近翻倍（RTF 1.1 → 0.7 上下），代价是显存多两三 GB。
# 设成 1 就退回完全串行。
def _concurrency() -> int:
    try:
        return max(1, min(4, int(os.environ.get("COSY_TTS_CONCURRENCY", "2"))))
    except (TypeError, ValueError):
        return 2


_lock = threading.BoundedSemaphore(_concurrency())   # 同时在跑几条合成
_voice_lock = threading.Lock()                       # 参考特征只算一次，别并发抢
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


# ---------------------------------------------------------------- CUDA Graph 解码
# LLM 是自回归逐 token 的：每步一次 forward，里面是几百个微小算子。Windows 的
# WDDM 逐算子提交开销 ~0.1ms，24 层叠出来每步 50~90ms —— 显卡利用率只有三成，
# 墙上时间全花在"把算子递给显卡"上（实测 GPU 满血 43 TFLOPS、硬件无恙）。
# vLLM 在 Linux 上靠 CUDA Graph 解决同一件事；这里手动捕获"单步解码"成一张图：
# 每步只做一次 fill_ + 一次 graph.replay()，实测 8.8ms/步，比 eager 快约 10 倍。
# 静态 KV cache（StaticCache）让每步形状固定，图才能复用；prompt+正文（prefill）
# 仍走 eager 一次性吃下。多请求并发时图只有一个，解码用锁串行 —— 解码快了 10
# 倍之后串行也远快于以前的两路乱序争抢，flow/hift 照旧可以并行。
class GraphDecoder:
    def __init__(self, llm_obj):
        import torch
        from transformers import StaticCache

        self.torch = torch
        self.llm_obj = llm_obj
        self.inner = llm_obj.llm.model.model          # Qwen2Model 本体
        cfg = self.inner.config
        self.hidden = int(cfg.hidden_size)
        self.max_cache = 900                          # prefill(≤300) + 解码(≤600)
        self.static = StaticCache(config=cfg, max_batch_size=1,
                                  max_cache_len=self.max_cache,
                                  device="cuda", dtype=torch.float32)
        self.static_x = torch.zeros(1, 1, self.hidden, device="cuda")
        self.cpos = torch.zeros(1, dtype=torch.long, device="cuda")
        self.pids = torch.zeros(1, 1, dtype=torch.long, device="cuda")
        self.lock = threading.Lock()                  # 图只有一份，解码串行
        self._capture()

    def _capture(self):
        torch = self.torch
        inner = self.inner

        def body():
            out = inner(inputs_embeds=self.static_x, attention_mask=None,
                        position_ids=self.pids, use_cache=True,
                        past_key_values=self.static,
                        cache_position=self.cpos)
            return out.last_hidden_state

        s = torch.cuda.Stream()
        s.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(s):
            for _ in range(3):
                self.cpos.fill_(0)
                self.pids.fill_(0)
                body()
        torch.cuda.current_stream().wait_stream(s)
        self.cpos.fill_(0)
        self.pids.fill_(0)
        self.graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(self.graph):
            self.out = body()                          # (1,1,hidden)

    def generate(self, text, text_len, prompt_text, prompt_text_len,
                 prompt_speech_token, prompt_speech_token_len, embedding,
                 sampling=25, max_token_text_ratio=20, min_token_text_ratio=2,
                 uuid=""):
        """复刻 Qwen2LM.inference 的语义，解码循环换成 graph replay。"""
        torch = self.torch
        llm_obj = self.llm_obj
        # llm_job 外层挂着 autocast(fp16)：图是按 fp32 权重捕获的，这里必须退出
        with torch.autocast("cuda", enabled=False), torch.inference_mode():
            with self.lock:
                device = text.device
                text_all = torch.concat([prompt_text, text], dim=1)
                text_len_all = text_len + prompt_text_len
                text_emb = llm_obj.llm.model.model.embed_tokens(text_all)
                sos_emb = llm_obj.llm_embedding.weight[llm_obj.sos].reshape(1, 1, -1)
                task_id_emb = llm_obj.llm_embedding.weight[llm_obj.task_id].reshape(1, 1, -1)
                prompt_speech_token_emb = llm_obj.speech_embedding(prompt_speech_token)
                lm_input = torch.concat(
                    [sos_emb, text_emb, task_id_emb, prompt_speech_token_emb], dim=1)
                min_len = int((text_len_all - prompt_text_len) * min_token_text_ratio)
                max_len = int((text_len_all - prompt_text_len) * max_token_text_ratio)
                # prefill：一次性吃下 prompt + 正文，KV 写进静态缓存
                p = int(lm_input.shape[1])
                cpos = torch.arange(p, device=device)
                out = self.inner(inputs_embeds=lm_input, attention_mask=None,
                                 position_ids=cpos.unsqueeze(0), use_cache=True,
                                 past_key_values=self.static,
                                 cache_position=cpos)
                hidden = out.last_hidden_state[:, -1]      # (1,hidden) 末位
                produced, pos = 0, p
                out_tokens = []
                while produced < max_len and pos < self.max_cache:
                    logp = llm_obj.llm_decoder(hidden).log_softmax(dim=-1)
                    top_ids = llm_obj.sampling_ids(
                        logp.squeeze(dim=0), out_tokens, sampling,
                        ignore_eos=produced < min_len)
                    if top_ids in llm_obj.stop_token_ids:
                        break
                    yield top_ids
                    out_tokens.append(top_ids)
                    produced += 1
                    pos += 1
                    if produced >= max_len or pos >= self.max_cache:
                        break
                    # 把刚采出的 token 的 embedding 喂给图，算下一位置的隐状态
                    emb = llm_obj.speech_embedding.weight[top_ids]
                    self.static_x.copy_(emb.reshape(1, 1, self.hidden))
                    self.cpos.fill_(pos)
                    self.pids.fill_(pos)
                    self.graph.replay()
                    hidden = self.out[:, -1]


def setup_graph_decode(engine):
    """把 Qwen2LM.inference 换成 graph 版。失败就保持原样（eager 也能跑）。"""
    if str(os.environ.get("COSY_TTS_GRAPH", "1")).lower() in ("0", "off", "false"):
        log.info("COSY_TTS_GRAPH=0，保持 eager 解码")
        return
    try:
        import torch

        llm_obj = engine.model.llm
        dec = GraphDecoder(llm_obj)
        llm_obj._graph_dec = dec

        def graph_inference(self, text, text_len, prompt_text, prompt_text_len,
                            prompt_speech_token, prompt_speech_token_len,
                            embedding, sampling=25, max_token_text_ratio=20,
                            min_token_text_ratio=2, uuid=""):
            return self._graph_dec.generate(
                text, text_len, prompt_text, prompt_text_len,
                prompt_speech_token, prompt_speech_token_len, embedding,
                sampling=sampling, max_token_text_ratio=max_token_text_ratio,
                min_token_text_ratio=min_token_text_ratio, uuid=uuid)

        llm_obj.inference = types.MethodType(graph_inference, llm_obj)
        _state["graph_decode"] = True
        log.info("CUDA Graph 解码已启用（StaticCache %d 位，捕获完成）", dec.max_cache)
    except Exception:                                     # noqa: BLE001
        _state["graph_decode"] = False
        log.exception("CUDA Graph 解码启用失败，退回 eager")


def setup_fast_sampling(engine):
    """把 ras_sampling 换成数学等价的向量化版。

    原始的 nucleus_sampling 对 65k 词表整体排序，再用 Python 循环挑前 25 个、
    逐个 append 到 list 再转回 tensor —— 每步 6ms 左右，和解码本身一样贵。
    这里 topk(25) + cumsum 向量化，复读惩罚也从"每步造一个 GPU 张量"改成
    纯 Python 比 int（top_ids 本来就是 int）。分布完全一致，只是更快。"""
    if str(os.environ.get("COSY_TTS_FAST_SAMPLING", "1")).lower() in ("0", "off", "false"):
        return
    try:
        import torch

        llm_obj = engine.model.llm

        def fast_sampling(weighted_scores, decoded_tokens, sampling,
                          top_p=0.8, top_k=25, win_size=10, tau_r=0.1):
            vals, idx = weighted_scores.softmax(dim=0).topk(min(top_k, weighted_scores.numel()))
            keep = (vals.cumsum(dim=0) - vals) < top_p     # 与原循环的 cum_prob 判定一致
            vals, idx = vals[keep], idx[keep]
            if vals.numel() == 0:                          # 理论到不了，兜底取最大值
                top_ids = int(weighted_scores.argmax())
            else:
                top_ids = int(idx[vals.multinomial(1, replacement=True)])
            if decoded_tokens:                             # VALL-E 2 的复读惩罚
                rep = sum(1 for t in decoded_tokens[-win_size:] if t == top_ids)
                if rep >= win_size * tau_r:
                    weighted_scores[top_ids] = -float("inf")
                    top_ids = int(weighted_scores.softmax(dim=0)
                                  .multinomial(1, replacement=True))
            return top_ids

        llm_obj.sampling = fast_sampling
        _state["fast_sampling"] = True
        log.info("采样已换成向量化版（数学等价）")
    except Exception:                                      # noqa: BLE001
        _state["fast_sampling"] = False
        log.exception("向量化采样启用失败，保持原版")


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
            setup_fast_sampling(engine)
            setup_graph_decode(engine)
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
                          stderr=subprocess.PIPE, timeout=180,
                          creationflags=NO_WINDOW)
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
        "graph_decode": bool(_state.get("graph_decode")),
        "fast_sampling": bool(_state.get("fast_sampling")),
        "sample_rate": _state["sample_rate"],
        "text_frontend": (engine.frontend.text_frontend if engine else ""),
        "voices": sorted(_state["voices"]) if engine else sorted(voice_catalog()),
    })


def _gpu_clock_mhz() -> int:
    """当前 SM 频率（MHz），拿不到返回 0。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=clocks.sm", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=(0x08000000 if os.name == "nt" else 0)).stdout
        return int(out.strip().splitlines()[0].strip())
    except Exception:                                          # noqa: BLE001
        return 0


@app.post("/warmup")
def warmup():
    engine, error = load_engine()
    if engine is None:
        return jsonify({"ok": False, "error": error}), 500
    boost = _gpu_burst()
    return jsonify({"ok": True, "load_seconds": _state["load_seconds"],
                    "voices": sorted(_state["voices"]), "gpu_boost": boost})


def _gpu_burst(seconds: float = 2.5) -> dict:
    """系统待机/熄屏后，驱动会把显卡按在 500MHz 上下——自回归合成这种
    利用率只有三成的负载根本拉不回来，直接让起播慢一倍多。跑几秒小矩阵
    乘把 DVFS 顶上高频：占用极小，换之后整段合成的高速度。

    例外：开了 CUDA Graph 解码后，解码循环把显卡真正喂饱了（实测合成期间
    自动 boost 到 2600MHz），burst 反而会跟正在解码的第一句抢显卡 —— 跳过。"""
    if _state.get("graph_decode"):
        return {"clk": _gpu_clock_mhz(), "burst": False, "skipped": "graph"}
    before = _gpu_clock_mhz()
    try:
        import torch
    except ImportError:
        return {"clk": before, "burst": False}
    a = torch.randn(2048, 2048, device="cuda", dtype=torch.float16)
    t0 = time.time()
    while time.time() - t0 < seconds:
        for _ in range(20):
            a @ a
        torch.cuda.synchronize()
    after = _gpu_clock_mhz()
    return {"clk": before, "burst": True, "after": after}


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

        with _voice_lock:
            spk_id = prepare_voice(name)
        with _lock:
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
