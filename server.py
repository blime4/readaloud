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
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# Windows 下所有子进程（nvidia-smi / git / ffmpeg…）都必须隐藏控制台窗口，
# 否则每次调用都会在屏幕上闪一个 CMD 黑框。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

import requests
import edge_tts
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

import bookshelf as SHELF
import document_import
import text_nlp as NLP
import tts_engine as TTS
import webfetch
import webagent
import cookies as LOGIN

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
    "cache_limit": 1200,            # 最多缓存多少个音频片段（本机 TTS 一句一个文件）
    "cache_mb": 900,                # 缓存总体积上限，条数之外的真正兜底
    # 上下文窗口。必须给"思考 + 正文"都留出空间：实测 8192 时 qwen3.5:9b 思考了
    # 一万七千多字就把预算烧光，正文输出 0 字（点「升配」等三分钟换来一句报错）。
    # 本机 16G 显存足够放到 32768。
    "num_ctx": 32768,
    "keep_alive": "30m",            # 让模型多在内存里待一会儿，免得隔几分钟再问要重新加载
    # 抓网页（/api/fetch、/api/agent）
    "fetch_timeout": 25,            # 单次抓取超时（秒）
    "fetch_proxy": "auto",          # auto=探测到本机 7897 代理就用；也可填具体地址或留空直连
    "agent_model": "",              # 取稿 Agent 用的模型，留空则跟 small_model
    "agent_max_tool_calls": 4,      # Agent 最多调几次工具
    # 登录态（知乎这类站没登录态就 403）
    "use_login": True,              # 抓到认识的站点时自动带上已配的登录态
    "browser_login_port": 9222,     # 「打开登录窗口」时浏览器监听的调试端口
    "browser_fetch": True,          # 规则抓取全军覆没时，让浏览器自己去取
    # 用户偏好：会注入 AI 提示词，让总结/润色越用越像你要的样子
    "prefs": {"style": "", "mode": "none", "model": "", "quality": False},
    # 监听地址。默认只听回环；想让手机连上来就改成 0.0.0.0（面板上有开关）
    "host": "127.0.0.1",
    # 语音引擎：edge=微软神经语音（默认）；local=本机 OpenAI 兼容 TTS
    "tts_provider": "edge",
    # 默认指向 CosyVoice2 的端口；用户不用知道这个数字，连通后引擎会自己写回
    "tts_local_url": "http://127.0.0.1:8891",
    "tts_local_model": "",
    "tts_local_voice": "",
    "tts_local_key": "",
    # 默认 WAV，不要 MP3：本机服务转 MP3 要起一个 ffmpeg 进程，实测每句多花
    # 1.0~1.8 秒（短句 1.0 秒是进程启动，长句还要加编码时间）。8 秒音频因此
    # 从 8.3 秒变成 10.2 秒，RTF 从 1.06 涨到 1.23 —— 本来刚好跟得上，一转码
    # 就跟不上了。WAV 体积大 5 倍，但走的是本机回环、又落在缓存里，不值一提。
    "tts_local_format": "wav",
    # 异地（手机走流量 / 不在同一个 WiFi）时发什么格式：
    # "auto" = 内网 WAV、异地 MP3（体积约 1/5，听书完全够）；也可强成 "wav"/"mp3"。
    # 转码那 1.8 秒在家里是无谓开销，在外却是省下 5 倍流量的关键，所以按客户端分。
    "remote_audio_format": "auto",
    # 本机 TTS 一键启动：切到 local 但服务没起来时，引擎帮忙拉起脚本
    "tts_local_autostart": True,
    "gpu_clock_lock": "1800",       # 启动 TTS 时把显卡锁到这个 MHz 以上（空=不锁）
    "gpu_clock_elevate": True,      # 没权限时弹一次 UAC 提权（拒绝则下次再试）
    "tts_local_python": "",         # 留空自动探测（见 _tts_python）
    "tts_local_script": "",         # 留空按端口猜：8891=tts_cosy_server，8890=tts_qwen_server
    "tts_local_args": "",           # 额外启动参数，例如 "--fp16"（半精度，显存吃紧时可试）
    "tts_local_env": "",            # 额外环境变量，写法 "K=V;K2=V2"（见 start_local_tts）
    "tts_local_split": 30,          # 本地合成超过这个字数就把长句切两段并发（0 关闭）
    # 当前本机引擎：cosy=CosyVoice2（GPU），moss=MOSS-TTS-Nano（纯 CPU / ONNX）
    "tts_engine": "cosy",
    # 播放时按住电源请求，别让系统进待机：待机状态下显卡只有 500MHz，
    # 合成时间直接翻倍（实测 RTF 1.07 → 2.2）。见 _awake_hold。
    "keep_display_awake": True,
    # 引擎自身：静默升级（git pull + 空闲时自动重启）
    "auto_update": True,
    # 兼容监听端口：老书签 127.0.0.1:8756 也指向本引擎，避免又打开一份旧副本
    "extra_ports": [8756],
}

ENGINE_VERSION = "1.13.0"

# --------------------------------------------------------------------------
# 本机引擎注册表：两套 TTS 都能用同一个 /v1/audio/speech 约定，切换只改配置
#
# cosy 走 GPU（0.5B，实测 RTF 0.91，音质稳）；moss 是 Nano 0.1B 的 ONNX 版，
# 纯 CPU 跑、完全不占显卡，适合 A/B 试听或把显卡腾给别的事。两套的音频缓存
# 键里本来就带 url/model，切换不会串音，也不用清缓存。
# --------------------------------------------------------------------------
def _engine_profile(eid: str) -> dict:
    moss_py = Path.home() / "miniconda3" / "envs" / "moss-nano" / "python.exe"
    cosy_py = Path.home() / "miniconda3" / "envs" / "cosyvoice" / "python.exe"
    qwen_py = BASE / ".tts-venv" / "Scripts" / "python.exe"
    if eid == "qwen3":
        weights = (BASE / "models" / "Qwen3-TTS-12Hz-1.7B-CustomVoice").resolve()
        return {
            "id": "qwen3", "label": "Qwen3-TTS · 1.7B",
            "note": "1.7B / 约 4GB 显存，官方 9 把调好的音色（中文 5 把），"
                    "支持文字描述造音色与 3 秒声音克隆；流式首包约 97ms。",
            "url": "http://127.0.0.1:8890",
            "model": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
            # 温暖柔和的中文女声，长篇朗读比明亮锐感的 vivian 耐听；id 必须是小写
            "voice": "serena", "script": "tts_qwen_server.py",
            "python": str(qwen_py) if qwen_py.exists() else "",
            "args": "--preload",
            "env": f"QWEN_TTS_MODEL_DIR={weights}",
            "weights_dir": str(weights),
        }
    if eid == "moss":
        nano = (Path(os.environ.get("MOSS_NANO_DIR") or (BASE.parent / "moss-tts-nano"))).resolve()
        voices_dir = (BASE / "assets" / "voices").resolve()
        return {
            "id": "moss", "label": "MOSS-TTS-Nano · ONNX",
            "note": "0.1B / 48kHz 立体声，官方内置 18 把音色（中文 6 把，含「说书」"
                    "「深夜电台」），也可放 wav 自定义克隆；CUDA 可用时 RTF≈0.9，"
                    "自动回退 CPU（RTF≈2.3）。",
            "url": "http://127.0.0.1:8892", "model": "MOSS-TTS-Nano-100M-ONNX",
            # 深夜电台是女声、专为长篇朗读准备，比 1.12.0 那把二次克隆干净
            "voice": "Lingyu", "script": "tts_moss_server.py",
            "python": str(moss_py) if moss_py.exists() else "",
            "args": "--preload",
            "env": f"MOSS_NANO_DIR={nano};MOSS_VOICES_DIR={voices_dir};"
                   f"MOSS_DEFAULT_VOICE=Lingyu",
            "nano_dir": str(nano), "voices_dir": str(voices_dir),
        }
    return {
        "id": "cosy", "label": "CosyVoice2 · GPU",
        "note": "0.5B 本地大模型，显存约 6GB，实测 RTF 0.91；现在的默认引擎。",
        "url": "http://127.0.0.1:8891", "model": "CosyVoice2-0.5B",
        "voice": "默认女声", "script": "tts_cosy_server.py",
        "python": str(cosy_py) if cosy_py.exists() else "",
        "args": "--fp16 --preload",
        "env": "COSY_TTS_CONCURRENCY=2",
    }


LOCAL_ENGINES = ("cosy", "moss", "qwen3")


