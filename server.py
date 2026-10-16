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
import html as _html
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

import text_nlp as NLP

# ---------------------------------------------------------------- 基础配置

BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "cache"
TTS_CACHE = CACHE_DIR / "tts"
AI_CACHE = CACHE_DIR / "ai"
for d in (CACHE_DIR, TTS_CACHE, AI_CACHE):
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
    "max_len": 62,                  # 单句上限（注意：是"一句的上限"，不是"攒够才切"）
    "auto_polish": False,           # 朗读前是否自动润色
    "normalize": True,              # 读音规整（符号/日期/金额读顺）
    "auto_voice": True,             # 按内容自动推荐音色
    "long_doc_chars": 6000,         # 超过这个字数就分块总结，避免静默截断
    "cache_limit": 400,             # 最多缓存多少个音频片段
    # 用户偏好：会注入 AI 提示词，让总结/润色越用越像你要的样子
    "prefs": {"style": "", "mode": "none", "model": "", "quality": False},
}

ENGINE_VERSION = "1.2.1"

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
# 真正的实现搬到了 text_nlp.py（结构解析 / 读音规整 / 逐句切分 / 音色推荐）。
# 这里只留薄封装，兼容旧调用方（Obsidian 插件、剪贴板脚本仍在用 clean_text）。

def clean_text(raw: str) -> str:
    return NLP.clean(raw)


def split_segments(text: str, max_len: int = 62) -> list:
    items = NLP.split_items("", max_len=max_len,
                            blocks=[{"kind": "para", "text": text}]) if text.strip() else []
    return [it["text"] for it in items] or ([text] if text.strip() else [])


def plan_text(raw: str, max_len: int = 62, do_normalize: bool = True) -> dict:
    """一站式：解析结构 -> 清洗 -> 读音规整 -> 逐句。返回 text/items/segments。"""
    return NLP.prepare(raw, max_len=max_len, do_normalize=do_normalize)


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


def _prosody(value, unit: str, fallback: str) -> str:
    """把语速/音调/音量规整成 edge-tts 认得的「±数值单位」写法。

    坑：前端滑块取到 0 时会拼出 "0%" / "0Hz"（少了正号），而 edge-tts 只认
    带符号的写法，会直接报 Invalid volume '0%' —— 表现为「页面显示已连接，
    但每一句都播不出来」。默认音调/音量恰好都是 0，所以这个坑一踩就是全坏。
    这里统一兜底：缺符号补 '+'、非法值退回默认、单位重复/带空格都容忍。
    """
    raw = str(value if value is not None else "").strip().replace(" ", "")
    if not raw:
        return str(fallback)
    if unit and raw.lower().endswith(unit.lower()):
        raw = raw[: -len(unit)]
    m = re.fullmatch(r"([+-]?)(\d+(?:\.\d+)?)", raw)
    if not m:
        return str(fallback)
    return f"{m.group(1) or '+'}{m.group(2)}{unit}"


def synth(text: str, voice: str, rate: str, pitch: str, volume: str) -> Path:
    """合成一段语音，带磁盘缓存。"""
    # 先规整韵律参数，避免 "0%" 这类写法让 edge-tts 直接失败
    rate = _prosody(rate, "%", CFG["rate"])
    pitch = _prosody(pitch, "Hz", CFG["pitch"])
    volume = _prosody(volume, "%", CFG["volume"])
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

# 长文用：先分块提炼要点（map），再把要点合成一份连贯朗读稿（reduce）。
# 不这么做的话，超过约 1.5 万字的原文会撞上 num_ctx=8192 被静默截断，
# 模型只看到前半段，给出"看起来正常其实漏了一半"的总结 —— 这是最危险的失败。
SYS_MAP = (
    "你是朗读者撰稿人。下面是长文的其中一部分，请只提炼这一部分的要点，"
    "写成 2 到 5 句口语短句，保留关键数字与结论，不要 Markdown，不要开场白，"
    "不要复述要求，不要输出思考过程。"
)

SYS_MERGE = (
    "你是朗读者撰稿人。下面是一篇文章各部分的要点，请把它们合并成一段连贯的朗读稿：\n"
    "1. 按原文脉络串起来，用连接词自然过渡，不要出现「第一部分」「要点一」这类字样；\n"
    "2. 完全口语化，句子短，适合听；不要 Markdown，不要括号注释；\n"
    "3. 数字、百分比、缩写写成便于朗读的中文（30% -> 百分之三十）；\n"
    "4. 只输出朗读稿正文，不要任何解释、复述或思考过程。"
)


def pref_suffix() -> str:
    """把用户偏好拼成提示词尾巴，让 AI 越用越贴合口味。"""
    p = CFG.get("prefs") or {}
    style = (p.get("style") or "").strip()
    if not style:
        return ""
    return f"\n\n【用户偏好，请遵守】{style[:200]}"


