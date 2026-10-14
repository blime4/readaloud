# -*- coding: utf-8 -*-
"""
ReadAloud · 本地朗读中枢
--------------------------------------------------
一个进程同时给三种用法供能：
  1) 网页播放器      http://127.0.0.1:8756/
  2) Obsidian 插件   （readaloud-bridge，走同一套 API）
  3) 任意位置快捷键  （剪贴板 -> 广播给已打开的播放器 / 自动开页面）

能力：
  * 语音合成：微软 Edge 神经语音（edge-tts），中/英/日多语种，音色自然
  * 文本预处理：剥离 Markdown（标题/表格/代码/链接），切成适合朗读的句子
  * AI 总结：用 Ollama 小模型把长文压成朗读稿
  * AI 润色：改写成口语稿，并给出建议语速/音调（打磨"音色"表达）
  * 结果缓存：同一段文本重复朗读直接命中缓存，二次播放零延迟
"""

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import platform
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import requests
import edge_tts
from flask import Flask, Response, jsonify, request, send_from_directory

# ---------------------------------------------------------------- 基础配置

BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "cache"
TTS_CACHE = CACHE_DIR / "tts"
for d in (CACHE_DIR, TTS_CACHE):
    d.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = BASE / "config.json"

DEFAULT_CONFIG = {
    "port": 8756,
    "ollama_url": "http://127.0.0.1:11434",
    "small_model": "qwen3:4b",      # 总结 / 润色用的快模型
    "quality_model": "qwen3.5:9b",  # 需要更高质量时可切
    "default_voice": "zh-CN-XiaoxiaoNeural",
    "rate": "+8%",
    "pitch": "+0Hz",
    "volume": "+0%",
    "max_len": 160,                 # 每段最大字数
    "auto_polish": False,           # 朗读前是否自动润色
    "cache_limit": 400,             # 最多缓存多少个音频片段
}

ENGINE_VERSION = "1.1.0"

# 页面文件：优先用 docs/（= GitHub Pages 站点目录），本地与线上共用同一份，
# 避免出现"本地能跑、线上是旧版"的分叉。
UI_CANDIDATES = (BASE / "docs" / "index.html", BASE / "static" / "index.html")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:  # 配置损坏时回退默认值，不影响使用
            log.warning("config.json 解析失败，使用默认配置: %s", e)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("配置写入失败: %s", e)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("readaloud")

CFG = load_config()
app = Flask(__name__, static_folder=None)


@app.after_request
def add_cors(resp):
    """让三类调用方都能直接用同一套接口：
       1) 本机程序（Obsidian 插件、剪贴板脚本）
       2) 从磁盘/预览面板打开的本地页面
       3) 部署在 GitHub Pages 上的 HTTPS 页面 —— 它要跨源访问 http://127.0.0.1

    第 3 类会遇到 Chrome 的 Private Network Access 检查，必须显式回
    Access-Control-Allow-Private-Network，否则预检就被拦掉（表现为"连不上"）。
    服务只监听 127.0.0.1，本机之外访问不到，所以放开来源是安全的。
    """
    origin = request.headers.get("Origin")
    resp.headers["Access-Control-Allow-Origin"] = origin or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Max-Age"] = "86400"
    if origin:
        resp.headers["Vary"] = "Origin"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
@app.route("/api/<path:_any>/", methods=["OPTIONS"])
def api_preflight(_any):
    """跨源预检直接放行（服务只听本机回环地址，无需再做鉴权）。"""
    return ("", 204)


# ---------------------------------------------------------------- 文本预处理

_MD_RULES = [
    (re.compile(r"```.*?```", re.S), " "),           # 代码块
    (re.compile(r"`([^`]*)`"), r"\1"),               # 行内代码
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),      # 图片
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),   # 链接
    (re.compile(r"\[\[([^\]|]*)\|([^\]]*)\]\]"), r"\2"),  # Obsidian 双链带别名
    (re.compile(r"\[\[([^\]]*)\]\]"), r"\1"),
    (re.compile(r"<!--.*?-->", re.S), " "),
]

SENT_END = "。！？!?…；;"