# 与 tts_qwen_server.py 的 SPEAKER_LABELS 保持一致：8890 没启动时面板也能列出音色。
QWEN_SPEAKER_LABELS = {
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


def _qwen_voice_catalog() -> list[dict]:
    return [{"id": k, "name": f"{k} · {v}", "builtin": True}
            for k, v in QWEN_SPEAKER_LABELS.items()]


def _moss_manifest(nano_dir) -> Path | None:
    """Nano 的 ONNX 权重清单，里面带官方 18 把预编码内置音色。"""
    try:
        return next((Path(nano_dir) / "models").glob("**/browser_poc_manifest.json"))
    except (StopIteration, OSError):
        return None


def _moss_voice_catalog(nano_dir) -> list[dict]:
    """主引擎不依赖 8892 是否启动，也能把可选音色列给前端。"""
    mf = _moss_manifest(nano_dir)
    if not mf:
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return []
    group_cn = {"Chinese Male": "中文男声", "Chinese Female": "中文女声",
                "English Male": "英文男声", "English Female": "英文女声",
                "Japanese Female": "日文女声"}
    rows = []
    for v in data.get("builtin_voices") or []:
        vid = str(v.get("voice") or "").strip()
        if not vid:
            continue
        disp = str(v.get("display_name") or vid)
        tag = disp.split(" ", 1)[-1] if disp[:3] in ("CN ", "EN ", "JP ") else disp
        group = str(v.get("group") or "")
        rows.append({"id": vid, "group": group,
                     "name": f"{tag} · {group_cn.get(group, group or '其他')}（{vid}）"})
    return rows

# 引擎"我具备哪些能力"的清单。网页端连上后会核对，缺项就提示用户重启引擎
# —— 因为改完 server.py 进程不会自己换代码，很容易"新页面配旧引擎"而无人察觉。
FEATURES = [
    "structured-items",     # prepare 返回带 kind 的 items（标题/列表/引文分层）
    "prosody-normalize",    # 韵律参数缺正号也能合成
    "num-ctx-config",       # 上下文窗口可配（升配模型能出正文）
    "keep-alive",           # 模型常驻，避免重复加载
    "web-fetch-quality",    # fetch 返回质量分/诊断/噪声清理统计
    "web-agent",            # /api/agent：工具调用小模型主导取稿
    "local-url-direct",     # 回环/内网地址直连，不经过代理
    "fail-fast",            # 抓不到就不叫模型空转，直接如实报错
    "site-login",           # 登录态：/api/login，从浏览器借 cookie 读知乎这类站
    "browser-fetch",        # 抓不动时让本机浏览器自己去取（含渲染后的 DOM）
    "local-document-import", # /api/import：读取本机 TXT / EPUB / MOBI / PDF
    "bookshelf",            # /api/books：书留在本机，电脑和手机共用进度
    "tts-provider",         # /api/tts 支持 provider=edge|local（本机 OpenAI 兼容 TTS）
    "lan-access",           # /api/lan：手机通过局域网访问本机引擎
    "tailscale-lan",        # /api/lan 的 remote_urls：异地（4G/公司网）也能连
    "remote-audio",         # 异地客户端自动发 MP3（WAV 现转，省 5 倍流量）
    "presynth",             # /api/presynth：整本后台预合成，播放句句命中缓存
    "presynth-hold",        # /api/presynth/hold：开播即让路，暂停从播放位置续备
    "presynth-resume",      # /api/presynth/resume：停下的任务一键从断点续上
    "task-monitor",         # /api/tasks：电脑/手机都能看到眼下在合成哪几句
    "task-stop",            # /api/tasks/stop：一键停预合成 / 暂停后台 30 分钟
    "tts-bench",            # /api/tts/bench：量本机合成的 RTF，判断能不能跟上播放
    "tts-autostart",        # /api/tts/local：一键拉起本机大模型 TTS 并自动写回配置
    "tts-engine-switch",    # /api/tts/engines：CosyVoice2 / MOSS-Nano / Qwen3-TTS 三套一键切换
    "moss-voices",          # MOSS-Nano 官方内置 18 把音色 + 自定义 wav 克隆，可逐把切换
    "qwen3-tts",            # 第三套本机引擎 Qwen3-TTS 1.7B：9 把官方音色，逐把可切
    "markdown-readaloud",   # .md 导入 + 结构渲染 + 通勤/平衡/精读三档朗读稿
    "engine-upgrade",       # /api/restart + /api/update：原地热重启、git pull 自升级
]

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
app.config["MAX_CONTENT_LENGTH"] = document_import.MAX_IMPORT_BYTES


@app.after_request
def add_cors(resp):
    """让三类调用方都能直接用同一套接口：
       1) 本机程序（Obsidian 插件、剪贴板脚本）
       2) 从磁盘/预览面板打开的本地页面
       3) 部署在 GitHub Pages 上的 HTTPS 页面 —— 它要跨源访问 http://127.0.0.1

    第 3 类会遇到 Chrome 的 Private Network Access 检查，必须显式回
    Access-Control-Allow-Private-Network，否则预检就被拦掉（表现为"连不上"）。
    服务默认只监听 127.0.0.1，本机之外访问不到，所以放开来源是安全的；
    一旦为了让手机连上来改成 0.0.0.0，就由 _guard_lan 收口（见下）。
    """
    origin = request.headers.get("Origin")
    resp.headers["Access-Control-Allow-Origin"] = origin or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-ReadAloud-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
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


# ---------------------------------------------------------------- 局域网访问
# 一旦为了手机把 host 改成 0.0.0.0，整套接口就对同一个 WiFi 里所有人都开着：
# 能读你的书、能用你配的知乎登录态去抓页面、还能烧你的显卡。
# 所以从本机之外来的请求必须带访问码，页面第一次打开时用 ?k=xxx 带进来并记住。

_PAGE_PATHS = ("/", "/index.html", "/favicon.ico", "/manifest.json",
               "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png")

# 页面要用的静态资源（脚本/样式/图标）跟 GitHub Pages 上那份一模一样，本来就不是秘密，
# 也拿不到任何书；但手机打开页面时 <script src> 不会带 ?k，所以这里必须整体放行，
# 否则 qr.js 401 → 二维码、语音组件全部哑掉。
_STATIC_EXT = (".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".webp",
               ".svg", ".ico", ".woff", ".woff2", ".ttf", ".json", ".webmanifest",
               ".wasm")


def lan_token() -> str:
    token = str(CFG.get("lan_token") or "")
    if not token:
        token = secrets.token_hex(4)
        CFG["lan_token"] = token
        save_config(CFG)
    return token


def _request_token() -> str:
    token = request.args.get("k") or request.headers.get("X-ReadAloud-Key") or ""
    if not token:
        token = str(request.cookies.get("ra_key") or "")      # 主页带码访问时种下的
    if not token and request.method in ("POST", "DELETE") and request.is_json:
        token = str((request.get_json(silent=True) or {}).get("k") or "")
    return token


@app.before_request
def _guard_lan():
    remote = request.remote_addr or ""
    if remote in ("127.0.0.1", "::1"):
        return None
    if (CFG.get("host") or "127.0.0.1") == "127.0.0.1":
        return None                      # 没开局域网，本来也连不上
    if request.method == "OPTIONS" or request.path in _PAGE_PATHS:
        return None                      # 页面本身可以先给，接口再收口
    if request.path.startswith(("/minicpm-voice", "/docs/")):
        return None
    if not request.path.startswith("/api/") and request.path.lower().endswith(_STATIC_EXT):
        return None                      # qr.js / 图标 / wasm 之类，页面外壳的一部分
    if _request_token() == lan_token():
        return None
    return jsonify({"ok": False, "need_key": True,
                    "error": "这个地址需要在网址后面带上访问码（?k=…），在工作台「手机访问」里能看到。"}), 401


# ---------------------------------------------------------------- 文本预处理
# 真正的实现搬到了 text_nlp.py（结构解析 / 读音规整 / 逐句切分 / 音色推荐）。
# 这里只留薄封装，兼容旧调用方（Obsidian 插件、剪贴板脚本仍在用 clean_text）。

def clean_text(raw: str) -> str:
    return NLP.clean(raw)


def split_segments(text: str, max_len: int = 62) -> list:
    items = NLP.split_items("", max_len=max_len,
                            blocks=[{"kind": "para", "text": text}]) if text.strip() else []
    return [it["text"] for it in items] or ([text] if text.strip() else [])


def plan_text(raw: str, max_len: int = 62, do_normalize: bool = True,
              markdown: bool | None = None, md_mode: str = "balanced") -> dict:
    """一站式：解析结构 -> 清洗 -> 读音规整 -> 逐句。返回 text/items/segments。

    markdown=None 自动检测 Markdown 源码；命中时额外返回 md（三档朗读稿 + 渲染视图）。
    """
    return NLP.prepare(raw, max_len=max_len, do_normalize=do_normalize,
                       markdown=markdown, md_mode=md_mode)


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


_LOCAL_SOFT_MARKS = "，,、；;：:·"      # 句内软停顿：长句在这里切开
_LOCAL_SENT_END = "。！？!?"


def _local_split(text: str, limit: int) -> list | None:
    """长句按软停顿切成两段（CosyVoice 对长句的流式分块推理效率很差：
    58 字 RTF 2.0+，切半并发后每段回到 1.1 档）。返回 None 表示不用切。

    段尾统一收成句号：拼回一条音频时，两段之间是原句本来就该有的停顿。
    """
    if len(text) < limit + 4:
        return None
    mid = len(text) // 2
    cand = [i for i, ch in enumerate(text)
            if ch in _LOCAL_SOFT_MARKS and 6 <= i < len(text) - 6]
    i = min(cand, key=lambda x: abs(x - mid)) if cand else mid
    s1, s2 = text[: i + 1], text[i + 1:]
    tail = s1[-1]
    if tail in "，,、；;：:·（(“\"“":
        s1 = s1[:-1] + "。"
    elif tail not in _LOCAL_SENT_END:
        s1 += "。"
    return [s1, s2]


def _join_audio(a: bytes, b: bytes, mime: str) -> tuple:
    """把两段同源音频接成一条。WAV 拼 data 块，MP3 直接接帧。"""
    if mime == "audio/mpeg":
        return a + b, mime
    if not (a.startswith(b"RIFF") and b.startswith(b"RIFF")):
        raise ValueError("分段合成的返回不是 WAV，无法拼接")
    off_a = a.find(b"data", 12)
    off_b = b.find(b"data", 12)
    if off_a < 0 or off_b < 0:
        raise ValueError("WAV 里找不到 data 块")
    da, db = a[off_a + 8:], b[off_b + 8:]
    head = bytearray(a[: off_a + 8])
    struct.pack_into("<I", head, off_a + 4, len(da) + len(db))       # data 块大小
    struct.pack_into("<I", head, 4, off_a + 8 + len(da) + len(db) - 8)  # RIFF 大小
    return bytes(head) + da + db, "audio/wav"


def _silent_wav(seconds: float = 0.3, rate: int = 24000) -> bytes:
    """一段静音 WAV。

    给「没有可念内容」的文本用：纯标点的合成请求在 CosyVoice 上是 502
    「模型没有产出音频」，而浏览器遇到非音频响应既不报错也不结束播放 ——
    听感就是整本书卡在那一句上，再也翻不过去。返回静音让它当停顿走过去。
    """
    n = max(1, int(rate * seconds))
    data = b"\x00\x00" * n
    head = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    head += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    head += b"data" + struct.pack("<I", len(data))
    return head + data


def _ip_is_lan(ip: str) -> bool:
    """这个来源地址是不是"就在身边"：回环 + 家庭/办公内网。

    Tailscale 的 100.64/10 故意**不算**内网 —— 它走的是互联网，手机在 4G 上
    看这本书，缓存里那点 WAV 体积乘以 5 倍就是流量账单。
    """
    ip = (ip or "").split("%")[0].strip()
    if not ip:
        return True                       # 拿不到就按本地处理，别误伤家里
    if ip in ("::1", "localhost"):
        return True
    if ip.startswith("127.") or ip.startswith("fe80"):
        return True
    if ip.startswith("10.") or ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _client_is_remote() -> bool:
    try:
        return not _ip_is_lan(request.remote_addr or "")
    except Exception:                                     # noqa: BLE001
        return False


def _audio_format_for_client() -> str:
    """这台客户端该收什么格式：家里的 WAV，异地的 MP3。

    家里转 MP3 要起 ffmpeg，实测每句多 1.0～1.8 秒，纯属白花；异地那 5 倍
    体积却是实打实的流量。所以按来源地址分，`remote_audio_format` 可强制。
    """
    mode = str(CFG.get("remote_audio_format") or "auto").lower()
    if mode in ("wav", "mp3"):
        return mode
    return "mp3" if _client_is_remote() else (str(CFG.get("tts_local_format") or "wav").lower())


_ffmpeg_path = {"v": None, "ts": 0.0}


def _ffmpeg() -> str:
    """找 ffmpeg（没有就返回空 → 异地也只好发 WAV）。结果缓存 5 分钟。"""
    now = time.time()
    if _ffmpeg_path["v"] is not None and now - _ffmpeg_path["ts"] < 300:
        return _ffmpeg_path["v"]
    exe = shutil.which("ffmpeg") or ""
    if not exe:
        for cand in (r"C:\Users\bo\miniconda3\Library\bin\ffmpeg.exe",):
            if os.path.exists(cand):
                exe = cand
                break
    _ffmpeg_path["v"] = exe
    _ffmpeg_path["ts"] = now
    return exe


def _wav_to_mp3(src) -> bytes | None:
    """把已缓存的 WAV 转成 MP3。异地听家里预合成好的书时，这条路比重新
    合成快得多（0.3 秒 vs 整句合成），也不用再抢一次显卡。"""
    exe = _ffmpeg()
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-ac", "1", "-b:a", "64k", "-f", "mp3", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            creationflags=NO_WINDOW)
        if done.returncode == 0 and done.stdout:
            return done.stdout
        log.warning("WAV→MP3 转码失败：%s", done.stderr.decode("utf-8", "replace")[:200])
    except Exception as e:                                # noqa: BLE001
        log.warning("WAV→MP3 转码异常：%s", e)
    return None


def synth(text: str, voice: str, rate: str, pitch: str, volume: str,
          provider: str | None = None, model: str | None = None,
          fmt: str | None = None) -> tuple:
    """合成一段语音，带磁盘缓存。返回 (文件路径, mimetype)。

    两条路线共用一个缓存目录，但缓存键带上引擎与模型 —— 同一句话在两个
    引擎下必须是两份音频，否则切了引擎听到的还是老声音（调参也一样）。

    `fmt` 只对 local 生效：异地客户端要 MP3（省 5 倍流量），家里要 WAV
    （不启 ffmpeg）。默认取配置，调用方按来源地址决定即可。
    """
    provider = (provider or CFG.get("tts_provider") or "edge").strip().lower()
    if provider == "local":
        url = CFG.get("tts_local_url") or ""
        api_key = CFG.get("tts_local_key") or ""
        fmt = (fmt or CFG.get("tts_local_format") or "wav").lower()
        fmt = fmt if fmt in TTS.FORMAT_MIME else "wav"
        voice = voice or CFG.get("tts_local_voice") or ""
        model = model or CFG.get("tts_local_model") or ""
        speed = TTS.speed_from_rate(rate)

        def _ck(f: str) -> tuple:
            stamp = f"local|{TTS.normalize_base(url)}|{model}|{voice}|{speed}|{f}|{text}"
            k = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:24]
            return TTS_CACHE / f"{k}.{f}", k

        path, key = _ck(fmt)
        if path.exists() and path.stat().st_size > 0:
            return path, TTS.FORMAT_MIME[fmt]
        # 异地要 MP3，但家里早把整本备成了 WAV：转码比重新合成快二十倍，
        # 也不用再抢显卡（出门在外现合成 = 一句一卡）。
        if fmt == "mp3":
            wav_path, _ = _ck("wav")
            if wav_path.exists() and wav_path.stat().st_size > 0:
                data = _wav_to_mp3(wav_path)
                if data:
                    path.write_bytes(data)
                    _trim_cache()
                    return path, "audio/mpeg"
        # 长句切成两段并发合成（tts_cosy_server 允许 2 条并发，单条喂不饱
        # 显卡；长句本身还因流式分块而 RTF 翻倍）。两段各自落缓存——同一句
        # 倒回去重听时，段缓存直接命中，只差一次内存拼接。
        # 0 真的表示「关闭」：以前写 `CFG.get(...) or 30`，设成 0 会被吞掉，
        # 结果换到不需要切句的引擎时长句仍被拆两段再拼（还会拷出坏 WAV 头）。
        _split_raw = CFG.get("tts_local_split", 30)
        _split = 30 if _split_raw in (None, "") else int(_split_raw)
        segs = _local_split(text, _split) if _split > 0 else None
        if segs and fmt in ("wav", "mp3"):
            parts: list = [None, None]

            def _seg(k: int, s: str) -> None:
                try:
                    parts[k] = synth(s, voice, rate, pitch, volume, provider, model, fmt)
                except BaseException as e:                     # noqa: BLE001
                    parts[k] = e

            ths = [threading.Thread(target=_seg, args=(k, s), daemon=True)
                   for k, s in enumerate(segs)]
            for t in ths: t.start()
            for t in ths: t.join()
            err = next((p for p in parts if isinstance(p, BaseException)), None)
            if err is not None:
                raise err
            ext = "mp3" if parts[0][1] == "audio/mpeg" else "wav"
            data, mime = _join_audio(parts[0][0].read_bytes(),
                                     parts[1][0].read_bytes(), parts[0][1])
        else:
            data, mime = TTS.synthesize_local(text, voice, model, url, api_key, rate, fmt)
            # 以服务端真实返回的类型为准（有的服务会忽略 response_format）
            ext = next((e for e, m in TTS.FORMAT_MIME.items() if m == mime), fmt)
        path = TTS_CACHE / f"{key}.{ext}"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        _trim_cache()
        return path, mime

    # 先规整韵律参数，避免 "0%" 这类写法让 edge-tts 直接失败
    rate = _prosody(rate, "%", CFG["rate"])
    pitch = _prosody(pitch, "Hz", CFG["pitch"])
    volume = _prosody(volume, "%", CFG["volume"])
    key = hashlib.sha1(f"{voice}|{rate}|{pitch}|{volume}|{text}".encode("utf-8")).hexdigest()[:24]
    path = TTS_CACHE / f"{key}.mp3"
    if path.exists() and path.stat().st_size > 0:
        return path, "audio/mpeg"
    tmp = path.with_suffix(".part")
    asyncio.run(
        edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume).save(str(tmp))
    )
    tmp.replace(path)
    _trim_cache()
    return path, "audio/mpeg"


def _trim_cache() -> None:
    """按「条数 + 体积」双上限清缓存（LRU）。

    只按条数清是不够的：一本书 464 句就顶穿了默认上限，预合成刚把整本
    做完，最早合成的那批（正好是最该听的头部）又被自己挤掉，播放时重新
    合成 —— 长文越听越卡，根子就在这里。

    两个约束是踩出来的：
    1) 删除失败绝不能让合成失败。本机的安全策略会拦批量删除，抛的还不是
       Exception 的子类 —— 结果整个预合成线程被带走，界面上看着"在跑"其实
       一句都没动。所以这里 catch BaseException，失败就放弃本轮。
    2) 一轮最多删几条，别在合成路径上一次性删几百个文件。
    """
    try:
        entries = []
        for p in TTS_CACHE.iterdir():
            if not p.is_file() or p.suffix == ".part":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        limit_n = _cache_limit_now()
        limit_b = int(CFG.get("cache_mb", 900)) * 1024 * 1024
        total_b = sum(e[1] for e in entries)
        if len(entries) <= limit_n + 8 and total_b <= limit_b:
            return                      # 没明显超：别每次合成都扫一遍目录
        entries.sort(reverse=True)
        used = 0
        removed = 0
        for i, (_m, size, p) in enumerate(entries):
            used += size
            if i < limit_n and used <= limit_b:
                continue
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except BaseException as e:                  # noqa: BLE001
                log.warning("删缓存文件失败，本轮放弃清理：%s", e)
                return
            if removed >= 24:
                return
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
                temperature: float = 0.4, stream: bool = False, *, think: bool | None = None):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": stream,
        "keep_alive": str(CFG.get("keep_alive", "30m")),
        "options": {"temperature": temperature,
                    "num_ctx": int(CFG.get("num_ctx", 32768))},
    }
    # 默认沿用模型设置。qwen3:4b 强制关闭思考时可能把推理写进正文。
    if think is not None:
        payload["think"] = think
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
    "你是有声书编辑。把原文逐段改写成可以直接念给听众听的稿子。\n"
    "1. 保留原文语言、段落顺序和段落间的空行。保留事实、名字、数字、引文、例句及译文，"
    "不要摘要或补充知识。\n"
    "2. 去掉排版标记、网址和独立的音标注记。拆分难以一口气读完的长句，"
    "补足停顿，把括号中的有效信息自然接进句子，把数字和符号写成适合原文语言的读法。\n"
    "3. 遇到词典词条，把词头和释义连成一句，例如「Adam's apple，意思是喉结。」"
    "保留英文词头、例句及有意义的词性信息，接着讲词源。把「词源」「例句」等栏目标记"
    "改成自然过渡，或者在正文已有过渡时省掉栏目标记。\n"
    "4. 给出与内容匹配的建议语速 rate "
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
    # 润色稿缓存随提示词和验收规则换代，总结稿仍沿用原来的键。
    cache_mode = "polish-v2" if mode == "polish" else mode
    if mode == "polish" and model.partition(":")[0] == "qwen3.5":
        cache_mode = "polish-v3"
    raw = f"{cache_mode}|{model}|{limit}|{len(text)}|{pref}|{text}"
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


def looks_like_cot(s: str, *, check_layout: bool = True) -> bool:
    """识别"把思考过程写进正文"的典型症状。"""
    head = (s or "").lstrip()[:160]
    if any(m in head for m in COT_MARKERS):
        return True
    return check_layout and len(s) > 2500 and s.count("\n") > 15


def pick_agent_model(prefer: str = "") -> str:
    """给取稿 Agent 挑模型：会调工具、而且**不思考**。

    为什么要专门挑"不思考"的：本机装的 qwen3 系列一律带 thinking，
    做「哪些行是网页垃圾」这种判断时输出只有十来个字符，却要先思考几十秒
    （实测 qwen3:4b 清理一次 175 秒，qwen2.5:7b 只要 6 秒，而且判得更准）。
    Agent 要连调好几轮工具，模型一慢整条链就没法用。

    挑选顺序：用户指定 → 已装模型里"有 tools 无 thinking"的 → small_model。
    """
    installed = [m for m in ollama_models() if m.get("name")]
    names = [m.get("name") for m in installed]
    if prefer and prefer in names:
        return prefer
    if CFG.get("agent_model") and CFG["agent_model"] in names:
        return CFG["agent_model"]

    def has(m, cap):
        return cap in (m.get("capabilities") or [])

    for m in installed:
        if has(m, "tools") and not has(m, "thinking"):
            return m["name"]
    for m in installed:
        if has(m, "tools"):
            return m["name"]
    return CFG.get("small_model") or (names[0] if names else "qwen3:4b")


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
    if mode == "polish" and len(text) >= int(CFG.get("long_doc_chars", 6000)):
        raise AIBad("原文过长，请按章节或段落分段润色")

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

            # Qwen3.5 润色可以直接生成朗读稿，省去额外的推理时间。
            think = False if mode == "polish" and model.partition(":")[0] == "qwen3.5" else None
            out = ollama_chat(SYS_POLISH + pref_suffix(),
                              f"请打磨下面这段文字的朗读稿：\n\n{text}",
                              model, json_mode=True, temperature=0.3, think=think)
            try:
                obj = json.loads(out)
            except Exception:
                raise AIBad("没有返回合法 JSON")
            polished = clean_text(obj.get("text") or "")
            if not polished:
                raise AIBad("润色结果为空")
            # 润色保留段落，换行数量本身不能用来判定思考过程。
            if looks_like_cot(polished, check_layout=False):
                raise AIBad("把思考过程写进了正文")
            source_chars = len(re.sub(r"\s+", "", text))
            polished_chars = len(re.sub(r"\s+", "", polished))
            if source_chars > 150 and polished_chars < source_chars * 0.6:
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
    # 显卡：装了 nvidia-smi 才有，没有就留空。
    # 启动子进程偶尔会撞上「0x800700E8 没有足够内存完成此命令」（系统进程/句柄
    # 一时吃紧），这是一次性的，重试一次基本就好 —— 否则页面会突然显示没有显卡。
    for attempt in range(2):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=4, creationflags=NO_WINDOW)
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if parts and parts[0]:
                    info["gpus"].append({"name": parts[0],
                                         "vram": parts[1] if len(parts) > 1 else "",
                                         "driver": parts[2] if len(parts) > 2 else ""})
            break
        except Exception as e:             # noqa: BLE001
            if attempt == 0:
                time.sleep(0.4)
                continue
            log.info("查显卡信息失败（不影响朗读）：%s", e)
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
@app.get("/index.html")
def index():
    for p in UI_CANDIDATES:
        if p.exists():
            resp = Response(p.read_bytes(), mimetype="text/html; charset=utf-8")
            resp.headers["Cache-Control"] = "no-store"     # 改完刷新即生效，线上本地不分叉
            # 带 ?k= 打开过一次，就把访问码种成 Cookie：手机加主屏 / 换了标签页
            # 之后即使 localStorage 没了、网址上也没了 ?k，接口照样能过闸。
            if request.args.get("k") == lan_token():
                resp.set_cookie("ra_key", lan_token(), max_age=180 * 86400,
                                samesite="Lax", path="/")
            return resp
    return ("页面文件缺失：docs/index.html", 500)