# ---------------------------------------------------------------- AI 结果缓存
# 同一段文字 + 同一组参数，第二次直接命中磁盘缓存。
# 这件事看着小，体验差别巨大：润色一次要 20~50 秒，缓存后是 0 秒。
# 也是「停止 -> 再点朗读」不再重跑模型的前提。

def _ai_key(text: str, mode: str, model: str, limit: int) -> str:
    pref = json.dumps(CFG.get("prefs") or {}, ensure_ascii=False, sort_keys=True)
    raw = f"{mode}|{model}|{limit}|{len(text)}|{pref}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def ai_cache_get(key: str):
    f = AI_CACHE / f"{key}.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        os.utime(f, None)                    # 命中就续命，常用的不会被清理掉
        return d
    except Exception:
        return None


def ai_cache_put(key: str, payload: dict) -> None:
    try:
        (AI_CACHE / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        files = sorted(AI_CACHE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[max(60, int(CFG.get("cache_limit", 400)) // 4):]:
            f.unlink(missing_ok=True)
    except Exception as e:
        log.warning("AI 缓存写入失败: %s", e)


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


def _chunk_text(text: str, size: int) -> list:
    """按段落聚成若干块，每块不超过 size 字（不切断段落）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > size:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p).strip() if buf else p
        while len(buf) > size * 1.6:               # 单个超长段落再按句切
            cut = buf[:size]
            idx = max(cut.rfind(m) for m in "。！？\n")
            if idx < size * 0.5:
                idx = size - 1
            chunks.append(buf[: idx + 1].strip())
            buf = buf[idx + 1:].strip()
    if buf:
        chunks.append(buf)
    return chunks or [text]


def summarize_long(text: str, model: str, limit: int) -> str:
    """长文分块总结：先逐块提要点，再合并成一份连贯朗读稿。"""
    chunks = _chunk_text(text, 2600)
    per = max(100, limit // max(1, len(chunks)))
    notes = []
    for i, c in enumerate(chunks, 1):
        out = clean_text(ollama_chat(
            SYS_MAP + pref_suffix(),
            f"这是长文的第 {i}/{len(chunks)} 部分，请提炼要点（{per} 字以内）：\n\n{c}",
            model, temperature=0.3))
        if looks_like_cot(out):
            raise AIBad(f"第 {i} 段提炼时把思考过程写进了正文")
        if out:
            notes.append(out)
    if not notes:
        raise AIBad("分块提炼结果为空")
    merged = ollama_chat(
        SYS_MERGE + pref_suffix(),
        f"请把下面这些要点合并成一段朗读稿（目标 {limit} 字以内）：\n\n" + "\n\n".join(notes),
        model, temperature=0.35)
    merged = clean_text(merged)
    if not merged or looks_like_cot(merged):
        raise AIBad("合并阶段输出不合格")
    return merged


def ai_refine(text: str, mode: str, body: dict, use_cache: bool = True):
    """跑 AI + 质量把关；不合格会自动换更强的模型重试，全不合格则抛 AIBad。

    返回 (处理后的文本, 音色建议, 实际使用的模型, 是否命中缓存)。
    """
    limit = int(body.get("limit") or 300)
    requested = body.get("model") or (CFG["quality_model"] if body.get("quality") else CFG["small_model"])
    key = _ai_key(text, mode, requested, limit)
    if use_cache:
        hit = ai_cache_get(key)
        if hit and hit.get("text"):
            log.info("AI 缓存命中（%s / %s）", mode, hit.get("model"))
            return hit["text"], hit.get("style") or {}, hit.get("model") or requested, True

    problems = []
    for model in pick_models(body):
        try:
            if mode == "summarize":
                if len(text) >= int(CFG.get("long_doc_chars", 6000)):
                    out = summarize_long(text, model, limit)
                else:
                    out = clean_text(ollama_chat(
                        SYS_SUMMARY + pref_suffix(),
                        f"请把下面内容压缩成朗读稿（目标 {limit} 字以内）：\n\n{text}", model))
                if not out:
                    raise AIBad("输出为空")
                if looks_like_cot(out):
                    raise AIBad("把思考过程写进了正文")
                if len(text) > 200 and len(out) < max(20, len(text) * 0.05):
                    raise AIBad("输出过短，疑似被截断")
                ai_cache_put(key, {"text": out, "style": {}, "model": model, "mode": mode,
                                   "ts": time.time(), "src_len": len(text)})
                return out, {}, model, False

            out = ollama_chat(SYS_POLISH + pref_suffix(),
                              f"请打磨下面这段文字的朗读稿：\n\n{text}",
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
            style = {
                "rate": obj.get("rate") or CFG["rate"],
                "pitch": obj.get("pitch") or CFG["pitch"],
                "note": obj.get("note") or "",
                "title": obj.get("title") or "",
            }
            ai_cache_put(key, {"text": polished, "style": style, "model": model, "mode": mode,
                               "ts": time.time(), "src_len": len(text)})
            return polished, style, model, False
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
    """只做清洗 + 读音规整 + 逐句，不调用 AI。给"先看稿子再决定"的场景用。"""
    body = request.get_json(silent=True) or {}
    if not (body.get("text") or "").strip():
        return jsonify({"ok": False, "error": "没有可处理的文本"}), 400
    r = plan_text(body.get("text", ""),
                  max_len=int(body.get("max_len") or CFG["max_len"]),
                  do_normalize=bool(body.get("normalize", CFG["normalize"])))
    return jsonify({"ok": True, "text": r["text"], "segments": r["segments"],
                    "items": r["items"], "count": len(r["segments"])})


RE_NOISE_TAG = re.compile(
    r"<(script|style|noscript|svg|iframe|template|nav|header|footer|aside|form|select|button)"
    r"[^>]*>.*?</\1\s*>", re.S | re.I)
RE_NOISE_ATTR = re.compile(
    r"<(div|section|ul|ol|span|p|dl)[^>]*(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:nav|menu|sidebar|breadcrumb|comment|copyright|footer|header|toolbar|share|related|"
    r"recommend|advert|\bads?\b|pagination|tag-list|meta|catalog|toc)[^\"']*[\"'][^>]*>"
    r".*?</\1\s*>", re.S | re.I)


def _looks_like_nav(line: str) -> bool:
    """判断一行是不是导航/菜单/标签云这类"不是正文"的东西。"""
    t = line.strip()
    if len(t) < 12:
        return True
    if re.search(r"[。！？；，、：.!?;,]", t):
        return False                       # 有标点，像正常句子
    if len(t.split()) >= 4:
        return True                        # 无标点的长行 + 空格分词多 = 菜单条
    if re.fullmatch(r"[\w\u4e00-\u9fff]{1,4}([\s·|/›»]+\S{1,6}){3,}", t):
        return True
    return False


def extract_main_text(raw: str) -> str:
    """从网页 HTML 里抠出正文（不用额外依赖的轻量做法）。"""
    s = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
    # 先盯住常见正文容器，命中就只取它，能显著减少噪声
    for pat in (r"<article[^>]*>(.*?)</article\s*>",
                r'<[^>]+(?:id|class)\s*=\s*["\'][^"\']*(?:js_content|rich_media_content|'
                r"article-content|post-content|entry-content|markdown-body)[^\"']*[\"'][^>]*>(.*?)</div\s*>",
                r"<main[^>]*>(.*?)</main\s*>"):
        m = re.search(pat, s, re.S | re.I)
        if m and len(re.sub(r"<[^>]+>", "", m.group(1))) > 200:
            s = m.group(1)
            break
    s = RE_NOISE_TAG.sub(" ", s)
    s = RE_NOISE_ATTR.sub(" ", s)
    s = re.sub(r"</(p|div|li|h[1-6]|tr|section|article|blockquote|dd|dt)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\u3000]+", " ", s)
    lines = []
    for line in s.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or _looks_like_nav(line):
            continue
        lines.append(line)
    return "\n\n".join(lines).strip()


@app.get("/api/fetch")
@app.post("/api/fetch")
def api_fetch():
    """把一个网页正文抓成可朗读的文本。剪贴板里是链接时直接用它。"""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
    else:
        body = request.args
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return jsonify({"ok": False, "error": "不是有效的 http/https 链接"}), 400
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        return jsonify({"ok": False, "error": f"抓取失败：{e}"}), 502

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    text = extract_main_text(raw)
    cleaned = clean_text(text)
    if len(cleaned) < 120:
        return jsonify({"ok": False, "chars": len(cleaned),
                        "error": "没能从这个页面提取到正文（可能需要登录，或内容由脚本动态加载）。"
                                 "请手动复制正文后再朗读。"}), 422
    return jsonify({"ok": True, "title": title, "text": cleaned,
                    "chars": len(cleaned), "url": r.url})


@app.get("/api/settings")
def api_settings_get():
    return jsonify({"ok": True, "settings": CFG.get("prefs") or {},
                    "config": {k: CFG[k] for k in ("normalize", "auto_voice", "max_len",
                                                   "long_doc_chars", "default_voice",
                                                   "rate", "pitch", "volume",
                                                   "small_model", "quality_model")}})


@app.post("/api/settings")
def api_settings_post():
    """记住用户偏好（会注入 AI 提示词），以及引擎侧的朗读选项。"""
    body = request.get_json(silent=True) or {}
    prefs = CFG.setdefault("prefs", {})
    for k in ("style", "mode", "model", "quality"):
        if k in body:
            prefs[k] = body[k]
    for k in ("normalize", "auto_voice", "max_len", "long_doc_chars",
              "default_voice", "rate", "pitch", "volume"):
        if k in body:
            CFG[k] = body[k]
    save_config(CFG)
    return jsonify({"ok": True, "settings": prefs})


@app.post("/api/recommend")
def api_recommend():
    """按内容推荐音色（前端也可以直接用 /api/prepare 里带回来的推荐）。"""
    body = request.get_json(silent=True) or {}
    text = clean_text(body.get("text", ""))
    if not text:
        return jsonify({"ok": False, "error": "没有文本"}), 400
    names = [v.get("ShortName") for v in list_voices() if v.get("ShortName")]
    rec = NLP.recommend_voice(text, names)
    return jsonify({"ok": True, **rec})


def _guarded_stream(text: str, limit: int, model: str):
    """流式总结：先攒前 160 字做一次质检，发现推理泄漏立刻中止，不把垃圾吐给用户。"""
    gen = ollama_chat(SYS_SUMMARY + pref_suffix(),
                      f"请把下面内容压缩成朗读稿（目标 {limit} 字以内）：\n\n{text}",
                      model, stream=True)
    buf, early = "", True
    total = ""
    for piece in gen:
        total += piece
        if early:
            buf += piece
            if looks_like_cot(buf):
                yield "\n（小模型把思考过程写进了正文，已中止。建议切到升配模型，或改用「润色」。）"
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
    if body.get("stream") and mode == "summarize" and len(text) < int(CFG.get("long_doc_chars", 6000)):
        try:
            model = pick_models(body)[0]
        except AIBad as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        return Response(_guarded_stream(text, limit, model), mimetype="text/plain; charset=utf-8")
    try:
        out_text, style, model, cached = ai_refine(text, mode, body)
    except AIBad as e:
        return jsonify({"ok": False, "error": f"AI 输出未通过质量检查：{e}"}), 502
    if mode == "summarize":
        return jsonify({"ok": True, "mode": mode, "model": model, "text": out_text,
                        "ai_cached": cached,
                        "long_doc": len(text) >= int(CFG.get("long_doc_chars", 6000))})
    return jsonify({"ok": True, "mode": mode, "model": model, "text": out_text,
                    "ai_cached": cached, **style})


@app.post("/api/prepare")
def api_prepare():
    """一步到位：清洗 -> （可选）AI -> 读音规整 -> 逐句。

    返回里除了兼容用的 segments（字符串数组），还多了 items：
       [{text, kind, pause, rate}]  —— pause 是这句之后该停多久（毫秒），
       kind 是 heading/list/quote/para，前端据此做停顿、语速层次和高亮。
    另外带上按内容推荐的音色与语言判定，前端可以直接采纳。
    """
    body = request.get_json(silent=True) or {}
    ai = body.get("ai") or "none"
    raw = body.get("text", "")
    do_norm = bool(body.get("normalize", CFG["normalize"]))
    max_len = int(body.get("max_len") or CFG["max_len"])
    if not (raw or "").strip():
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
    cached = False
    long_doc = False
    source = clean_text(raw)
    final_text = source

    if ai in ("summarize", "polish"):
        try:
            final_text, style2, model, cached = ai_refine(source, ai, body)
            style.update(style2)
        except Exception as e:
            # AI 不靠谱时绝不阻断朗读：退回原文，并把原因告诉调用方
            log.warning("AI 未通过质量检查，改为朗读原文: %s", e)
            ai_note = f"AI 输出未通过质量检查，已改为朗读原文。原因：{e}"
            ai = "none"
            model = None
            final_text = source
        long_doc = len(source) >= int(CFG.get("long_doc_chars", 6000))

    r = plan_text(final_text, max_len=max_len, do_normalize=do_norm)
    segs = r["segments"]
    if not segs and r["text"]:
        segs = [r["text"]]
        r["items"] = [{"text": r["text"], "kind": "para", "pause": 660, "rate": 0}]

    names = [v.get("ShortName") for v in list_voices() if v.get("ShortName")]
    rec = NLP.recommend_voice(r["text"], names) if r["text"] else {}
    return jsonify({
        "ok": True,
        "text": r["text"],
        "segments": segs,
        "items": r["items"],
        "count": len(segs),
        "style": style,
        "ai": ai,
        "ai_note": ai_note,
        "ai_cached": cached,
        "model": model,
        "long_doc": long_doc,
        "source_chars": len(source),
        "lang": rec.get("lang"),
        "voice_suggestion": rec.get("voice"),
        "voice_reason": rec.get("reason"),
        "normalized": do_norm,
    })


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
        "hint": body.get("hint") or "",
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