def clean_text(raw: str) -> str:
    """把 Markdown / 富文本洗成朗读友好的纯文本。"""
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    for pat, rep in _MD_RULES:
        s = pat.sub(rep, s)
    out = []
    for line in s.split("\n"):
        t = line.strip()
        if not t:
            out.append("")
            continue
        if re.match(r"^\|.*\|$", t):           # 表格行
            continue
        if re.match(r"^[\-=*_~]{3,}$", t):     # 分隔线
            continue
        if t.startswith("[!") or t.startswith("```"):
            continue
        t = re.sub(r"^#{1,6}\s*", "", t)
        t = re.sub(r"^>+\s*", "", t)
        t = re.sub(r"^\s*[-*+]\s+", "", t)
        t = re.sub(r"^\s*\d+[.)]\s+", "", t)
        t = t.replace("**", "").replace("__", "").replace("~~", "")
        out.append(t)
    s = "\n".join(out)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def split_segments(text: str, max_len: int = 160, min_len: int = 12) -> list:
    """按段落 -> 句子 -> 硬切 三级切分，产出一串适合逐段合成/逐段高亮的片段。"""
    segs = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        parts = []
        buf = ""
        for piece in re.split(r"(?<=[%s])(?![%s])" % (SENT_END, SENT_END), para):
            piece = piece.strip()
            if not piece:
                continue
            if len(buf) + len(piece) <= max_len:
                buf += piece
            else:
                if buf:
                    parts.append(buf)
                while len(piece) > max_len:      # 超长句硬切
                    cut = piece[:max_len]
                    for mark in ("，", ",", "、", " "):
                        idx = cut.rfind(mark)
                        if idx > max_len * 0.5:
                            cut = cut[: idx + 1]
                            break
                    parts.append(cut.strip())
                    piece = piece[len(cut):].strip()
                buf = piece
        if buf:
            parts.append(buf)
        # 段内合并过短的碎片（不跨段，保证标题/列表项各自独立成句）
        merged = []
        for s in parts:
            if merged and len(merged[-1]) < min_len and not merged[-1].endswith(tuple(SENT_END)):
                merged[-1] += s
            else:
                merged.append(s)
        segs.extend(merged)
    return [s for s in segs if s.strip()]


# ---------------------------------------------------------------- 语音合成

_voice_cache = {"ts": 0, "data": []}
_voice_lock = threading.Lock()


def list_voices(refresh: bool = False) -> list:
    with _voice_lock:
        if not refresh and _voice_cache["data"] and time.time() - _voice_cache["ts"] < 86400:
            return _voice_cache["data"]
    try:
        raw = asyncio.run(edge_tts.list_voices())
        raw = [v for v in raw if v.get("Locale", "").startswith(("zh-", "en-", "ja-", "ko-"))]
        _voice_cache["data"] = raw
        _voice_cache["ts"] = time.time()
        (CACHE_DIR / "voices.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return raw
    except Exception as e:
        log.warning("获取音色列表失败，尝试读本地缓存: %s", e)
        f = CACHE_DIR / "voices.json"
        if f.exists():
            _voice_cache["data"] = json.loads(f.read_text(encoding="utf-8"))
            _voice_cache["ts"] = time.time()
            return _voice_cache["data"]
        return []


def synth(text: str, voice: str, rate: str, pitch: str, volume: str) -> Path:
    """合成一段语音，带磁盘缓存。"""
    key = hashlib.sha1(f"{voice}|{rate}|{pitch}|{volume}|{text}".encode("utf-8")).hexdigest()[:24]
    path = TTS_CACHE / f"{key}.mp3"
    if path.exists() and path.stat().st_size > 0:
        return path
    tmp = path.with_suffix(".part")
    asyncio.run(
        edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume).save(str(tmp))
    )
    tmp.replace(path)
    _trim_cache()
    return path