@app.get("/minicpm-voice.js")
@app.get("/minicpm-voice-worklet.js")
def minicpm_voice_script():
    return send_from_directory(BASE / "docs", request.path[1:], max_age=0)


@app.get("/manifest.json")
@app.get("/icon-192.png")
@app.get("/icon-512.png")
@app.get("/apple-touch-icon.png")
def pwa_assets():
    """手机"添加到主屏幕"要的几样东西，和页面放一起，改了立刻生效。"""
    return send_from_directory(BASE / "docs", request.path[1:], max_age=0)


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/<path:name>")
def docs_static(name):
    """docs/ 下其余静态资源（qr.js、图标、字体等）。
    漏了这条会很难查：页面能开，但 qr.js 404 → qrcode 未定义，二维码永远不出来。"""
    docs_dir = (BASE / "docs").resolve()
    try:
        target = (docs_dir / name).resolve()
        if target.is_file() and (target == docs_dir or docs_dir in target.parents):
            return send_from_directory(docs_dir, name, max_age=0)
    except Exception:
        pass
    return ("", 404)


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
        # 能力探测：网页端据此判断"连上的引擎是不是旧版"。
        # 老实现在改完之后不会自动重启，曾出现过"页面是新的、引擎还是两天前的旧代码"
        # 却毫无提示的情况。加一项功能就往这里加一个标记，页面会自己发现落后。
        "features": FEATURES,
        "ollama": bool(models),
        "models": [m.get("name") for m in models],
        # 会调工具的模型有哪些 —— 取稿 Agent 只能靠它们
        "tool_models": [m.get("name") for m in models if "tools" in (m.get("capabilities") or [])],
        "fetch": _fetch_caps(),
        "voices": len(list_voices()),
        "clients": len(_subscribers),
        "tts": {"provider": CFG.get("tts_provider") or "edge",
                "local_url": CFG.get("tts_local_url") or ""},
        "books": SHELF.stats()["count"],
        "config": {k: v for k, v in CFG.items()
                   if k not in ("lan_token", "tts_local_key")},
    })


_fetch_caps_cache: dict = {}


def _fetch_caps() -> dict:
    """抓取能力自述：用不用得上专业提取库、走不走代理。"""
    if _fetch_caps_cache:
        return _fetch_caps_cache
    try:
        import trafilatura  # noqa: F401
        has_traf = True
    except Exception:
        has_traf = False
    try:
        proxies = webfetch._proxy_candidates(CFG.get("fetch_proxy", "auto"))
        proxy = proxies[0] if proxies and proxies[0] else ""
    except Exception:
        proxy = ""
    _fetch_caps_cache.update({
        "engine": "webfetch",
        "trafilatura": has_traf,
        "proxy": proxy,
        "agent_ready": has_traf,
    })
    return _fetch_caps_cache


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


# --------------------------------------------------------------------------
# 登录态
#
# 知乎、小红书这类站**没有登录态就是不给内容**：裸请求是 403（一个 zse
# 挑战页），带上浏览器 cookie 但没登录就变成软 404，只有带上登录凭证
# （知乎的 z_c0）才是正文。所以这里做三件事：借登录态、存登录态、验登录态。
#
# 浏览器 cookie 库本身读不了 —— Chrome 127+ 的 App-Bound Encryption 让
# Local State 里只剩 app_bound_encrypted_key，解密要走 COM 提权接口。
# 所以走 CDP：让浏览器自己把 cookie 交出来（能拿到 HttpOnly 的 z_c0）。
# --------------------------------------------------------------------------

# 专用浏览器配置目录。**必须是独立目录**：Chrome 136 起不允许在默认
# user-data-dir 上开调试端口（防止恶意程序接管你的日常浏览器）。
BROWSER_PROFILE_DIR = BASE / "browser-profile"


def _browser_exe() -> str:
    """找一个能开调试端口的 Chromium 系浏览器。"""
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pf86) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe" if local else None,
        Path(pf86) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Microsoft/Edge/Application/msedge.exe",
        Path(local) / "Microsoft/Edge/Application/msedge.exe" if local else None,
    ]
    for c in cands:
        try:
            if c and c.exists():
                return str(c)
        except OSError:
            continue
    return ""


def _open_login_window(home: str = "https://www.zhihu.com/") -> dict:
    """开一个带调试端口的浏览器窗口，让用户在里面登录一次。

    用独立 profile，所以不会碰你日常浏览器；登录一次以后
    cookie 就存在这个 profile 里，引擎随时能取。
    """
    exe = _browser_exe()
    if not exe:
        return {"ok": False, "error": "没在本机找到 Chrome 或 Edge"}
    port = int(CFG.get("browser_login_port", 9222))
    already = LOGIN.probe_browsers([port])
    if already:
        return {"ok": True, "already": True, "port": port,
                "browser": already[0].get("browser"),
                "note": f"127.0.0.1:{port} 上已经有浏览器在跑了，直接用它"}
    try:
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [exe, f"--remote-debugging-port={port}",
             f"--user-data-dir={BROWSER_PROFILE_DIR}",
             "--no-first-run", "--no-default-browser-check",
             "--new-window", home],
            close_fds=True)
    except Exception as e:
        return {"ok": False, "error": f"启动浏览器失败：{type(e).__name__}: {e}"}
    return {"ok": True, "port": port, "exe": exe, "home": home,
            "note": "浏览器已打开。在这个窗口里登录一次知乎，然后回来点「从浏览器导入」。"}


@app.get("/api/login")
def api_login_get():
    """当前登录态一览：配了哪些站、有没有能一键导入的浏览器。"""
    st = LOGIN.status()
    st["browsers"] = LOGIN.probe_browsers()
    st["exe"] = _browser_exe()
    st["profile_dir"] = str(BROWSER_PROFILE_DIR)
    st["port"] = int(CFG.get("browser_login_port", 9222))
    st["use_login"] = bool(CFG.get("use_login", True))
    st["browser_fetch"] = bool(CFG.get("browser_fetch", True))
    return jsonify(st)


@app.post("/api/login")
def api_login_post():
    """登录态操作。一个端点管五种动作，前端好接。

    action=import  从本机浏览器把 cookie 整套搬过来（可带 warmup 预热）
    action=paste   手工粘贴 Cookie（不需要浏览器配合）
    action=clear   清掉某个站或全部
    action=probe   看看哪些调试端口开着
    action=open    开一个带调试端口的登录窗口
    action=test    用当前登录态试抓一个链接，看通不通
    """
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "import").strip()

    if action == "probe":
        return jsonify({"ok": True, "browsers": LOGIN.probe_browsers()})

    if action == "open":
        return jsonify(_open_login_window(body.get("home") or "https://www.zhihu.com/"))

    if action == "import":
        warm = body.get("warmup") or []
        if isinstance(warm, str):
            warm = [warm] if warm else []
        if not warm and body.get("url"):
            warm = LOGIN.warmup_urls_for(body["url"])
        r = LOGIN.import_from_cdp(warmup=warm or None)
        if r.get("ok"):
            log.info("登录态导入：端口 %s，导入 %s 个站", r.get("port"),
                     len(r.get("imported") or []))
        return jsonify(r), (200 if r.get("ok") else 400)

    if action == "paste":
        raw = body.get("cookie") or ""
        domain = (body.get("domain") or "").strip().lower()
        jar = LOGIN.parse_cookie_text(raw)
        if not jar:
            return jsonify({"ok": False,
                            "error": "没能从这段文本里读出 cookie。"
                                     "至少要像 `z_c0=xxx; d_c0=yyy` 这样"}), 400
        if not domain:
            return jsonify({"ok": False, "error": "请指定这是哪个站的 cookie"}), 400
        # 先归一域名，保证和 for_url() 查的是同一个键
        dom = LOGIN.base_domain(domain) or re.sub(r"^www\.", "", domain)
        if not LOGIN.base_domain(domain):
            # 不在已知站点表里也能存，但补一条记录，状态面板才看得到
            LOGIN.SITES.setdefault(dom, {"name": dom, "referer": f"https://{dom}/",
                                         "关键 cookie": [], "hint": "自定义站点"})
        ua = (body.get("ua") or "").strip() or webfetch.UA_DESKTOP
        entry = LOGIN.set_site(dom, jar, ua=ua, source="manual",
                               note=body.get("note") or "",
                               merge=bool(body.get("merge", True)))
        return jsonify({"ok": True, "domain": dom,
                        "count": len(entry.get("cookie") or {}),
                        "added": len(jar), "keys": sorted(jar)[:14],
                        "has_login": LOGIN._looks_logged_in(dom, entry.get("cookie") or {}),
                        "entry": {k: v for k, v in entry.items() if k != "cookie"}})

    if action == "clear":
        n = LOGIN.clear((body.get("domain") or "").strip().lower())
        return jsonify({"ok": True, "cleared": n})

    if action == "test":
        url = (body.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return jsonify({"ok": False, "error": "给个 http/https 链接"}), 400
        got = LOGIN.for_url(url)
        d = webfetch.fetch(url,
                           timeout=float(CFG.get("fetch_timeout", 25)),
                           proxy=CFG.get("fetch_proxy", "auto"),
                           login=bool(CFG.get("use_login", True)),
                           allow_browser=False)
        return jsonify({
            "ok": bool(d.get("ok")),
            "error": d.get("error"),
            "used_login": bool(got),
            "domain": (got or {}).get("domain"),
            "has_login": LOGIN._looks_logged_in((got or {}).get("domain") or "",
                                                (got or {}).get("jar") or {})
            if got else False,
            "title": d.get("title"), "chars": d.get("chars"),
            "quality": d.get("quality"), "route": d.get("route"),
            "head": (d.get("text") or "")[:260],
            "diag": d.get("diag"),
        })

    return jsonify({"ok": False, "error": f"不认识的动作：{action}"}), 400


@app.get("/api/fetch")
@app.post("/api/fetch")
def api_fetch():
    """把一个网页抓成可朗读的正文。剪贴板里是链接、Obsidian 里贴了链接都走这里。

    真正的活儿在 webfetch 里（编码探测 / 站点适配 / 段落重排 / 噪声清理 /
    质量评分）。这里只负责取参数、做规范化、把诊断信息一并回给调用方 ——
    让前端能说清"为什么没抓到"，而不是丢一句"抓取失败"。
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
    else:
        body = request.args
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return jsonify({"ok": False, "error": "不是有效的 http/https 链接"}), 400

    mode = (body.get("mode") or "auto").strip()
    try:
        d = webfetch.fetch(url, mode=mode,
                           timeout=float(CFG.get("fetch_timeout", 25)),
                           proxy=CFG.get("fetch_proxy", "auto"),
                           login=bool(CFG.get("use_login", True)),
                           allow_browser=bool(CFG.get("browser_fetch", True)))
    except Exception as e:
        log.exception("抓取网页失败")
        return jsonify({"ok": False, "error": f"抓取失败：{type(e).__name__}: {e}"}), 502

    if not d.get("ok"):
        return jsonify({"ok": False, "error": d.get("error") or "没能提取到正文",
                        "blocked": d.get("blocked"),
                        "need_login": bool(d.get("need_login")),
                        "diag": d.get("diag")}), 422

    # 正文原样带回，**不要**在这里 clean_text：
    # clean_text 会把 "#/-/>" 这些结构标记一起洗掉，而下游 /api/prepare 正是靠
    # 这些标记认标题、条目、引文。洗掉之后每一次抓取都退化成"全是普通段落"，
    # 语气分层（标题慢读、条目紧凑、引用留白）整个失效 —— 网页读着就是平的。
    # 读音规整由 prepare 里的 do_normalize 负责，不在这里做。
    text = d["text"]
    return jsonify({"ok": True, "title": d.get("title", ""), "text": text,
                    "chars": len(text), "url": d.get("url"),
                    "quality": d.get("quality"), "grade": d.get("grade"),
                    "paras": d.get("paras"), "route": d.get("route"),
                    "dropped": d.get("dropped"),
                    "diag": d.get("diag")})


@app.post("/api/agent")
def api_agent():
    """让会调工具的小模型主导"把一个链接变成朗读稿"。

    分两级：先走零模型开销的确定性抓取（通常不到 1 秒），抓得够干净就直接返回；
    抓不到、抓得差、或用户要求「深度清理」时才唤醒模型，让它换策略重抓、
    或挑出稿子头尾的网页垃圾。

    body:
        url          要抓的链接（也可以只给 text，会从里面找链接）
        text         原始输入，便于"帮我读这篇 https://…"这种整句
        clean        true = 强制走模型深度清理
        model        指定模型，默认 agent_model 或 small_model
        max_calls    工具调用上限
        retry        抓不到时也让模型硬试一次（默认关：多半是白烧时间）
    """
    body = request.get_json(silent=True) or {}
    raw = (body.get("text") or "").strip()
    url = (body.get("url") or "").strip() or webagent.extract_url(raw)
    if not re.match(r"^https?://", url, re.I):
        return jsonify({"ok": False, "error": "没找到有效的 http/https 链接"}), 400

    model = pick_agent_model(body.get("model") or "")
    # Ollama 没开也算不上错 —— 抓得干净时压根不需要模型。把"能不能用模型"
    # 交给 run 内部判断，而不是在这里一刀切 503。
    has_ollama = bool(ollama_models())

    do_clean = bool(body.get("clean"))
    try:
        d = webagent.run(
            url, model=model,
            ollama_url=CFG["ollama_url"],
            num_ctx=int(CFG.get("num_ctx", 32768)),
            keep_alive=str(CFG.get("keep_alive", "30m")),
            auto=body.get("auto", True) is not False,
            do_clean=do_clean,
            fetch_timeout=float(CFG.get("fetch_timeout", 25)),
            proxy=CFG.get("fetch_proxy", "auto"),
            retry_on_fail=bool(body.get("retry")),
            ollama_ok=has_ollama,
            use_login=bool(CFG.get("use_login", True)),
            allow_browser=bool(CFG.get("browser_fetch", True)),
            max_tool_calls=int(body.get("max_calls")
                               or CFG.get("agent_max_tool_calls", 4)),
        )
    except Exception as e:
        log.exception("取稿 Agent 失败")
        return jsonify({"ok": False, "error": f"取稿失败：{type(e).__name__}: {e}"}), 502

    # 同样保留结构标记，交给 /api/prepare 去认标题/条目/引文（见 api_fetch 的说明）
    return jsonify(d)


@app.get("/api/settings")
def api_settings_get():
    return jsonify({"ok": True, "settings": CFG.get("prefs") or {},
                    "config": {k: CFG[k] for k in ("normalize", "auto_voice", "max_len",
                                                   "long_doc_chars", "default_voice",
                                                   "rate", "pitch", "volume",
                                                   "small_model", "quality_model",
                                                   "agent_model", "fetch_proxy",
                                                   "fetch_timeout")}})


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


def _nlp_fingerprint() -> str:
    """分段/读音逻辑的版本指纹 —— 改了词典**或分段代码**，分段缓存都得失效。
    以前只算 pronounce.json 的 mtime，改完断句规则老书还在用旧分段，
    看起来像"修了个寂寞"。"""
    parts = []
    for name in ("pronounce.json", "text_nlp.py"):
        try:
            parts.append(str(int((BASE / name).stat().st_mtime)))
        except Exception:
            parts.append("0")
    return "-".join(parts)


@app.post("/api/prepare")
def api_prepare():
    """一步到位：清洗 -> （可选）AI -> 读音规整 -> 逐句。

    返回里除了兼容用的 segments（字符串数组），还多了 items：
       [{text, kind, pause, rate}]  —— pause 是这句之后该停多久（毫秒），
       kind 是 heading/list/quote/para，前端据此做停顿、语速层次和高亮。
    另外带上按内容推荐的音色与语言判定，前端可以直接采纳。

    书架的书带 book_id 来：纯直读（ai=none）的分段结果按
    「正文指纹+参数+词典版本」缓存进 books/<id>/prep.json，
    第二次打开同一本书直接秒回 —— 点开即听，不用再等几秒解析。
    """
    body = request.get_json(silent=True) or {}
    ai = body.get("ai") or "none"
    raw = body.get("text", "")
    do_norm = bool(body.get("normalize", CFG["normalize"]))
    max_len = int(body.get("max_len") or CFG["max_len"])
    # Markdown：前端导入 .md 时显式带 true，直接粘 MD 文本则由 text_nlp 自动识别。
    md_flag = body.get("md", None)
    md_mode = str(body.get("md_mode") or "balanced")
    if md_mode not in ("commute", "balanced", "study"):
        md_mode = "balanced"
    if not (raw or "").strip():
        return jsonify({"ok": False, "error": "没有可朗读的文本"}), 400

    # 分段缓存：只缓存「原文直读」。AI 模式的产出随模型/提示词变化，不进缓存。
    book_id = str(body.get("book_id") or "")
    prep_key = ""
    if ai == "none" and re.fullmatch(r"[0-9a-f]{12}", book_id):
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        prep_key = f"{digest}|{max_len}|{int(do_norm)}|{_nlp_fingerprint()}|md2"
        cached_plan = SHELF.get_prep(book_id, prep_key)
        if cached_plan:
            items = cached_plan.get("items") or []
            segments = cached_plan.get("segments") or [it.get("text") for it in items]
            md = cached_plan.get("md")
            # 一次缓存三档：换听法不用重新解析，也不用再发一次请求。
            if md and md.get("modes", {}).get(md_mode):
                md = dict(md, mode=md_mode)
                cur = md["modes"][md_mode]
                items, segments = cur["items"], cur["segments"]
            return jsonify({
                "ok": True,
                "text": raw,
                "segments": segments,
                "items": items,
                "md": md,
                "count": len(items),
                "style": {
                    "voice": body.get("voice") or CFG["default_voice"],
                    "rate": body.get("rate") or CFG["rate"],
                    "pitch": body.get("pitch") or CFG["pitch"],
                    "volume": body.get("volume") or CFG["volume"],
                    "note": "", "title": "",
                },
                "ai": "none", "ai_note": "", "ai_cached": False, "model": None,
                "long_doc": False, "source_chars": len(raw),
                "normalized": do_norm, "prep_cached": True,
            })

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
    # 结构解析要用「原文」：clean_text 会把 #、-、> 这些结构标记一起洗掉，
    # 拿洗过的纯文本去解析，标题/列表/引文永远识别不出来，kind 全是 para。
    plan_src = raw

    if ai in ("summarize", "polish"):
        try:
            final_text, style2, model, cached = ai_refine(source, ai, body)
            style.update(style2)
            plan_src = final_text          # AI 产出是纯文本，重新规整即可
        except Exception as e:
            # AI 不靠谱时绝不阻断朗读：退回原文，并把原因告诉调用方
            log.warning("AI 处理未完成，改为朗读清理后的原文: %s", e)
            ai_note = f"AI 处理未完成，已改为朗读清理后的原文。原因：{e}"
            ai = "none"
            model = None
            final_text = source
            plan_src = raw
        long_doc = ai == "summarize" and len(source) >= int(CFG.get("long_doc_chars", 6000))

    # AI 润色/摘要产出的是纯文本，结构标记已经没了，强制走普通路径。
    r = plan_text(plan_src, max_len=max_len, do_normalize=do_norm,
                  markdown=(md_flag if ai == "none" else False), md_mode=md_mode)
    segs = r["segments"]
    if not segs and r["text"]:
        segs = [r["text"]]
        r["items"] = [{"text": r["text"], "kind": "para", "pause": 660, "rate": 0,
                       "bid": -1, "si": 0}]

    if prep_key:                        # 首次解析完顺手存缓存，下次秒回
        try:
            entry = {"items": r["items"], "segments": segs,
                     "count": len(segs), "ts": time.time()}
            if r.get("md"):
                entry["md"] = r["md"]   # 三档朗读稿 + 渲染视图一起缓存
            SHELF.put_prep(book_id, prep_key, entry, len(raw))
        except Exception:
            pass

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
        "prep_cached": False,
        "md": r.get("md"),
    })


@app.post("/api/import")
def api_import():
    """Extract a locally selected document without retaining its contents."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "请选择要导入的文件。"}), 400
    try:
        extracted = document_import.extract_document(upload.filename, upload.read())
    except document_import.DocumentImportError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    book = None
    try:
        # 导入即入库：手机上最需要的就是"这本书还在，接着听"。
        book = SHELF.add_book(extracted["title"], extracted["text"],
                              extracted["type"], source=upload.filename)
    except SHELF.BookError as error:
        log.warning("入库失败（不影响本次朗读）: %s", error)
    return jsonify({"ok": True, **extracted, "chars": len(extracted["text"]),
                    "book": book})


# ---------------------------------------------------------------- 书架

@app.get("/api/books")
def api_books():
    sort = (request.args.get("sort") or "recent").lower()
    if sort not in ("recent", "title", "progress"):
        sort = "recent"
    return jsonify({"ok": True, "books": SHELF.list_books(sort), "sort": sort, **SHELF.stats()})


@app.get("/api/books/trash")
def api_books_trash():
    return jsonify({"ok": True, "books": SHELF.list_trash()})


@app.post("/api/books/trash/empty")
def api_books_trash_empty():
    return jsonify({"ok": True, "purged": SHELF.empty_trash()})


@app.post("/api/books/trash/<book_id>/restore")
def api_book_restore(book_id):
    try:
        return jsonify({"ok": True, "book": SHELF.restore_book(book_id)})
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@app.delete("/api/books/trash/<book_id>")
def api_book_purge(book_id):
    SHELF.purge_book(book_id)
    return jsonify({"ok": True})


@app.post("/api/books/<book_id>/rename")
def api_book_rename(book_id):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "book": SHELF.rename_book(book_id, body.get("title") or "")})
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@app.post("/api/books/<book_id>/pin")
def api_book_pin(book_id):
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "book": SHELF.set_pin(book_id, bool(body.get("on")))})
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@app.get("/api/books/<book_id>")
def api_book(book_id):
    try:
        return jsonify({"ok": True, "book": SHELF.get_book(book_id)})
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 404