def _trim_cache() -> None:
    try:
        files = sorted(TTS_CACHE.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[CFG["cache_limit"]:]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- Ollama

_ollama_models = {"ts": 0, "data": []}


def ollama_models(force: bool = False) -> list:
    if not force and _ollama_models["data"] and time.time() - _ollama_models["ts"] < 30:
        return _ollama_models["data"]
    try:
        r = requests.get(f"{CFG['ollama_url']}/api/tags", timeout=4)
        r.raise_for_status()
        data = r.json().get("models", [])
    except Exception as e:
        log.warning("连接 Ollama 失败: %s", e)
        data = []
    _ollama_models["data"] = data
    _ollama_models["ts"] = time.time()
    return data


def _model_caps(name: str) -> list:
    for m in ollama_models():
        if m.get("name") == name or m.get("model") == name:
            return m.get("capabilities") or []
    return []


def _strip_think(s: str) -> str:
    s = re.sub(r"<think(ing)?>.*?</think(ing)?>", "", s, flags=re.S | re.I)
    return s.strip()


def ollama_chat(system: str, user: str, model: str, json_mode: bool = False,
                temperature: float = 0.4, stream: bool = False):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": stream,
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    # 注意：不要传 think 参数。实测在 Ollama 里给 qwen3 系列传 think=false 反而会把
    # 推理过程写进正文（qwen3:4b 尤为明显），不传才干净。
    if json_mode:
        payload["format"] = "json"
    url = f"{CFG['ollama_url']}/api/chat"
    if not stream:
        r = requests.post(url, json=payload, timeout=300)
        r.raise_for_status()
        return _strip_think(r.json()["message"]["content"])
    r = requests.post(url, json=payload, stream=True, timeout=600)
    r.raise_for_status()

    def gen():
        buf = ""
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            piece = (chunk.get("message") or {}).get("content", "")
            buf += piece
            if chunk.get("done"):
                break
            if piece:
                # 过滤思维链：处于  thinking 内部时不外发
                if " thinking" in buf and "<｜end▁of▁thinking｜>" not in buf:
                    continue
                yield piece
    return gen()


SYS_SUMMARY = (
    "你是一位专业的朗读者撰稿人。把用户给的内容压缩成一段适合朗读的口语稿：\n"
    "1. 只保留核心信息与结论，抓住脉络，不超过原文的三分之一；\n"
    "2. 完全口语化，像在跟人讲话，句子短，逻辑用连接词自然串起来；\n"
    "3. 禁止出现任何 Markdown 标记、标题符号、序号、括号注释、代码；\n"
    "4. 数字、百分比、缩写写成便于朗读的中文（例如 30% -> 百分之三十）；\n"
    "5. 直接输出朗读稿正文，不要任何开场白、解释或前后缀；\n"
    "6. 绝对不要输出你的思考过程、对要求的复述或分析，只输出成品。"
)

SYS_POLISH = (
    "你是一位专业的有声书配音撰稿人，负责把文字打磨成好听的朗读稿。规则：\n"
    "1. 去干净所有 Markdown（#、*、表格、链接、代码围栏、双链符号）；\n"
    "2. 把符号/缩写/数字改写成顺口的中文读法（30% -> 百分之三十，AI -> 人工智能）；\n"
    "3. 长句拆短，补足停顿标点（逗号、顿号、破折号），让呼吸节奏自然；\n"
    "4. 保持原意与信息量，不删观点，不添加原文没有的内容；\n"
    "5. 判断这段文字的语气体感（舒缓/叙述/激昂/技术说明等），给出建议语速 rate "
    "（取值在 -15% 到 +20% 之间）与建议音调 pitch（取值在 -15Hz 到 +15Hz 之间）。\n"
    '只输出 JSON：{"text": "润色后的朗读稿", "rate": "+8%", "pitch": "+0Hz", '
    '"note": "一句话说明语气体感", "title": "不超过12字的短标题"}'
)


# ---------------------------------------------------------------- AI 质量把关

class AIBad(Exception):
    """AI 输出未通过质量检查。"""


COT_MARKERS = ("用户要求", "用户的内容", "用户提供", "用户给我的", "我需要", "首先，",
               "让我们", "分析：", "好的，", "根据要求", "按要求", "思考过程",
               "我来分析", "用户希望")


def looks_like_cot(s: str) -> bool:
    """识别"把思考过程写进正文"的典型症状。"""
    head = (s or "").lstrip()[:160]
    if any(m in head for m in COT_MARKERS):
        return True
    return len(s) > 2500 and s.count("\n") > 15


def pick_models(body: dict) -> list:
    """候选模型链：用户指定/小模型 -> 升配模型。"""
    installed = [m.get("name") for m in ollama_models()]
    primary = body.get("model") or (CFG["quality_model"] if body.get("quality") else CFG["small_model"])
    chain = [primary]
    if primary != CFG["quality_model"]:
        chain.append(CFG["quality_model"])
    chain = [m for m in chain if m in installed]
    if not chain and installed:
        chain = installed[:1]
    if not chain:
        raise AIBad("Ollama 里没有可用模型")
    return chain


def ai_refine(text: str, mode: str, body: dict):
    """跑 AI + 质量把关；不合格会自动换更强的模型重试，全不合格则抛 AIBad。

    返回 (处理后的文本, 音色建议, 实际使用的模型)。
    """
    limit = int(body.get("limit") or 300)
    problems = []
    for model in pick_models(body):
        try:
            if mode == "summarize":
                out = clean_text(ollama_chat(
                    SYS_SUMMARY,
                    f"请把下面内容压缩成朗读稿（目标 {limit} 字以内）：\n\n{text}", model))
                if not out:
                    raise AIBad("输出为空")
                if looks_like_cot(out):
                    raise AIBad("把思考过程写进了正文")
                if len(text) > 200 and len(out) < max(20, len(text) * 0.05):
                    raise AIBad("输出过短，疑似被截断")
                return out, {}, model

            out = ollama_chat(SYS_POLISH, f"请打磨下面这段文字的朗读稿：\n\n{text}",
                              model, json_mode=True, temperature=0.3)
            try:
                obj = json.loads(out)
            except Exception:
                raise AIBad("没有返回合法 JSON")
            polished = clean_text(obj.get("text") or "")
            if not polished:
                raise AIBad("润色结果为空")
            if looks_like_cot(polished):
                raise AIBad("把思考过程写进了正文")
            if len(text) > 150 and len(polished) < len(text) * 0.25:
                raise AIBad(f"只输出了 {len(polished)} 字，疑似截断")
            return polished, {
                "rate": obj.get("rate") or CFG["rate"],
                "pitch": obj.get("pitch") or CFG["pitch"],
                "note": obj.get("note") or "",
                "title": obj.get("title") or "",
            }, model
        except Exception as e:
            log.warning("模型 %s 处理 %s 未通过：%s", model, mode, e)
            problems.append(f"{model}（{e}）")
    raise AIBad("；".join(problems))


# ---------------------------------------------------------------- 播放广播（给剪贴板/热键用）

_subscribers = []
_subs_lock = threading.Lock()


def broadcast(payload: dict) -> int:
    with _subs_lock:
        alive = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
                alive.append(q)
            except Exception:
                pass
        _subscribers[:] = alive
        return len(alive)


# ---------------------------------------------------------------- 本机信息

_host_cache = {"ts": 0, "data": None}


def host_info(force: bool = False) -> dict:
    """探测当前这台机器（页面据此显示"正在用哪台电脑跑"）。结果缓存 60 秒。"""
    if not force and _host_cache["data"] and time.time() - _host_cache["ts"] < 60:
        return _host_cache["data"]
    info = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": re.sub(r"\s+", " ", platform.processor() or "").strip(),
        "cores": os.cpu_count(),
        "ram_gb": None,
        "gpus": [],
    }
    try:                                   # 物理内存：Win32 原生调用，不引第三方库
        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            info["ram_gb"] = round(ms.ullTotalPhys / 1e9, 1)
    except Exception:
        pass
    try:                                   # 显卡：装了 nvidia-smi 才有，没有就留空
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=4)
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if parts and parts[0]:
                info["gpus"].append({"name": parts[0],
                                     "vram": parts[1] if len(parts) > 1 else "",
                                     "driver": parts[2] if len(parts) > 2 else ""})
    except Exception:
        pass
    _host_cache["data"], _host_cache["ts"] = info, time.time()
    return info