@app.post("/api/books/<book_id>/mdmode")
def api_book_mdmode(book_id):
    """记住 Markdown 听法档位，手机/电脑共用同一档。"""
    body = request.get_json(silent=True) or {}
    try:
        book = SHELF.set_md_mode(book_id, str(body.get("mode") or ""))
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    return jsonify({"ok": True, "book": book})


@app.post("/api/books")
def api_book_add():
    body = request.get_json(silent=True) or {}
    try:
        book = SHELF.add_book(body.get("title") or "", body.get("text") or "",
                              body.get("type") or "txt", body.get("source") or "")
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    return jsonify({"ok": True, "book": book})


@app.post("/api/books/<book_id>/progress")
def api_book_progress(book_id):
    body = request.get_json(silent=True) or {}
    try:
        book = SHELF.save_progress(book_id, body.get("next") or 0, body.get("total") or 0)
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    return jsonify({"ok": True, "book": book})


@app.delete("/api/books/<book_id>")
def api_book_delete(book_id):
    try:
        SHELF.delete_book(book_id)
    except SHELF.BookError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 后台预合成全文
# CUDA Graph 解码后吞吐 2 倍实时，「边听边备」重新成立 —— 但不能无脑并发：
# 起播头几句、播放缓冲见底时必须独占 GPU（hold=true），否则前台请求排在
# 预合成后面，又是一句一卡。所以把"何时让路"交给前端判断：它知道自己缓冲
# 有多厚 —— 缓冲 <3 句喊停，≥6 句放行（见 docs 里的 pollPresynth）。
# 没在播的时候维持老规矩：前台安静 PRESYNTH_IDLE 秒就往后备整本。

_presynth = {
    "running": False, "job": 0, "total": 0, "done": 0,
    "fail": 0, "book": "", "label": "", "started": 0.0, "finished": 0.0,
    "error": "", "hold": False, "at": 0, "jobs": [], "args": None,
}
_presynth_lock = threading.Lock()
_synth_gate = threading.Condition()     # 前台/预合成共用 GPU 的闸门
_fg_busy = {"n": 0}                     # 正在跑的前台合成数
_play_touch = {"ts": 0.0}               # 最近一次前台合成的时间；预合成见了要让路

# --------------------------------------------------------------------------
# 正在合成什么（电脑端/手机端都看得到，也能一键停）
#
# 本地大模型一次只同时合成 COSY_TTS_CONCURRENCY 句（默认 2）。手机"听着卡"
# 最常见的原因不是网络，而是这台机器同时在替别的段落干活：预合成往后备整本、
# 另一台设备开着页面在预取、测速还没跑完 —— 全在抢同两个槽位。以前这些只有
# 一行"预合成 N/M"，看不到"眼下这一句是谁在要"。
# 所以每句合成进来都登记一笔，前端轮询 /api/tasks 画成一张实时清单。
# --------------------------------------------------------------------------
_jobs = {}                       # jid -> {id, kind, text, t, ...}
_jobs_lock = threading.Lock()
_job_seq = [0]

JOB_KINDS = {"fg": "播放", "pre": "预合成", "bench": "测速", "api": "接口"}

# 暂停后台多久（秒）。到点自己恢复，不用用户记着回来点"继续"。
_quiet = {"until": 0.0}


def _job_open(kind: str, text: str, **meta) -> int:
    with _jobs_lock:
        _job_seq[0] += 1
        jid = _job_seq[0]
        _jobs[jid] = {
            "id": jid, "kind": kind,
            "text": (text or "").strip().replace("\n", " ")[:46],
            "chars": len(text or ""), "t": time.time(), **meta,
        }
        return jid


def _job_close(jid: int) -> None:
    with _jobs_lock:
        _jobs.pop(jid, None)


def _jobs_rows() -> list:
    now = time.time()
    with _jobs_lock:
        rows = [dict(r) for r in _jobs.values()]
    for r in rows:
        r["age"] = round(now - r["t"], 1)
    rows.sort(key=lambda r: r["t"])
    return rows


def _client_of() -> dict:
    """这一句是哪台设备要的：内网还是异地，手机还是电脑。

    面板上分得清"谁的活"，才敢放心点停止 —— 停错设备比卡更难受。
    """
    ip = request.remote_addr or ""
    ua = (request.headers.get("User-Agent") or "").lower()
    phone = ("mobile" in ua or "android" in ua or "iphone" in ua)
    return {"dev": "手机" if phone else "电脑",
            "src": "lan" if _ip_is_lan(ip) else "remote"}


def _quiet_left() -> int:
    return max(0, int(round(_quiet["until"] - time.time())))


def _pre_label() -> str:
    with _presynth_lock:
        return str(_presynth.get("label") or "全文")


PRESYNTH_MAX_SEGS = 6000        # 一本书的合理上限，防误传整站文章
PRESYNTH_IDLE = 25.0            # 前台安静这么久，预合成才动手（短暂停不打扰）


# --------------------------------------------------------------------------
# 播放期间别让系统睡
#
# 这条是实测出来的大头：机器一进入低功耗待机（屏幕熄灭 / Modern Standby），
# 显卡就会被压在 400～800MHz、功耗 28W（满负载能到 2775MHz / 165W），
# 同一句话的合成时间直接从 9.9 秒变成 22 秒，RTF 1.07 → 2.2 —— 长文必然卡。
#
# 而「躺着听书」恰恰是最常见的使用方式，听众根本不看屏幕。所以只要在播
# （前端 POST /api/presynth/hold {on:true}），就在这里替系统按住一个电源请求：
# SetThreadExecutionState 是**按线程**生效的，必须由一条活着的线程持续声明，
# 线程一退就失效，所以专门起一条常驻线程。恢复用 ES_CONTINUOUS 收尾。
# --------------------------------------------------------------------------
_awake = {"on": False, "thread": None, "lock": threading.Lock(), "last": 0.0}
AWAKE_IDLE = 900.0     # 一直没合成请求就认为人走了，自动松开（页面被关掉的情况）


def _awake_touch() -> None:
    """记一笔"还在用"。页面关掉后前端不会再发 hold，靠这个超时兜底。"""
    _awake["last"] = time.time()


def _awake_loop() -> None:
    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:                                        # noqa: BLE001
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if CFG.get("keep_display_awake", True):
        flags |= ES_DISPLAY_REQUIRED        # 屏幕也留着，GPU 才不会掉到最低档
    try:
        while _awake["on"]:
            kernel32.SetThreadExecutionState(flags)
            for _ in range(30):             # 30 秒补一次，旗标有超时
                if not _awake["on"]:
                    break
                time.sleep(1)
            # 一直没人来合成，多半是页面被关掉了，自己松开，别让机器整夜不睡
            if _awake["last"] and time.time() - _awake["last"] > AWAKE_IDLE:
                log.info("已 %.0f 分钟没有合成请求，自动松开电源请求", AWAKE_IDLE / 60)
                _awake["on"] = False
                break
    finally:
        try:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:                                    # noqa: BLE001
            pass


def _gpu_burst_kick() -> None:
    """让本机 TTS 服务跑一次几秒的 GPU burst，把 DVFS 从待机的 500MHz
    顶回高频。机器已经睡着的话，电源请求只能防它继续睡、救不了频率 ——
    这个 burst 才是拉频的。放后台线程，不拖慢起播。"""
    base = (CFG.get("tts_local_url") or "").strip()
    if not base:
        return
    def _go():
        try:
            requests.post(f"{TTS.normalize_base(base)}/warmup",
                          headers=TTS._headers(CFG.get("tts_local_key") or ""),
                          timeout=30)
        except Exception:                                      # noqa: BLE001
            pass
    threading.Thread(target=_go, daemon=True, name="gpu-burst").start()


def _awake_hold(on: bool) -> None:
    """播放时按住电源请求，停下就放开。重复调用无副作用。"""
    if os.name != "nt":
        return
    with _awake["lock"]:
        first = on and not _awake["on"]
        if on == _awake["on"]:
            return
        _awake["on"] = on
        if on:
            if not (_awake["thread"] and _awake["thread"].is_alive()):
                _awake["thread"] = threading.Thread(
                    target=_awake_loop, daemon=True, name="keep-awake")
                _awake["thread"].start()
            _awake_touch()
            if first and CFG.get("keep_display_awake", True):
                _gpu_burst_kick()
            log.info("播放中：先不让系统睡（否则显卡掉到 500MHz，合成慢一倍）")
        else:
            log.info("停止播放：恢复系统的睡眠策略")


def _cache_limit_now() -> int:
    """当前这本书整本的音频都留着。

    预合成把 464 句备齐了，缓存上限却还是 400 —— 一超就按 LRU 删，
    删掉的正好是最早合成、也就是开头那几十句，听到那儿又得重新合成。
    所以上限跟着书的长度走，体积由 cache_mb 兜底。
    """
    with _presynth_lock:
        total = int(_presynth.get("total") or 0)
    base = int(CFG["cache_limit"])
    return max(base, total + 80) if total > 0 else base


def _presynth_turn(stale) -> bool:
    """等一个「GPU 闲着」的时机；返回 False 表示这个 job 已经作废。

    让路条件分两种：
    - hold=true：前端明确喊停（起播头几句、缓冲见底），一句都不许合成；
    - hold=false 且正在播放（电源请求按着）：CUDA Graph 后吞吐 2 倍实时，
      缓冲够厚时可以见缝插针，只需避让正在跑的前台请求；
    - hold=false 且没在播：维持老规矩 —— 前台安静 PRESYNTH_IDLE 秒才动手。
    """
    while True:
        if stale():
            return False
        with _synth_gate:
            busy = (_fg_busy["n"] > 0
                    or _presynth["hold"]
                    or time.time() < _quiet["until"]
                    or (not _awake["on"]
                        and time.time() - _play_touch["ts"] < PRESYNTH_IDLE))
            if not busy:
                return True
            _synth_gate.wait(0.5)



def _seg_rate(base_rate: int, offset) -> str:
    """复刻前端 segRate()：全局语速 + 该句偏移，夹在 [-40, 50]。
    缓存键里是字符串，差一个字符就是两份音频，必须一模一样。"""
    try:
        off = int(offset or 0)
    except (TypeError, ValueError):
        off = 0
    v = max(-40, min(50, int(base_rate or 0) + off))
    return (f"+{v}%" if v >= 0 else f"{v}%")