def ollama_version() -> str:
    try:
        return requests.get(f"{CFG['ollama_url']}/api/version", timeout=3).json().get("version", "")
    except Exception:
        return ""


def model_list() -> list:
    """当前电脑上 Ollama 已装的模型 —— 页面「模型」下拉直接用它。"""
    out = []
    for m in ollama_models(True):
        det = m.get("details") or {}
        out.append({
            "name": m.get("name"),
            "size_gb": round((m.get("size") or 0) / 1e9, 1),
            "params": det.get("parameter_size", ""),
            "family": det.get("family", ""),
            "quant": det.get("quantization_level", ""),
            "caps": m.get("capabilities") or [],
            "modified": (m.get("modified_at") or "")[:10],
        })
    out.sort(key=lambda x: x["size_gb"])
    return out


# ---------------------------------------------------------------- 路由

@app.get("/")
def index():
    for p in UI_CANDIDATES:
        if p.exists():
            resp = Response(p.read_bytes(), mimetype="text/html; charset=utf-8")
            resp.headers["Cache-Control"] = "no-store"     # 改完刷新即生效，线上本地不分叉
            return resp
    return ("页面文件缺失：docs/index.html", 500)


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/api/ping")
def api_ping():
    """极轻量探活：跨源页面靠它判断"引擎在不在"，不触碰任何外部服务。"""
    return jsonify({"ok": True, "app": "readaloud",
                    "version": ENGINE_VERSION, "port": int(CFG["port"])})


@app.get("/api/engine")
def api_engine():
    """当前电脑与环境：网页据此说明"在哪台机器上跑、能用哪些模型"。"""
    models = ollama_models()
    return jsonify({
        "ok": True,
        "engine": {"version": ENGINE_VERSION, "port": int(CFG["port"])},
        "host": host_info(),
        "ollama": {
            "url": CFG["ollama_url"],
            "version": ollama_version(),
            "online": bool(models),
            "models": model_list(),
        },
    })


@app.get("/api/health")
def health():
    models = ollama_models()
    return jsonify({
        "ok": True,
        "version": ENGINE_VERSION,
        "ollama": bool(models),
        "models": [m.get("name") for m in models],
        "voices": len(list_voices()),
        "clients": len(_subscribers),
        "config": CFG,
    })


@app.get("/api/voices")
def api_voices():
    vs = list_voices(bool(request.args.get("refresh")))
    want = request.args.get("all")
    if want:
        return jsonify(vs)
    curated = [v for v in vs if v.get("Locale", "").startswith("zh-CN")]
    curated += [v for v in vs if v.get("Locale", "") in ("zh-HK", "zh-TW")]
    curated += [v for v in vs if v.get("Locale", "").startswith(("en-US", "ja-JP"))]
    return jsonify([{
        "name": v["ShortName"],
        "locale": v["Locale"],
        "gender": v.get("Gender", ""),
        "friendly": re.sub(r"Neural$", "", v["ShortName"].split("-")[-1]),
        "tags": v.get("VoiceTag", {}).get("VoicePersonalities", []),
    } for v in curated])


@app.get("/api/models")
def api_models():
    return jsonify({"models": model_list(),
                    "small_model": CFG["small_model"],
                    "quality_model": CFG["quality_model"]})


@app.post("/api/config")
def api_config():
    body = request.get_json(silent=True) or {}
    for k in DEFAULT_CONFIG:
        if k in body:
            CFG[k] = body[k]
    save_config(CFG)
    return jsonify({"ok": True, "config": CFG})


@app.post("/api/plan")
def api_plan():
    """只做清洗 + 切句，不调用 AI。"""
    body = request.get_json(silent=True) or {}
    text = clean_text(body.get("text", ""))
    max_len = int(body.get("max_len") or CFG["max_len"])
    segs = split_segments(text, max_len)
    if not segs and text:
        segs = [text]
    return jsonify({"text": text, "segments": segs, "count": len(segs)})


def _guarded_stream(text: str, limit: int, model: str):
    """流式总结：先攒前 160 字做一次质检，发现推理泄漏立刻中止，不把垃圾吐给用户。"""
    gen = ollama_chat(SYS_SUMMARY,
                      f"请把下面内容压缩成朗读稿（目标 {limit} 字以内）：\n\n{text}",
                      model, stream=True)
    buf, early = "", True
    for piece in gen:
        if early:
            buf += piece
            if looks_like_cot(buf):
                yield "（小模型把思考过程写进了正文，已中止。建议切到升配模型，或改用「润色」。）"
                return
            if len(buf) >= 160:
                early = False
                yield buf
                buf = ""
            continue
        yield piece
    if buf:
        yield buf