def _presynth_worker(my_job: int, jobs: list, args: dict, base_at: int = 0) -> None:
    """job 号就是停止信号：开始新任务时号数 +1，旧 worker 立刻作废。
    以前用布尔 cancel —— 旧线程还没退出时新任务把它复位了，旧线程
    永远不退，两个线程抢着写进度，统计出过 4/2 这种鬼数字。"""
    voice = args["voice"]
    base_rate = int(args.get("rate") or 0)
    pitch = args["pitch"]
    volume = args["volume"]
    provider = args["provider"]
    model = args["model"]
    # 发起整本预合成的那台设备决定音频格式：手机在外发起 → 备 MP3，
    # 电脑在家发起 → 备 WAV。跟播放时的选择保持一致，才吃得到缓存。
    fmt = args.get("fmt") or None

    def stale() -> bool:
        with _presynth_lock:
            return _presynth["job"] != my_job

    done = base_at           # done 用绝对下标计数，前端进度条才对得上
    fail = 0
    for k, (text, off) in enumerate(jobs):
        with _presynth_lock:
            if _presynth["job"] != my_job:
                return
            _presynth["at"] = base_at + k       # 记着备到第几句，暂停后从这里续
        # 前台在合成 / 正在播 / 刚播过：一律让路。让路的粒度就是一句，
        # 所以「一句」不能太长 —— 这也是 max_len 别调太大的原因。
        if not _presynth_turn(stale):
            return
        try:
            jid = _job_open("pre", text, dev=args.get("dev") or "电脑",
                            src=args.get("src") or "", label=_pre_label(),
                            idx=base_at + k + 1, total=len(jobs))
            try:
                synth(text, voice, _seg_rate(base_rate, off), pitch, volume,
                      provider, model, fmt)
            finally:
                _job_close(jid)
            done += 1
        except BaseException as e:          # 单句失败不停整本书（含被安全策略拦下的删除）
            fail += 1
            log.warning("预合成第 %d/%d 句失败: %s", done + fail,
                        len(jobs), e)
        with _presynth_lock:
            if _presynth["job"] == my_job:
                _presynth["done"], _presynth["fail"] = done, fail
    with _presynth_lock:
        if _presynth["job"] == my_job:
            _presynth["running"] = False
            _presynth["finished"] = time.time()
            log.info("预合成结束：%s —— 完成 %d / 失败 %d / 共 %d",
                     _presynth["label"], done, fail, _presynth["total"])


def _start_presynth(jobs: list, args: dict, start_at: int = 0) -> None:
    """jobs 永远是**整本**的列表，start_at 是绝对下标（第几句）。

    以前给 worker 传的是切过的子列表，at 却按绝对下标记 —— 暂停后
    「从第 8 句续」会算成子列表的第 8 项，直接越界、一句都不做。
    """
    total = len(jobs)
    start_at = max(0, min(int(start_at), total - 1))
    with _presynth_lock:
        _presynth["job"] += 1               # 号数一变，旧 worker 自动作废
        _presynth.update(running=True, total=total,
                         done=start_at, fail=0, started=time.time(),
                         finished=0.0, error="", at=start_at,
                         jobs=list(jobs), args=dict(args),
                         book=args.get("book") or "",
                         label=args.get("label") or "全文")
        my_job = _presynth["job"]
    threading.Thread(target=_presynth_worker,
                     args=(my_job, jobs[start_at:], args, start_at),
                     daemon=True, name="presynth").start()


def _presynth_seek(at: int) -> None:
    """从第 at 句重新开工（只往前跳，不回头）。

    播到一半暂停时，前端把播放位置传过来 —— 已经听过的部分没必要再备，
    直接把队列挪到身后，省下来的 GPU 全用在「马上要听的那几句」上。
    """
    with _presynth_lock:
        jobs = list(_presynth.get("jobs") or [])
        args = _presynth.get("args")
        running = _presynth["running"]
        cur = int(_presynth.get("at") or 0)
    if not jobs or not args or not running:
        return
    at = max(0, min(int(at), len(jobs) - 1))
    if at <= cur:
        return
    _start_presynth(jobs, args, start_at=at)


@app.post("/api/presynth")
def api_presynth_start():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    jobs = []
    for it in items[:PRESYNTH_MAX_SEGS]:
        text = (it.get("text") or "").strip() if isinstance(it, dict) else str(it).strip()
        if text:
            jobs.append((text, (it.get("rate") if isinstance(it, dict) else 0) or 0))
    if not jobs:
        return jsonify({"ok": False, "error": "没有可预合成的句子"}), 400
    args = {
        "voice": body.get("voice") or CFG["default_voice"],
        "rate": body.get("rate") or 0,
        "pitch": body.get("pitch") or "+0Hz",
        "volume": body.get("volume") or "+0%",
        "provider": (body.get("provider") or CFG.get("tts_provider") or "edge").lower(),
        "model": body.get("model") or "",
        "book": body.get("book") or "",
        "label": body.get("label") or "全文",
        # 谁发起就按谁的带宽备：异地发起备 MP3（体积 1/5），家里发起备 WAV
        "fmt": _audio_format_for_client(),
        # 面板上要能说清"这台手机要的"还是"家里电脑要的"
        **_client_of(),
    }
    if args["provider"] not in TTS.PROVIDERS:
        return jsonify({"ok": False, "error": f"不认识的语音引擎：{args['provider']}"}), 400
    try:
        start_at = max(0, int(body.get("from") or 0))
    except (TypeError, ValueError):
        start_at = 0
    start_at = min(start_at, len(jobs) - 1)
    _start_presynth(jobs, args, start_at=start_at)
    return jsonify({"ok": True, "total": len(jobs), "from": start_at})


@app.get("/api/presynth")
def api_presynth_status():
    with _presynth_lock:
        st = {k: _presynth[k] for k in
              ("running", "total", "done", "fail", "book", "label",
               "started", "finished", "error", "hold", "at")}
    with _synth_gate:
        st["fg"] = _fg_busy["n"]
        st["idle"] = round(time.time() - _play_touch["ts"], 1)
    st["quiet_left"] = _quiet_left()      # >0 表示"暂停后台"还在生效中
    return jsonify({"ok": True, **st})


@app.post("/api/presynth/hold")
def api_presynth_hold():
    """播放器开播 → hold=true（预合成立刻让路）；暂停/停止 → hold=false。

    传了 at 就顺手把预合成队列挪到播放位置后面 —— 已经听过的不用再备。
    playing 是独立的"还在播"信号（只管电源请求）：CUDA Graph 之后合成吞吐
    2 倍实时，前端会在缓冲厚的时候主动 hold=false 让预合成见缝插针往后备，
    但此时人还在听，电源请求必须继续按着 —— 所以两个标志不能共用一个值。
    旧前端不带 playing，退回老语义（playing = on）。
    """
    body = request.get_json(silent=True) or {}
    on = bool(body.get("on"))
    playing = bool(body["playing"]) if "playing" in body else on
    new_at = None
    if not on and body.get("at") is not None:
        try:
            new_at = max(0, int(body.get("at")))
        except (TypeError, ValueError):
            new_at = None
    with _synth_gate:
        _presynth["hold"] = on
        _synth_gate.notify_all()
    _awake_hold(playing)
    if new_at is not None:
        _presynth_seek(new_at)
    with _presynth_lock:
        return jsonify({"ok": True, "hold": on, "at": _presynth["at"],
                        "running": _presynth["running"]})


@app.post("/api/presynth/cancel")
def api_presynth_cancel():
    with _presynth_lock:
        if _presynth["running"]:
            _presynth["job"] += 1           # 旧 worker 见号数变了自动退出
            _presynth["running"] = False
            _presynth["finished"] = time.time()
            _presynth["error"] = "已取消"
    return jsonify({"ok": True})


@app.post("/api/presynth/resume")
def api_presynth_resume():
    """「继续预合成」：把停下的任务从断点接回去。

    以前网页上只有「点一下＝取消」，停了 chip 就消失，再没有任何继续的入口
    —— 只能靠"播放→暂停"把自动任务勾回来，还常被「整本已备完」的标记挡住。
    jobs/args 在 cancel 时是留着的，所以这里不必让前端重发整本正文；
    引擎重启过（内存状态没了）才由前端退回 /api/presynth 重新提交。
    """
    body = request.get_json(silent=True) or {}
    with _presynth_lock:
        jobs = list(_presynth.get("jobs") or [])
        args = dict(_presynth.get("args") or {})
        st = {k: _presynth[k] for k in ("running", "at", "done", "book")}
        total = len(jobs)
    if not jobs or not args:
        return jsonify({"ok": False, "error": "没有可继续的任务"}), 400
    # 换过书 / 正文改过：旧队列已经不适用，让前端按当前稿子重开
    want_book = str(body.get("book") or "")
    if want_book and st["book"] and want_book != st["book"]:
        return jsonify({"ok": False, "error": "这个任务属于另一本书"}), 409
    try:
        want_total = int(body.get("total") or 0)
    except (TypeError, ValueError):
        want_total = 0
    if want_total and want_total != total:
        return jsonify({"ok": False, "error": "正文已经变了"}), 409
    if st["running"]:
        return jsonify({"ok": True, "running": True, "total": total, "from": st["at"]})
    try:
        at = int(body.get("at")) if body.get("at") is not None else int(st["at"] or 0)
    except (TypeError, ValueError):
        at = int(st["at"] or 0)
    # done 是绝对下标：已经备过的别退回去重算（前端还可能传更靠前的播放位置）
    at = max(at, int(st["done"] or 0))
    at = max(0, min(at, total - 1))
    _start_presynth(jobs, args, start_at=at)
    log.info("预合成继续：从第 %d/%d 句往后", at + 1, total)
    return jsonify({"ok": True, "running": True, "total": total, "from": at})


@app.get("/api/tasks")
def api_tasks():
    """眼下这台机器在替谁合成什么 —— 面板用它画实时清单。

    手机"卡"十有八九能在这里看出原因：明明只想听一句，下面却挂着预合成在备
    整本、另一个页面在预取、或者上一次测速还没跑完。看到就能停。
    """
    rows = _jobs_rows()
    with _presynth_lock:
        pre = {k: _presynth[k] for k in
               ("running", "hold", "done", "total", "at", "label", "book", "fail")}
    with _synth_gate:
        fg = _fg_busy["n"]
    try:
        conc = max(1, min(4, int(os.environ.get("COSY_TTS_CONCURRENCY", "2"))))
    except (TypeError, ValueError):
        conc = 2
    engine = (CFG.get("tts_engine") or "cosy").lower()
    is_moss = engine == "moss"       # ONNX session 串行
    is_qwen3 = engine == "qwen3"     # 1.7B 整句生成，服务内 _model_lock 也是串行
    if is_moss or is_qwen3:
        conc = 1                     # 别在清单里显示 GPU 的 2/2
    if (CFG.get("tts_provider") or "") == "local" and not is_moss:
        gpu = _gpu_clocks()
    else:
        gpu = {}
    return jsonify({
        "ok": True, "now": time.time(), "jobs": rows, "fg": fg,
        "conc": conc, "provider": CFG.get("tts_provider") or "edge",
        "backend": "onnx" if is_moss else ("qwen3" if is_qwen3 else "gpu"),
        "presynth": pre, "gpu": gpu, "quiet_left": _quiet_left(),
    })


@app.post("/api/tasks/stop")
def api_tasks_stop():
    """一键把不必要的活停掉。三种口径：

    - presynth：停止整本预合成。进度留在断点，随时能「继续预合成」；
    - pause   ：不取消任务，只是按住 minutes 分钟（到点自动继续）——
                临时想安静听完一章，又不想丢掉已经备好的队列；
    - all     ：停预合成 + 按住 30 分钟（"全都让路，只留我正在听的"）。
    - resume  ：取消按住，立刻放行。
    """
    body = request.get_json(silent=True) or {}
    scope = (body.get("scope") or "presynth").strip().lower()
    stopped = 0
    if scope in ("presynth", "all"):
        with _presynth_lock:
            if _presynth["running"]:
                _presynth["job"] += 1      # 旧 worker 见号数变了自动退出
                _presynth["running"] = False
                _presynth["finished"] = time.time()
                _presynth["error"] = "手动停止"
                stopped += 1
        with _synth_gate:
            _presynth["hold"] = True       # 别让前端下一拍又把它勾起来
            _synth_gate.notify_all()
    if scope in ("pause", "all"):
        try:
            mins = float(body.get("minutes") or 30)
        except (TypeError, ValueError):
            mins = 30.0
        mins = max(1.0, min(360.0, mins))
        _quiet["until"] = time.time() + mins * 60
        with _synth_gate:
            _presynth["hold"] = True
            _synth_gate.notify_all()
        log.info("后台合成暂停 %d 分钟", int(mins))
    elif scope == "resume":
        _quiet["until"] = 0.0
        with _synth_gate:
            _presynth["hold"] = False
            _synth_gate.notify_all()
    return jsonify({"ok": True, "scope": scope, "stopped": stopped,
                    "quiet_left": _quiet_left()})


# ---------------------------------------------------------------- 版本 / 重启 / 升级
# 旧版把「重启引擎」交给 readaloud:// 协议，没装协议处理器时点了毫无反应；
# 现在引擎自己重启自己，不依赖任何外部安装。

def _active_clients() -> int:
    try:
        return len(_subscribers)
    except Exception:
        return 0


_srv_ref = {"srv": None, "bind": "", "port": 0}
_STOPPED = {"v": False}
_extra_srvs = []          # 兼容端口（8756 等）的服务器，热重启时要一起收掉


def _stop_extra_servers() -> None:
    """热重启前必须关掉兼容端口。

    否则旧模块里那个 serve_forever 线程还抱着旧 app 对象继续跑，
    8759 已经换成新代码、8756 还是上一版 —— 老书签打开就是"新页面旧接口"，
    正是当初"导入点了没反应"的根。"""
    while _extra_srvs:
        s = _extra_srvs.pop()
        try:
            s.shutdown()
            s.server_close()
        except Exception as e:
            log.warning("关停兼容端口出错（继续）: %s", e)


def _extra_port_list() -> list:
    out = []
    for x in (CFG.get("extra_ports") or []):
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _reclaim_extra_ports() -> None:
    """收掉"上一次热重启"留下的兼容端口监听。

    以前 8756 上会越叠越多：Windows 的 SO_REUSEADDR 允许同一端口被重复绑定，
    每次热重启新模块都"绑成功"，但旧模块的线程还抱着旧 app 在那儿收请求，
    于是老书签打开时灵时不灵，版本还一阵一阵地旧 —— 用户看到的正是
    "手机导入完全没反应"那一类怪事。

    同一个进程里线程栈是可查的：顺着 serve_forever 的栈把 BaseServer 找出来
    逐个关掉（可能不止一个）。找不到就说明端口被别的程序占着，什么也不做。
    """
    import sys
    import threading
    from socketserver import BaseServer

    want = set(_extra_port_list()) - {int(_srv_ref.get("port") or 0)}
    if not want:
        return
    frames = sys._current_frames()
    me = threading.current_thread().ident
    killed = 0
    for t in threading.enumerate():
        if t.ident is None or t.ident == me or not t.is_alive():
            continue
        frame = frames.get(t.ident)
        depth = 0
        found = None
        while frame is not None and depth < 16 and found is None:
            for key in ("srv", "self", "server"):
                cand = frame.f_locals.get(key)
                if not isinstance(cand, BaseServer):
                    continue
                try:
                    if int(cand.server_address[1]) in want:
                        found = cand
                except Exception:
                    pass
                break
            frame = frame.f_back
            depth += 1
        if found is None:
            continue
        try:
            found.shutdown()
            found.server_close()
            killed += 1
        except Exception as e:
            log.warning("收掉兼容端口失败（继续）: %s", e)
    if killed:
        log.info("收掉 %d 个上次热重启遗留的兼容端口监听", killed)
        _rlog(f"收掉 {killed} 个遗留的兼容端口监听")
        time.sleep(0.3)   # 给 socket 一点时间真正关闭


def _can_load_new_code() -> tuple:
    """重启前先确认磁盘上的代码能编译 —— 语法错就别把现有服务关掉。"""
    try:
        src = (BASE / "server.py").read_text(encoding="utf-8")
        compile(src, str(BASE / "server.py"), "exec")
        return True, ""
    except Exception as e:
        return False, str(e)