@app.post("/api/ai")
def api_ai():
    """AI 总结 / 润色。mode=summarize|polish，stream=true（仅总结）时流式返回纯文本。"""
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "summarize")
    text = clean_text(body.get("text", ""))
    if not text:
        return jsonify({"ok": False, "error": "没有可处理的文本"}), 400
    limit = int(body.get("limit") or 300)
    if body.get("stream") and mode == "summarize":
        try:
            model = pick_models(body)[0]
        except AIBad as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        return Response(_guarded_stream(text, limit, model), mimetype="text/plain; charset=utf-8")
    try:
        out_text, style, model = ai_refine(text, mode, body)
    except AIBad as e:
        return jsonify({"ok": False, "error": f"AI 输出未通过质量检查：{e}"}), 502
    if mode == "summarize":
        return jsonify({"ok": True, "mode": mode, "model": model, "text": out_text})
    return jsonify({"ok": True, "mode": mode, "model": model, "text": out_text, **style})


@app.post("/api/prepare")
def api_prepare():
    """一步到位：清洗 -> （可选）AI -> 切句。供 Obsidian 与快捷朗读调用。"""
    body = request.get_json(silent=True) or {}
    ai = body.get("ai") or "none"
    text = clean_text(body.get("text", ""))
    if not text:
        return jsonify({"ok": False, "error": "没有可朗读的文本"}), 400
    style = {
        "voice": body.get("voice") or CFG["default_voice"],
        "rate": body.get("rate") or CFG["rate"],
        "pitch": body.get("pitch") or CFG["pitch"],
        "volume": body.get("volume") or CFG["volume"],
        "note": "", "title": "",
    }
    model = None
    ai_note = ""
    if ai in ("summarize", "polish"):
        try:
            text, style2, model = ai_refine(text, ai, body)
            style.update(style2)
        except Exception as e:
            # AI 不靠谱时绝不阻断朗读：退回原文，并把原因告诉调用方
            log.warning("AI 未通过质量检查，改为朗读原文: %s", e)
            ai_note = f"AI 输出未通过质量检查，已改为朗读原文。原因：{e}"
            ai = "none"
            model = None
    segs = split_segments(text, int(body.get("max_len") or CFG["max_len"])) or [text]
    return jsonify({"ok": True, "text": text, "segments": segs, "style": style,
                    "ai": ai, "ai_note": ai_note, "model": model, "count": len(segs)})


@app.route("/api/tts", methods=["GET", "POST"])
def api_tts():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
    else:
        body = request.args
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 为空"}), 400
    voice = body.get("voice") or CFG["default_voice"]
    rate = body.get("rate") or CFG["rate"]
    pitch = body.get("pitch") or CFG["pitch"]
    volume = body.get("volume") or CFG["volume"]
    try:
        path = synth(text, voice, rate, pitch, volume)
    except Exception as e:
        log.error("语音合成失败: %s", e)
        return jsonify({"ok": False, "error": f"语音合成失败：{e}"}), 502
    resp = send_from_directory(TTS_CACHE, path.name, mimetype="audio/mpeg")
    if body.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="readaloud.mp3"'
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.post("/api/broadcast")
def api_broadcast():
    """把一段文字推送给所有已打开的播放器；没人听就返回 delivered=0。"""
    body = request.get_json(silent=True) or {}
    n = broadcast({
        "text": body.get("text", ""),
        "ai": body.get("ai") or "none",
        "voice": body.get("voice") or "",
        "rate": body.get("rate") or "",
        "pitch": body.get("pitch") or "",
        "auto": body.get("auto", True),
        "ts": time.time(),
    })
    return jsonify({"ok": True, "delivered": n})


@app.get("/api/events")
def api_events():
    q = queue.Queue(maxsize=50)

    def stream():
        with _subs_lock:
            _subscribers.append(q)
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    item = q.get(timeout=20)
                    yield "event: speak\ndata: %s\n\n" % json.dumps(item, ensure_ascii=False)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _subs_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    # 端口优先级：环境变量 > config.json > 默认 8756
    port = int(os.environ.get("READALOUD_PORT") or CFG["port"])
    vs = list_voices()
    log.info("ReadAloud 引擎 v%s", ENGINE_VERSION)
    log.info("音色可用 %d 个；Ollama 模型 %d 个", len(vs), len(ollama_models()))
    log.info("本机 %s | 已监听 http://127.0.0.1:%s/", platform.node(), port)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