def _restart_engine(delay: float = 0.4) -> bool:
    """同进程热重启：关掉监听 → 用 runpy 重新执行磁盘上的整份脚本。

    为什么不在新进程里重启自己：从 IDE / 终端 / 计划任务里启动时，父进程
    常在一个 Windows Job 里，父进程一退出子进程跟着被回收 —— 表现就是
    "点了重启，引擎直接没了"。同进程重新执行没有这个前提，进程号都不变，
    端口也能立刻重绑。
    """
    ok, err = _can_load_new_code()
    if not ok:
        log.error("新代码编译不过，保持当前进程继续服务: %s", err)
        return False
    threading.Thread(target=_soft_restart, daemon=True).start()
    return True


def _rlog(msg: str) -> None:
    """热重启的日志单独落一份文件。

    重启后原来的 stdout 可能已经跟着旧窗口/终端走了，出问题时"为什么重启没生效"
    在控制台里根本看不到。restart.log 就是给这个场景留的。"""
    try:
        p = BASE / "restart.log"
        if p.exists() and p.stat().st_size > 256 * 1024:
            p.write_text("", encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {msg}\n")
    except Exception:
        pass


def _soft_restart() -> None:
    global _STOPPED
    srv = _srv_ref.get("srv")
    log.info("开始热重启（pid %s）", os.getpid())
    _rlog(f"开始热重启，旧监听 {_srv_ref.get('bind')}:{_srv_ref.get('port')}")
    _STOPPED["v"] = True          # 旧副本的后台线程（自动升级）别再起作用
    time.sleep(0.2)               # 让当前请求先把响应发出去
    try:
        if srv:
            srv.shutdown()
            srv.server_close()
    except Exception as e:
        log.warning("关停旧监听时出错（继续）: %s", e)
    _srv_ref["srv"] = None
    _stop_extra_servers()
    try:
        import importlib
        import runpy
        # runpy 只重跑 server.py 自己：text_nlp / tts_engine / bookshelf 这些
        # 兄弟模块还留在 sys.modules 里，磁盘上改了也不生效 —— 表现就是
        # 「重启了，新逻辑却没上线」。这里把它们一并摘掉，让 runpy 重新导入。
        here = os.path.dirname(os.path.abspath(__file__))
        for name, mod in list(sys.modules.items()):
            if name == "__main__":
                continue
            f = getattr(mod, "__file__", None)
            if f and os.path.dirname(os.path.abspath(f)) == here:
                sys.modules.pop(name, None)
        importlib.invalidate_caches()
        _rlog("旧监听已关，开始跑新代码")
        runpy.run_path(str(BASE / "server.py"), run_name="__main__")
        _rlog("新代码已接管")
    except SystemExit:
        _rlog("新代码 SystemExit")
    except Exception:
        log.exception("热重启失败")
        _rlog("热重启失败：\n" + traceback.format_exc())
        try:                       # 兜底：至少把当前代码重新服务起来
            from werkzeug.serving import make_server
            srv2 = make_server(_srv_ref["bind"] or "127.0.0.1",
                               int(_srv_ref["port"] or CFG["port"]), app, threaded=True)
            _srv_ref["srv"] = srv2
            threading.Thread(target=srv2.serve_forever, daemon=True).start()
            _rlog("已用旧代码兜底重启")
        except Exception as e:
            log.error("兜底重启也失败，请手动启动引擎: %s", e)
            _rlog(f"兜底重启失败：{e}")


def _git(*args, timeout: float = 25) -> tuple:
    try:
        r = subprocess.run(["git", *args], cwd=str(BASE), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "git 超时（网络？）"
    except Exception as e:
        return 127, "", str(e)


def _repo_state() -> dict:
    if not (BASE / ".git").exists():
        return {"repo": False}
    rc, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=8)
    rc2, head, _ = _git("rev-parse", "--short", "HEAD", timeout=8)
    rc3, dirty, _ = _git("status", "--porcelain", timeout=15)
    return {"repo": rc == 0, "branch": branch if rc == 0 else "",
            "head": head if rc2 == 0 else "",
            "dirty": bool(dirty.strip()) if rc3 == 0 else True}


_remote_pref = {"order": ["origin", "ssh"]}   # 哪条路通就先用哪条（见 _fetch_remote）


def _ssh_url(url: str) -> str:
    """把 https://github.com/a/b.git 换成 git@github.com:a/b.git。"""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", (url or "").strip())
    return f"git@github.com:{m.group(1)}/{m.group(2)}.git" if m else ""


def _remote_url() -> str:
    rc, out, _ = _git("remote", "get-url", "origin", timeout=8)
    return out.strip() if rc == 0 else ""


def _fetch_remote(branch: str, timeout: float = 12) -> tuple:
    """拉远端引用，HTTPS 不通就改走 SSH；记住哪条通，下次先用它。

    这台机器上 Clash 对 github.com 的 HTTPS 是持续 502，api.github.com 却正常，
    SSH 22 也是通的 —— 结果就是"检查更新"永远报拉取失败，静默升级看着在跑，
    其实从来没查到过新版本。两条腿都试，通的那条排前面（不然每次都要等
    HTTPS 超时，按钮转二十秒）。返回 (rc, err, 用了哪条路)"""
    alt = _ssh_url(_remote_url())
    order = [k for k in _remote_pref["order"] if k == "origin" or alt]
    if "origin" not in order:
        order.insert(0, "origin")
    errs = []
    for kind in order:
        if kind == "origin":
            rc, _o, err = _git("fetch", "--quiet", "origin", branch, timeout=timeout)
            if rc == 0:
                _remote_pref["order"] = order
                return 0, "", "origin"
            errs.append("HTTPS: " + err[-120:])
        else:
            rc2, _o2, err2 = _git("fetch", "--quiet", alt,
                                  f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                                  timeout=timeout)
            if rc2 == 0:
                _remote_pref["order"] = [k for k in ("ssh", "origin") if k == "origin" or alt]
                log.info("HTTPS 拉不动，改走 SSH 拉到远端引用")
                return 0, "", alt
            errs.append("SSH: " + err2[-120:])
    return 1, " / ".join(errs)[-300:], ""


def _pull_remote(branch: str, timeout: float = 90) -> tuple:
    """升级用：先 origin，失败再试 SSH。返回 (rc, 输出, 用了哪条路)。"""
    alt = _ssh_url(_remote_url())
    rc, out, err = _git("pull", "--ff-only", "origin", branch, timeout=timeout)
    if rc == 0:
        return 0, out, "origin"
    if not alt:
        return rc, (err or out), "origin"
    rc2, out2, err2 = _git("pull", "--ff-only", alt, branch, timeout=timeout)
    if rc2 == 0:
        log.info("HTTPS 拉不动，改走 SSH 升级")
        return 0, out2, alt
    return rc2, " / ".join(x for x in (err, err2, out2) if x)[-300:], alt


def _check_update() -> dict:
    st = _repo_state()
    if not st.get("repo"):
        return {"ok": False, "error": "这个目录不是 git 仓库，没法自动升级。", **st}
    branch = st["branch"] or "main"
    rc, err, via = _fetch_remote(branch)
    behind, latest = 0, ""
    if rc == 0:
        rc2, out, _ = _git("rev-list", "--count", f"HEAD..origin/{branch}", timeout=15)
        if rc2 == 0 and out.isdigit():
            behind = int(out)
        rc3, subj, _ = _git("log", "-1", "--format=%s", f"origin/{branch}", timeout=10)
        latest = subj if rc3 == 0 else ""
    return {"ok": True, "version": ENGINE_VERSION, "behind": behind, "latest": latest,
            "via": via, "error": "" if rc == 0 else ("拉取失败：" + err[-160:]), **st}


@app.get("/api/version")
def api_version():
    return jsonify({"ok": True, "version": ENGINE_VERSION,
                    "python": sys.version.split()[0], "pid": os.getpid()})


@app.post("/api/restart")
def api_restart():
    if not _restart_engine(1.2):
        return jsonify({"ok": False, "error": "拉起新进程失败，看 engine.log"}), 500
    return jsonify({"ok": True, "restarting": True})


@app.get("/api/update")
def api_update_check():
    return jsonify(_check_update())


@app.post("/api/update")
def api_update_apply():
    st = _repo_state()
    if not st.get("repo"):
        return jsonify({"ok": False, "error": "不是 git 仓库，只能手动更新。"}), 400
    if st.get("dirty"):
        return jsonify({"ok": False, "error": "工作区有未提交的改动，先提交或还原再升级。"}), 409
    branch = st["branch"] or "main"
    rc, out, via = _pull_remote(branch)
    if rc != 0:
        return jsonify({"ok": False, "error": (out or "git pull 失败")[-300:]}), 502
    _restart_engine(1.6)
    return jsonify({"ok": True, "restarted": True, "via": via, "output": out[-400:]})


def _auto_update_worker() -> None:
    """静默升级：查到新版本就拉下来，等没人听（也没有预合成）时自动重启。
    宁可晚一点生效，也不要在用户听着书的时候把引擎掐了。"""
    if not CFG.get("auto_update", True):
        return
    time.sleep(20)
    while not _STOPPED["v"]:
        try:
            info = _check_update()
            if info.get("ok") and info.get("behind", 0) > 0:
                if info.get("dirty"):
                    log.info("有新版本，但工作区有改动，跳过自动升级")
                elif _active_clients() == 0 and not _presynth["running"]:
                    branch = info.get("branch") or "main"
                    rc, out, via = _pull_remote(branch)
                    if rc == 0:
                        log.info("静默升级完成（%s 个提交，走 %s），重启引擎", info["behind"], via)
                        _rlog(f"静默升级：{info['behind']} 个提交（{via}）")
                        _restart_engine(1.2)
                        return
                    log.warning("静默升级失败: %s", (out or "")[-200:])
                else:
                    log.info("有新版本，等当前会话结束再升级")
        except Exception as e:
            log.debug("自动升级检查失败: %s", e)
        time.sleep(600)


# ---------------------------------------------------------------- 语音引擎

# 本地 TTS 的探测结果缓存 30 秒 —— 页面每次加载都会问，别每次都去戳端口
_tts_probe = {"ts": 0.0, "url": "", "data": None}


def _tts_local_report(force: bool = False) -> dict:
    url = CFG.get("tts_local_url") or ""
    cached = _tts_probe["data"]
    if (not force and cached is not None and _tts_probe["url"] == url
            and time.time() - _tts_probe["ts"] < 30):
        return cached
    try:
        data = TTS.probe(url, CFG.get("tts_local_key") or "")
    except TTS.TTSConfigError as error:
        data = {"ok": False, "error": str(error), "models": [], "voices": []}
    _tts_probe.update(ts=time.time(), url=url, data=data)
    return data


# --- 本机 TTS：一键拉起 ---------------------------------------------------
# 用户不该知道"要在哪个 conda 环境里跑哪个脚本"。切到本机大模型时服务没起来，
# 引擎自己把它拉起来 —— 解释器按优先级探，脚本按端口猜，都可以在 config 里改。

_tts_proc = {"popen": None, "ts": 0.0}


def _tts_port() -> int:
    try:
        from urllib.parse import urlparse
        return int(urlparse(CFG.get("tts_local_url") or "").port or 0)
    except Exception:
        return 0


def _tts_python() -> str:
    """挑一个真能跑起这个脚本的解释器。
    两个服务用的是两套环境：CosyVoice2 要 conda env cosyvoice（pynini 只能 conda 装），
    Qwen3-TTS 用 .tts-venv。按脚本名选，否则拉起来第一行就 ModuleNotFoundError。"""
    explicit = CFG.get("tts_local_python") or os.environ.get("READALOUD_TTS_PYTHON") or ""
    if explicit and Path(explicit).exists():
        return explicit
    cosy_envs = [Path.home() / "miniconda3" / "envs" / "cosyvoice" / "python.exe",
                 Path.home() / "anaconda3" / "envs" / "cosyvoice" / "python.exe"]
    venv = BASE / ".tts-venv" / "Scripts" / "python.exe"
    is_cosy = "cosy" in _tts_script().name.lower()
    cands = (cosy_envs + [venv]) if is_cosy else ([venv] + cosy_envs)
    for c in cands:
        if Path(c).exists():
            return str(c)
    return sys.executable


def _tts_script() -> Path:
    if CFG.get("tts_local_script"):
        p = Path(CFG["tts_local_script"])
        return p if p.is_absolute() else BASE / p
    return BASE / ("tts_qwen_server.py" if _tts_port() == 8890 else "tts_cosy_server.py")


def _gpu_query(args: list) -> str:
    """nvidia-smi 单字段查询，提取行内第一个数字。
    新驱动会往输出里混一行 "Requested functionality has been deprecated"
    再跟实际值（如 "1800 MHz"），所以按整行找数字，别按行首判断。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=15,
                           creationflags=(0x08000000 if os.name == "nt" else 0))
        m = re.search(r"(\d+)", r.stdout or "")
        return m.group(1) if m else ""
    except Exception:                                          # noqa: BLE001
        return ""


def _gpu_apply_lock(elevate: bool = True) -> dict:
    """把显卡锁在高频（nvidia-smi -lgc）。启动本机 TTS 时调用：CosyVoice
    跑起来利用率只有三成，DVFS 会把卡按在几百 MHz —— 锁住之后待机也高频，
    换来的是合成全程满速。锁频要管理员：先直接试，没权限且允许提权就弹
    一次 UAC（用户点一次"是"即可，锁频持续到重启，之后不再弹）。

    「已经锁着」没法直接查（新驱动把 Applications Clocks 查询废弃了），
    用静置频率推断：锁着待机也有 1800，没锁只有两三百。"""
    val = str(CFG.get("gpu_clock_lock") or "").strip()
    if not val or val.lower() in ("0", "off", "none", "reset"):
        return {"applied": False, "reason": "gpu_clock_lock 未启用"}
    try:
        lo = max(1800, int(val))
    except ValueError:
        lo = 1800
    cap = int(_gpu_clocks().get("clk_max") or 0)
    hi = max(lo, cap)
    clk = _gpu_query(["nvidia-smi", "--query-gpu=clocks.sm", "--format=csv,noheader,nounits"])
    if clk.isdigit() and int(clk) >= lo:                       # 静置还这么高 = 锁着呢
        return {"applied": True, "already": True, "clk": clk}
    r = subprocess.run(["nvidia-smi", "-lgc", str(lo), str(hi)],
                       capture_output=True, text=True, timeout=15,
                       creationflags=(0x08000000 if os.name == "nt" else 0))
    if r.returncode == 0:
        log.info("已锁显卡频率 %d~%d MHz（合成全程满速；恢复用 nvidia-smi -rgc）", lo, hi)
        return {"applied": True, "clk": f"{lo}~{hi}"}
    if not elevate or not CFG.get("gpu_clock_elevate", True):
        return {"applied": False, "error": "无权限", "hint": "管理员跑一次 lock-gpu-clock.bat"}
    # 没权限：弹一次 UAC 提权。用户拒绝或超时就放弃，下次启动再试。
    ps = (f"Start-Process nvidia-smi -ArgumentList '-lgc {lo},{hi}' "
          f"-Verb RunAs -WindowStyle Hidden")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30,
                       creationflags=(0x08000000 if os.name == "nt" else 0))
    except subprocess.TimeoutExpired:
        return {"applied": False, "error": "提权弹窗没人理（30 秒超时）"}
    clk = _gpu_query(["nvidia-smi", "--query-gpu=clocks.sm", "--format=csv,noheader,nounits"])
    if clk.isdigit() and int(clk) >= lo:
        log.info("已锁显卡频率 %d~%d MHz（通过 UAC 提权）", lo, hi)
        return {"applied": True, "clk": f"{lo}~{hi}", "elevated": True}
    return {"applied": False,
            "error": "需要管理员权限（UAC 未确认）",
            "hint": "右键 lock-gpu-clock.bat 以管理员身份运行，或在弹窗里点「是」"}


def start_local_tts() -> dict:
    """后台拉起本机 TTS，立刻返回。真正就绪要看 /health（模型要加载十几秒）。"""
    lock = None
    # Nano 是纯 CPU 引擎，别为了它去锁显卡频率（还可能弹一次 UAC）
    if (os.name == "nt" and CFG.get("gpu_clock_lock")
            and (CFG.get("tts_engine") or "cosy") != "moss"):
        try:
            lock = _gpu_apply_lock()               # 合成慢的头号原因就是掉频
        except Exception as e:                     # noqa: BLE001
            lock = {"applied": False, "error": str(e)}
            log.warning("锁显卡频率失败: %s", e)
        if not lock.get("applied"):
            log.info("没锁成显卡频率：%s（%s）",
                     lock.get("error"), lock.get("hint", ""))
    if _tts_local_report(force=True).get("ok"):
        return {"ok": True, "already": True, "gpu_lock": lock,
                "msg": "本机 TTS 已经在跑了"}
    proc = _tts_proc.get("popen")
    if proc and proc.poll() is None:
        return {"ok": True, "starting": True, "pid": proc.pid, "msg": "正在启动中…"}
    script = _tts_script()
    if not script.exists():
        return {"ok": False, "error": f"找不到启动脚本 {script.name}"}
    port = _tts_port() or 8891
    py = _tts_python()
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 额外环境变量，写法 "K=V;K2=V2"。留给 TTS 服务自己的开关用，
    # 例如 COSY_FLOW_STEPS=6（余弦流匹配积分步数）、COSY_TTS_CONCURRENCY=2。
    for pair in str(CFG.get("tts_local_env") or "").replace(",", ";").split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip():
                env[k.strip()] = v.strip()
    if script.name.startswith("tts_moss"):
        # Nano 走 ONNX CUDA provider 时，要能找到 torch 自带的 CUDA 12 DLL
        cuda_dll = (Path.home() / "miniconda3" / "envs" / "cosyvoice" / "Lib"
                    / "site-packages" / "torch" / "lib")
        if cuda_dll.is_dir():
            env["PATH"] = str(cuda_dll) + os.pathsep + env.get("PATH", "")
    flags = (0x00000008 | 0x00000200 | 0x08000000) if os.name == "nt" else 0
    try:
        out = open(BASE / "tts.log", "ab")
        extra = CFG.get("tts_local_args") or ""
        if isinstance(extra, str):
            extra = extra.split()
        cmd = [py, str(script), "--port", str(port)] + [str(a) for a in extra]
        popen = subprocess.Popen(cmd, cwd=str(BASE),
                                 env=env, stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=flags,
                                 close_fds=True)
    except Exception as e:
        log.error("拉起本机 TTS 失败: %s", e)
        return {"ok": False, "error": f"启动失败：{e}"}
    _tts_proc.update(popen=popen, ts=time.time())
    log.info("已拉起本机 TTS：%s（pid %s，端口 %s，解释器 %s）",
             script.name, popen.pid, port, py)
    return {"ok": True, "starting": True, "pid": popen.pid, "python": py,
            "script": script.name, "port": port, "gpu_lock": lock,
            "msg": f"正在启动 {script.stem}，第一次加载模型约 10～60 秒"}


def _stop_other_tts(keep_port: int) -> list[int]:
    """切引擎时停掉其它本机 TTS 服务，把显存留给当前引擎。

    三套模型同时常驻会把 16GB 显存挤爆：实测 ONNX Runtime 报
    CUBLAS_STATUS_NOT_INITIALIZED，正在听的那套每句都 502，可 /health 依旧
    显示 ready —— 极具迷惑性。端口是本项目专用约定，只停这几个端口上的
    LISTENING 进程，不碰用户自己的服务（比如本机跑着的 Comni / Ollama）。
    """
    from urllib.parse import urlparse
    ports = set()
    for eid in LOCAL_ENGINES:
        try:
            ports.add(int(urlparse(_engine_profile(eid)["url"]).port or 0))
        except Exception:                                        # noqa: BLE001
            pass
    ports.discard(0)
    targets = ports - {keep_port}
    if not targets:
        return []
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15, creationflags=flags).stdout
    except Exception:                                            # noqa: BLE001
        return []
    stopped = []
    for line in (out or "").splitlines():
        cols = line.split()
        if len(cols) < 5 or cols[3].upper() != "LISTENING":
            continue
        try:
            port = int(cols[1].rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        pid = cols[4]
        if port not in targets or port in stopped or not pid.isdigit():
            continue
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F", "/T"],
                           capture_output=True, timeout=10, creationflags=flags)
            stopped.append(port)
            log.info("切引擎：停掉 %s 上的旧 TTS（pid %s）释放显存", port, pid)
        except Exception as e:                                   # noqa: BLE001
            log.warning("停掉 %s 上的旧 TTS 失败：%s", port, e)
    return stopped


def _tts_maybe_autostart() -> dict | None:
    """切到 local / 生成语音时顺手兜一下：没起来就拉起来。"""
    if not CFG.get("tts_local_autostart", True):
        return None
    if _tts_local_report(force=True).get("ok"):
        return None
    return start_local_tts()


def _tts_log_tail(lines: int = 12) -> str:
    """启动失败时把 tts.log 尾巴给前端 —— 少一行 ModuleNotFoundError，
    用户就得多花半小时猜"为什么点了没反应"。"""
    try:
        text = (BASE / "tts.log").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    rows = [r for r in text.strip().splitlines() if r.strip()]
    return "\n".join(rows[-lines:])[-1500:]


def _tts_config_snapshot() -> dict:
    return {"url": CFG.get("tts_local_url") or "",
            "model": CFG.get("tts_local_model") or "",
            "voice": CFG.get("tts_local_voice") or ""}


def _adopt_local_tts(report: dict | None = None) -> dict:
    """服务一旦通了，就把真实地址/模型/音色写回 config。

    用户只需要点"一键启动"，不该再去问自己 8891 是什么、模型名该填哪个。
    只在字段为空或与实测不符时覆盖，不抢用户手动填的值。"""
    r = report if report is not None else _tts_local_report(force=True)
    if not r or not r.get("ok"):
        return {"adopted": False, "config": _tts_config_snapshot()}
    changed = {}
    base = (r.get("base") or "").rstrip("/")
    if base and (CFG.get("tts_local_url") or "").rstrip("/") != base:
        CFG["tts_local_url"] = base
        changed["tts_local_url"] = base
    models = [m for m in (r.get("models") or []) if m]
    if not CFG.get("tts_local_model") and models:
        CFG["tts_local_model"] = models[0]
        changed["tts_local_model"] = models[0]
    voices = [v for v in (r.get("voices") or []) if v]
    if not CFG.get("tts_local_voice") and voices:
        CFG["tts_local_voice"] = voices[0]
        changed["tts_local_voice"] = voices[0]
    if changed:
        save_config(CFG)
        log.info("本机 TTS 已接通，自动写回配置：%s", changed)
    return {"adopted": bool(changed), "changed": changed,
            "config": _tts_config_snapshot()}


@app.post("/api/tts/local/start")
def api_tts_local_start():
    result = start_local_tts()
    if result.get("already"):
        # 已经在跑：顺手把地址/模型/音色对齐到实际端口
        result.update(_adopt_local_tts())
    return jsonify(result)


@app.get("/api/tts/local/status")
def api_tts_local_status():
    report = _tts_local_report(force=True)
    proc = _tts_proc.get("popen")
    up = bool(report.get("ok"))
    adopt = _adopt_local_tts(report) if up else {"adopted": False,
                                                 "config": _tts_config_snapshot()}
    return jsonify({"ok": True, "up": up, "probe": report,
                    "starting": bool(proc and proc.poll() is None),
                    "python": _tts_python(), "script": _tts_script().name,
                    "port": _tts_port(), "adopted": adopt.get("adopted", False),
                    "config": adopt.get("config", {}),
                    "log": "" if up else _tts_log_tail()})


@app.get("/api/tts/engines")
def api_tts_engines():
    """本机三套 TTS 的现状：现在用哪个、各自起没起来、缺什么。"""
    cur = (CFG.get("tts_engine") or "cosy").lower()
    rows = []
    for eid in LOCAL_ENGINES:
        p = _engine_profile(eid)
        try:
            probe = TTS.probe(p["url"], "", timeout=0.8)
            up = bool(probe.get("ok"))
        except Exception:                                        # noqa: BLE001
            probe, up = {}, False
        missing = []
        if eid == "moss":
            if not p["python"]:
                missing.append("moss-nano Python 环境")
            if not (BASE / p["script"]).exists():
                missing.append("tts_moss_server.py")
            if not _moss_manifest(p["nano_dir"]):
                missing.append("Nano ONNX 权重（含内置音色清单）")
        elif eid == "qwen3":
            if not p["python"]:
                missing.append("Qwen3-TTS 环境（.tts-venv）")
            if not (BASE / p["script"]).exists():
                missing.append("tts_qwen_server.py")
            if not (Path(p["weights_dir"]) / "model.safetensors").exists():
                missing.append("Qwen3-TTS 1.7B 权重（models/）")
        row = {"id": eid, "label": p["label"], "note": p["note"],
               "url": p["url"], "up": up, "missing": missing,
               "ready": up or not missing,
               "voices": [], "default_voice": p["voice"]}
        if eid == "moss":
            row["voices"] = _moss_voice_catalog(p["nano_dir"])
        elif eid == "qwen3":
            # 服务起来了用真实音色表；没起来时静态表兜底，面板才不会空白
            names = probe.get("voices") or []
            labels = probe.get("voice_labels") or {}
            row["voices"] = ([{"id": n, "name": labels.get(n, n)} for n in names]
                             if names else _qwen_voice_catalog())
        rows.append(row)
    return jsonify({"ok": True, "current": cur,
                    "voice": CFG.get("tts_local_voice") or "", "engines": rows})


@app.post("/api/tts/engines")
def api_tts_engines_switch():
    """一键切换本机引擎：改地址/模型/音色/启动脚本，并按需把新服务拉起来。"""
    body = request.get_json(silent=True) or {}
    eid = (body.get("engine") or body.get("id") or "").strip().lower()
    if eid not in LOCAL_ENGINES:
        return jsonify({"ok": False, "error": f"不认识的本机引擎：{eid}"}), 400
    p = _engine_profile(eid)
    absent = []
    if eid == "moss":
        if not p["python"]:
            absent.append("moss-nano Python 环境（conda create -n moss-nano python=3.12）")
        if not _moss_manifest(p["nano_dir"]):
            absent.append("Nano ONNX 权重（models/ 里的 browser_poc_manifest.json）")
    elif eid == "qwen3":
        if not p["python"]:
            absent.append("Qwen3-TTS 环境（.tts-venv/Scripts/python.exe）")
        if not (BASE / p["script"]).exists():
            absent.append("tts_qwen_server.py")
        if not (Path(p["weights_dir"]) / "model.safetensors").exists():
            absent.append("Qwen3-TTS 1.7B 权重（models/Qwen3-TTS-12Hz-1.7B-CustomVoice/）")
    if absent:
        return jsonify({"ok": False, "error": p["label"] + " 还没装好：" + "、".join(absent),
                        "missing": absent}), 409
    # 三套的音色体系互不相通：切走再切回来时各自记住上次挑的那把
    voice = CFG.get(f"{eid}_voice") or p["voice"]
    CFG.update({
        "tts_engine": eid, "tts_provider": "local",
        "tts_local_url": p["url"], "tts_local_model": p["model"],
        "tts_local_voice": voice,
        # CosyVoice 靠 8891 端口就能自动猜到脚本/解释器；另两套必须显式写，
        # 否则会被猜成 cosyvoice 环境，启动时第一行就 ModuleNotFoundError。
        "tts_local_script": "" if eid == "cosy" else p["script"],
        "tts_local_python": "" if eid == "cosy" else p["python"],
        "tts_local_args": p["args"],
        "tts_local_env": p["env"], "tts_local_format": "wav",
        # Nano 自己按 token 分块；Qwen3-TTS 整句生成慢，切太碎反而更慢
        "tts_local_split": 0 if eid in ("moss", "qwen3") else 30,
    })
    save_config(CFG)
    # 先停掉别的引擎释放显存，再探测/拉起新引擎：三套共存会挤爆 16GB，
    # ONNX Runtime 会报 CUBLAS_STATUS_NOT_INITIALIZED 而 /health 仍显示 ready。
    stopped = _stop_other_tts(_tts_port())
    report = _tts_local_report(force=True)
    started = None
    if not report.get("ok"):
        started = start_local_tts()
    tail = "（正在启动…）" if started else "（已就绪）"
    if stopped:
        tail += f"，已停掉 {len(stopped)} 个旧服务释放显存"
    return jsonify({"ok": True, "engine": eid, "label": p["label"],
                    "probe": report, "started": started, "stopped": stopped,
                    "msg": "切到 " + p["label"] + tail})


@app.post("/api/tts/voice")
def api_tts_voice_set():
    """前端换音色时同步给引擎。

    只写 localStorage 的话，服务端发起的预合成和另一台设备还会用旧音色，
    会出现「试听是说书、整本备成了别的声音」。缓存键本来就带 voice，
    换音色自动另起一份，不会串音。
    """
    body = request.get_json(silent=True) or {}
    voice = str(body.get("voice") or "").strip()
    if not voice or len(voice) > 120:
        return jsonify({"ok": False, "error": "音色名不合法"}), 400
    engine = (CFG.get("tts_engine") or "cosy").lower()
    CFG["tts_local_voice"] = voice
    if engine in LOCAL_ENGINES:
        CFG[f"{engine}_voice"] = voice
    save_config(CFG)
    return jsonify({"ok": True, "voice": voice})


@app.get("/api/tts/providers")
def api_tts_providers():
    """面板要显示"有哪些引擎、现在用哪个、本地那个通不通"。"""
    local = _tts_local_report()
    return jsonify({
        "ok": True,
        "provider": CFG.get("tts_provider") or "edge",
        "providers": [{"id": k, **v} for k, v in TTS.PROVIDERS.items()],
        "local": {
            "url": CFG.get("tts_local_url") or "",
            "model": CFG.get("tts_local_model") or "",
            "voice": CFG.get("tts_local_voice") or "",
            "has_key": bool(CFG.get("tts_local_key")),
            "format": CFG.get("tts_local_format") or "mp3",
            "voice_labels": local.get("voice_labels") or {},
            **{k: local.get(k) for k in ("ok", "error", "models", "voices", "base", "engine")},
        },
    })


@app.post("/api/tts/providers")
def api_tts_providers_set():
    """保存语音引擎选择，并按需探测一次本地服务。"""
    body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "").lower()
    if provider and provider not in TTS.PROVIDERS:
        return jsonify({"ok": False, "error": f"不认识的语音引擎：{provider}"}), 400
    if provider:
        CFG["tts_provider"] = provider
    for key in ("tts_local_url", "tts_local_model", "tts_local_voice",
                "tts_local_key", "tts_local_format"):
        if key in body:
            CFG[key] = body[key]
    save_config(CFG)
    started = None
    if (CFG.get("tts_provider") == "local" or body.get("probe")) and CFG.get("tts_local_url"):
        try:
            TTS.normalize_base(CFG["tts_local_url"])     # 地址写错直接给 400，别让前端猜
        except TTS.TTSConfigError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        report = _tts_local_report(force=True)
        if not report.get("ok"):
            started = _tts_maybe_autostart()   # 没起来就顺手拉起，别让用户去配环境
        if body.get("probe"):
            return jsonify({"ok": True, "probe": report, "autostart": started})
    return jsonify({"ok": True, "autostart": started})


# ---------------------------------------------------------------- 手机访问

def _lan_ip() -> str:
    """猜本机在局域网里的地址。连一下不会真的发包，只是让系统选出口网卡。"""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        sock.close()


def _lan_ips() -> list:
    """本机所有可用的内网 IPv4（多网卡时很常见：WiFi 一个、有线或 VPN 又一个）。
    手机和电脑"同一个 WiFi"未必是默认路由那张网卡，所以扫不通时得能换一个试。"""
    import socket
    found = []

    def add(a: str):
        if (a and a not in found and not a.startswith("127.")
                and not a.startswith("169.254.")          # APIPA，没拿到 DHCP 时的假地址
                and not a.startswith("0.")):
            found.append(a)

    add(_lan_ip())                                        # 默认出口网卡排第一
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    if not found:                                         # 主机名解析不出来就用路由探测兜底
        add(_lan_ip())

    # 排个序：家庭/办公常见网段在前，Docker/WSL 这类虚拟网卡（172.17+）垫底，
    # 免得二维码弹窗里第一个备选就是永远连不上的桥接地址。
    def rank(a: str) -> int:
        if a.startswith("192.168."):
            return 0
        if a.startswith("10."):
            return 1
        if a.startswith("172."):
            return 9
        return 5

    if found:
        first, rest = found[0], sorted(found[1:], key=lambda a: (rank(a), a))
        return [first] + rest
    return found


_ts = {"ip": "", "state": "", "ts": 0.0, "login": ""}


def _tailscale_state() -> dict:
    """Tailscale 的 100.x 地址与状态，给「异地也能听」用。

    state: ready（已登录拿到 IP）/ needs-login（装了没登录）/ absent（没装）/
    offline（服务没起或查询失败）。缓存 60 秒 —— 用户在另一台设备上登录后，
    页面最多一分钟就能自己变过来，不用重启引擎。
    """
    now = time.time()
    if _ts["ts"] and now - _ts["ts"] < 60:
        return {"ip": _ts["ip"], "state": _ts["state"], "login": _ts["login"]}
    exe = ""
    for cand in (r"C:\Program Files\Tailscale\tailscale.exe",
                 r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
        if os.path.exists(cand):
            exe = cand
            break
    if not exe:
        exe = shutil.which("tailscale") or ""
    if not exe:
        _ts.update(ip="", state="absent", ts=now, login="")
        return {"ip": "", "state": "absent", "login": ""}
    ip, state, login = "", "offline", ""
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                             timeout=5, creationflags=NO_WINDOW)
        first = (out.stdout or "").strip().splitlines()
        ip = first[0].strip() if first else ""
        if ip.startswith("100."):
            state = "ready"
        else:
            st = subprocess.run([exe, "status", "--json"], capture_output=True,
                                text=True, timeout=5, creationflags=NO_WINDOW)
            raw = st.stdout or ""
            state = "needs-login" if ("NeedsLogin" in raw) else "offline"
            if state == "needs-login":
                try:
                    login = json.loads(raw).get("AuthURL") or ""
                except Exception:                        # noqa: BLE001
                    login = ""
    except Exception:                                    # noqa: BLE001
        ip, state = "", "offline"
    _ts.update(ip=ip, state=state, ts=now, login=login)
    return {"ip": ip, "state": state, "login": login}


@app.get("/api/lan")
def api_lan():
    host = (CFG.get("host") or "127.0.0.1")
    port = int(CFG["port"])
    ip = _lan_ip()
    open_lan = host not in ("127.0.0.1", "localhost", "::1")
    token = lan_token()
    urls = ([f"http://{ip}:{port}/?k={token}"]
            + [f"http://{a}:{port}/?k={token}" for a in _lan_ips() if a != ip]) \
        if (open_lan and ip) else []
    ts = _tailscale_state()
    # 异地的入口：Tailscale 打通的 100.x 地址。不在同一个 WiFi 时手机用它，
    # 页面/接口完全一样（同一个引擎、同一份书架和进度）。
    remote_urls = ([f"http://{ts['ip']}:{port}/?k={token}"]
                   if (open_lan and ts["ip"]) else [])
    return jsonify({
        "ok": True, "host": host, "port": port, "ip": ip, "open": open_lan,
        "token": token,
        "url": urls[0] if urls else "",
        "urls": urls,                     # 多网卡时给备选，手机不在同一网段可换一个扫
        "remote_urls": remote_urls,       # 异地（Tailscale）
        "tailscale": ts["state"],
        "tailscale_login": ts["login"],   # 没登录时给一条可点的登录链接
    })


@app.post("/api/lan")
def api_lan_set():
    """让手机能连上来：把监听地址从回环改成 0.0.0.0（或改回去）。"""
    body = request.get_json(silent=True) or {}
    want = str(body.get("host") or "").strip()
    if want not in ("127.0.0.1", "0.0.0.0"):
        return jsonify({"ok": False, "error": "host 只能是 127.0.0.1 或 0.0.0.0"}), 400
    CFG["host"] = want
    save_config(CFG)
    ip = _lan_ip()
    token = lan_token()
    return jsonify({"ok": True, "host": want, "token": token,
                    "url": f"http://{ip}:{int(CFG['port'])}/?k={token}"
                           if want == "0.0.0.0" and ip else "",
                    "restart_needed": True})


@app.errorhandler(RequestEntityTooLarge)
def import_too_large(_error):
    return jsonify({"ok": False, "error": "文件不能超过 80 MB。"}), 413


_gpu_clk = {"ts": 0.0, "val": {}}


def _gpu_clocks() -> dict:
    """当前 SM 频率与功耗（MHz / W）。拿不到就返回空，别让测速跟着失败。

    为什么值得单独报：同样的代码，显卡跑到 2600MHz 时一句 8 秒音频要 8.3 秒，
    掉到 500MHz 时要 22 秒 —— 差一倍多。而掉频最常见的诱因根本不是温度，
    是系统进待机/屏幕熄灭（见 _awake_hold）。把频率摆到界面上，用户一眼
    就能分辨"是机器在睡觉"还是"引擎有问题"。
    """
    now = time.time()
    if _gpu_clk["val"] and now - _gpu_clk["ts"] < 5:
        return dict(_gpu_clk["val"])
    info = {}
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        done = subprocess.run(
            ["nvidia-smi", "--query-gpu=clocks.sm,clocks.max.sm,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=flags)
        first = (done.stdout or "").strip().splitlines()
        parts = [p.strip() for p in first[0].split(",")] if first else []
        if len(parts) >= 4:
            info = {"clk": int(float(parts[0])), "clk_max": int(float(parts[1])),
                    "power": round(float(parts[2]), 1),
                    "power_max": round(float(parts[3]), 1)}
    except Exception:                                        # noqa: BLE001
        info = {}
    _gpu_clk.update(ts=now, val=info)
    return dict(info)


def _gpu_watch_start():
    """开一个每秒采样的 nvidia-smi 观察进程。

    为什么不能只在测速结束后查一次：显卡是"干完活才升频"的，结束后那一瞬
    经常正好是 2595MHz 的尖峰，而整个合成过程其实压在 500MHz —— 只看一眼
    会把结论完全带反。所以要边合成边采样，取平均。
    """
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        return subprocess.Popen(
            ["nvidia-smi", "--query-gpu=clocks.sm,power.draw",
             "--format=csv,noheader,nounits", "-l", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=flags)
    except Exception:                                        # noqa: BLE001
        return None


def _gpu_watch_stop(proc, base: dict) -> dict:
    """收掉观察进程，把这段时间的采样平均出来。"""
    info = dict(base or {})
    if proc is None:
        return info
    try:
        proc.terminate()
        out = (proc.communicate(timeout=4)[0] or "")
    except Exception:                                        # noqa: BLE001
        return info
    clks, pws = [], []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                clks.append(float(parts[0]))
                pws.append(float(parts[1]))
            except ValueError:
                continue
    if clks:
        info["clk_avg"] = int(round(sum(clks) / len(clks)))
        info["clk_peak"] = int(max(clks))
        info["power_avg"] = round(sum(pws) / len(pws), 1)
        info["samples"] = len(clks)
    return info


def _media_seconds(path: Path, mime: str) -> float:
    """估一段音频有多长（秒）。测速要拿它当分母，所以别依赖第三方库。

    WAV 读 RIFF 头里的 byte_rate 和 data 块大小；MP3 按 72kbps 反推
    （本机服务固定 72k，偏差很小）；其它容器给 0，让调用方自己降级。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0.0
    if mime == "audio/wav":
        try:
            head = path.read_bytes()[:8192]
        except OSError:
            return 0.0
        if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return 0.0
        byte_rate = struct.unpack_from("<I", head, 28)[0]
        if not byte_rate:
            return 0.0
        pos = 12
        while pos + 8 <= len(head):
            cid = head[pos:pos + 4]
            csz = struct.unpack_from("<I", head, pos + 4)[0]
            if cid == b"data":
                # ffmpeg 往管道写 WAV 时 data 长度常是 0xFFFFFFFF（未知长度）；
                # 拿它当真值会把时长算成 6 小时，测速的 RTF 直接失真。
                if csz in (0, 0xFFFFFFFF):
                    csz = max(0, size - pos - 8)
                return csz / byte_rate
            pos += 8 + csz + (csz & 1)      # 块按偶数字节对齐
        return 0.0
    if mime == "audio/mpeg":
        return size / 9216.0                # 72kbps mp3 ≈ 9KB/s
    return 0.0


@app.get("/api/tts/bench")
def api_tts_bench():
    """合成一句真的，量出「每秒音频要花几秒合成」（RTF）。

    本地大模型的 RTF 直接决定长文能不能顺：>1 就意味着合成永远追不上播放，
    再怎么预取也只是把卡顿推迟一点。这个数字受显卡频率/显存影响很大
    （实测同一台机器能在 0.9～2.6 之间浮动），所以让用户随时能自己量一次。
    """
    provider = (request.args.get("provider") or CFG.get("tts_provider") or "edge").lower()
    if provider not in TTS.PROVIDERS:
        return jsonify({"ok": False, "error": f"不认识的语音引擎：{provider}"}), 400
    # 带时间戳是为了绕开磁盘缓存 —— 命中缓存的话永远是 0 秒，量不出真东西
    text = "测一句速度，看看这台机器合成一秒语音到底要花多久。" + f"（{int(time.time())}）"
    voice = ((CFG.get("tts_local_voice") or CFG["default_voice"])
             if provider == "local" else CFG["default_voice"])
    base_gpu = _gpu_clocks() if provider == "local" else {}
    watch = _gpu_watch_start() if provider == "local" else None
    started = time.time()
    jid = _job_open("bench", "（测速）" + text[:24], **_client_of())
    with _synth_gate:
        _fg_busy["n"] += 1
        _play_touch["ts"] = time.time()
        _synth_gate.notify_all()
    try:
        path, mime = synth(text, voice, CFG["rate"], CFG["pitch"], CFG["volume"],
                           provider, CFG.get("tts_local_model") or "")
    except Exception as e:                                   # noqa: BLE001
        _gpu_watch_stop(watch, base_gpu)
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502
    finally:
        _job_close(jid)
        with _synth_gate:
            _fg_busy["n"] = max(0, _fg_busy["n"] - 1)
            _play_touch["ts"] = time.time()
            _synth_gate.notify_all()
        _awake_touch()      # 还在合成 = 人还在听，别让保活线程以为人走了
    took = time.time() - started
    audio = _media_seconds(path, mime)
    rtf = round(took / audio, 2) if audio > 0 else 0.0
    if not audio:
        verdict = "未知"
    elif rtf < 0.8:
        verdict = "很快"
    elif rtf <= 1.0:
        verdict = "跟得上"
    elif rtf < 1.4:
        verdict = "偏慢"
    else:
        verdict = "跟不上"
    gpu = _gpu_watch_stop(watch, base_gpu)
    tip = ""
    clk, cap = gpu.get("clk_avg"), gpu.get("clk_max")
    if clk and cap and rtf >= 1.3:
        if clk < cap * 0.6:
            tip = (f"合成期间显卡平均只有 {clk}MHz（上限 {cap}MHz，峰 {gpu.get('clk_peak')}MHz），"
                   "系统多半在待机/熄屏。播放时会自动按住电源请求，但已经睡下去就拉不回来——"
                   "亮屏再测一次会快一倍多；想彻底解决跑一次 lock-gpu-clock.bat（管理员）。")
        else:
            tip = (f"显卡是满速跑着的（平均 {clk}MHz），慢在模型本身——"
                   "把朗读语速调到 +10% 就能让合成追上播放。")
    return jsonify({"ok": True, "provider": provider, "voice": voice,
                    "seconds": round(took, 2), "audio": round(audio, 2),
                    "rtf": rtf, "verdict": verdict, "gpu": gpu, "tip": tip})


@app.route("/api/tts", methods=["GET", "POST"])
def api_tts():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
    else:
        body = request.args
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text 为空"}), 400
    if not NLP.speakable(text):
        # 纯标点（第 118 句只有一个 "."）：没音可发。这里直接给一段静音，
        # 而不是让合成器回 502 —— 浏览器拿到非音频响应会一直挂着不前进。
        return Response(_silent_wav(), mimetype="audio/wav",
                        headers={"Cache-Control": "public, max-age=604800"})
    voice = body.get("voice") or CFG["default_voice"]
    rate = body.get("rate") or CFG["rate"]
    pitch = body.get("pitch") or CFG["pitch"]
    volume = body.get("volume") or CFG["volume"]
    provider = (body.get("provider") or CFG.get("tts_provider") or "edge").lower()
    model = body.get("model") or ""
    if provider not in TTS.PROVIDERS:
        return jsonify({"ok": False, "error": f"不认识的语音引擎：{provider}"}), 400
    # 上闸门：前台请求期间预合成不许动手，否则这一句要排在它后面等一整句
    fmt = _audio_format_for_client()
    jid = _job_open("fg", text, fmt=fmt, **_client_of())
    with _synth_gate:
        _fg_busy["n"] += 1
        _play_touch["ts"] = time.time()
        _synth_gate.notify_all()
    try:
        path, mime = synth(text, voice, rate, pitch, volume, provider, model, fmt)
    except TTS.TTSConfigError as e:
        # 本地 TTS 没起来：顺手拉起一个，并把"正在启动"这个事实告诉前端，
        # 而不是甩一句"连不上 127.0.0.1:8891"让用户自己去配环境。
        started = _tts_maybe_autostart() if provider == "local" else None
        msg = str(e)
        if started and started.get("ok"):
            msg = "本机 TTS 刚刚启动，模型加载约 10～60 秒；稍等一下会自动重试。"
        return jsonify({"ok": False, "error": msg, "provider": provider,
                        "autostart": started}), 502
    except Exception as e:
        log.error("语音合成失败: %s", e)
        return jsonify({"ok": False, "error": f"语音合成失败：{e}"}), 502
    finally:
        _job_close(jid)
        with _synth_gate:
            _fg_busy["n"] = max(0, _fg_busy["n"] - 1)
            _play_touch["ts"] = time.time()
            _synth_gate.notify_all()
        _awake_touch()      # 还在合成 = 人还在听，别让保活线程以为人走了
    resp = send_from_directory(TTS_CACHE, path.name, mimetype=mime)
    if body.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="readaloud.{path.suffix[1:]}"'
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
    # 监听地址同理：想用手机连，就把 host 设成 0.0.0.0（面板上有开关）
    bind = os.environ.get("READALOUD_HOST") or CFG.get("host") or "127.0.0.1"
    # 自我重启时先等一下：旧进程还没释放 socket，抢着绑就是 EADDRINUSE
    delay = float(os.environ.get("READALOUD_RESTART_DELAY") or 0)
    if delay > 0:
        log.info("重启中：等 %.1f 秒再绑端口", delay)
        time.sleep(delay)
    vs = list_voices()
    log.info("ReadAloud 引擎 v%s", ENGINE_VERSION)
    log.info("音色可用 %d 个；Ollama 模型 %d 个", len(vs), len(ollama_models()))
    log.info("语音引擎 %s；书架 %d 本", CFG.get("tts_provider"), SHELF.stats()["count"])
    log.info("本机 %s | 已监听 http://%s:%s/", platform.node(), bind, port)
    if bind != "127.0.0.1":
        log.info("局域网可见：手机打开 http://%s:%s/ 即可", _lan_ip() or "<本机IP>", port)

    # 兼容端口：老书签（默认 8756）也指向这同一个引擎。
    # 以前这些端口上躺着几周前启动的旧副本，页面是新的、接口是老的，
    # 用户看到的就是"导入点了没反应" —— 现在抢回来自己听。
    # 先清掉上一次热重启遗留的同进程监听，否则这里会 EADDRINUSE 然后一直是旧版。
    _reclaim_extra_ports()
    for extra in (CFG.get("extra_ports") or []):
        try:
            extra_port = int(extra)
        except (TypeError, ValueError):
            continue
        if extra_port == port:
            continue

        def _serve_extra(p=extra_port):
            try:
                from werkzeug.serving import make_server
                srv = make_server(bind, p, app, threaded=True)
                _extra_srvs.append(srv)
                log.info("兼容端口已监听 http://%s:%s/", bind, p)
                srv.serve_forever()
            except Exception as e:
                log.warning("兼容端口 %s 没绑上（被别的程序占着？）: %s", p, e)

        threading.Thread(target=_serve_extra, daemon=True).start()

    threading.Thread(target=_auto_update_worker, daemon=True).start()

    # 用 make_server 而不是 app.run：自我重启时子进程要能重试绑定端口。
    # 旧进程退出后 socket 释放有延迟（Windows 上尤其明显），一次失败就退出
    # 的表现就是"点了重启，引擎没了"。
    from werkzeug.serving import make_server
    srv = None
    for attempt in range(1, 21):
        try:
            srv = make_server(bind, port, app, threaded=True)
            break
        except OSError as e:
            log.warning("绑定 %s:%s 失败（第 %d 次，2 秒后重试）: %s", bind, port, attempt, e)
            time.sleep(2)
    if srv is None:
        log.error("端口 %s 一直绑不上，退出", port)
        sys.exit(1)
    log.info("已开始服务 http://%s:%s/（pid %s）", bind, port, os.getpid())
    _srv_ref.update(srv=srv, bind=bind, port=port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
        sys.exit(0)
    # serve_forever 返回 = 有人在热重启（新副本会在线程里接手）。
    # 主线程这时候绝不能退出，否则整个进程跟着结束，热重启就成了自杀。
    while True:
        time.sleep(3600)
