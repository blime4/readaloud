"""语音合成引擎：默认走微软神经语音，也可以指向本机的 OpenAI 兼容 TTS。

为什么要有这一层
----------------
edge-tts 是默认选择：免费、稳定、几十个中文音色、单句一百毫秒级，
朗读长文时它是唯一"跟得上翻页"的方案。

但它终究是云端音色库里的通用声音。想听更自然、更像真人（还能克隆自己
声音）的，就得让本机的模型来合成 —— CosyVoice2、Kokoro、GPT-SoVITS、
IndexTTS 这些都已经提供 OpenAI 兼容的 `/v1/audio/speech`，
一台机器上跑着，文字一样不出本机。

注意：Ollama 本身不产出音频，它只有文本/视觉/向量能力，
所以"用 Ollama 做 TTS"这条路走不通 —— 但那台机器上再起一个本地 TTS
服务是完全一样的思路，这里就是为它准备的接口。
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import requests

# 本机 TTS 就在同一台机器或局域网里，任何系统代理都不该掺和进来 ——
# 实测 Clash 会把回环请求拦成 502（upstream connect failed），明明服务活着却"连不上"。
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.proxies = {"http": None, "https": None}

PROVIDERS = {
    "edge": {
        "label": "微软神经语音",
        "note": "默认。免费、快、可缓存，几十个中文音色，长文朗读首选。",
        "needs_url": False,
    },
    "local": {
        "label": "本机大模型 TTS",
        "note": "指向本机任意 OpenAI 兼容 TTS（Qwen3-TTS / CosyVoice / Kokoro / GPT-SoVITS）。文字不出本机。",
        "needs_url": True,
    },
}

# OpenAI 的 response_format 取值；本地服务不一定都支持，默认 mp3
FORMAT_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/ogg",
    "flac": "audio/flac",
    "aac": "audio/aac",
}


class TTSConfigError(ValueError):
    """本地 TTS 配置有问题（给用户看的中文原因）。"""


def normalize_base(url: str) -> str:
    """把用户填的地址规整成"根"：去掉末尾 /，去掉 /v1 后缀。"""
    raw = (url or "").strip()
    if not raw:
        raise TTSConfigError("先填本地 TTS 的地址，例如 http://127.0.0.1:8880")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise TTSConfigError("地址要以 http:// 或 https:// 开头。")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    if path.endswith("/audio/speech"):
        path = path[: -len("/audio/speech")]
        if path.endswith("/v1"):
            path = path[: -len("/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def probe(url: str, key: str = "", timeout: float = 3.0) -> dict:
    """探测一个本地 TTS 服务：有没有、有哪些模型、有哪些音色。

    只做只读 GET，不合成、不占显存。三路探测并行发 —— 地址根本没服务时，
    每一路都要吃满超时，串行就是三倍等待。探测失败也要带着原因返回，
    因为面板上要显示"为什么连不上"，而不是让用户猜。
    """
    base = normalize_base(url)                     # 地址不合法直接抛，调用方转 400
    report = {"ok": False, "base": base, "models": [], "voices": [],
              "engine": "", "error": ""}

    def _get(path: str):
        try:
            return SESSION.get(f"{base}{path}", headers=_headers(key), timeout=timeout)
        except Exception as error:
            return error

    health_res, models_res = None, None
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {p: pool.submit(_get, p)
                   for p in ("/health", "/v1/models", "/v1/audio/voices")}
        health_res = futures["/health"].result()
        models_res = futures["/v1/models"].result()
        voices_res = futures["/v1/audio/voices"].result()

    if isinstance(health_res, Exception):
        report["error"] = f"{type(health_res).__name__}: {health_res}"
    elif health_res.ok:
        report["ok"] = True
        try:
            data = health_res.json()
            report["engine"] = str(data.get("engine") or data.get("service") or "")
        except ValueError:
            pass

    if isinstance(models_res, requests.Response) and models_res.ok:
        try:
            payload = models_res.json()
            rows = payload.get("data") if isinstance(payload, dict) else payload
            report["models"] = [str(m.get("id") or m.get("name") or m)
                                for m in (rows or []) if m][:40]
            report["ok"] = True
        except ValueError:
            pass

    for res in (voices_res,):
        if not isinstance(res, requests.Response) or not res.ok:
            continue
        try:
            payload = res.json()
        except ValueError:
            continue
        rows = payload.get("voices") if isinstance(payload, dict) else payload
        names = []
        for row in rows or []:
            if isinstance(row, str):
                names.append(row)
            elif isinstance(row, dict):
                names.append(str(row.get("id") or row.get("name") or row.get("voice") or ""))
        report["voices"] = [n for n in names if n][:200]
        report["ok"] = True
        break

    if not report["ok"] and not report["error"]:
        report["error"] = "服务有响应，但不像 OpenAI 兼容的 TTS（没有 /v1/models 也没有音色表）。"
    return report


def speed_from_rate(rate: str) -> float:
    """把引擎里的 "+8%" 语速换成 OpenAI speech 的 speed 倍率。"""
    match = re.search(r"([+-]?\d+(?:\.\d+)?)", str(rate or ""))
    pct = float(match.group(1)) if match else 0.0
    return round(max(0.25, min(4.0, 1.0 + pct / 100.0)), 3)


def synthesize_local(text: str, voice: str, model: str, url: str, key: str = "",
                     rate: str = "+0%", fmt: str = "mp3",
                     timeout: float = 120.0) -> tuple:
    """调本地 /v1/audio/speech，返回 (字节, mimetype)。"""
    base = normalize_base(url)
    fmt = fmt if fmt in FORMAT_MIME else "mp3"
    body = {
        "model": model or "tts-1",
        "input": text,
        "voice": voice or "alloy",
        "response_format": fmt,
        "speed": speed_from_rate(rate),
    }
    try:
        response = SESSION.post(f"{base}/v1/audio/speech", json=body,
                                headers=_headers(key), timeout=timeout)
    except requests.RequestException as error:
        raise TTSConfigError(f"连不上本地 TTS（{base}）：{error}") from error
    if not response.ok:
        detail = (response.text or "").strip()[:200]
        raise TTSConfigError(f"本地 TTS 回了 HTTP {response.status_code}：{detail}")
    if not response.content:
        raise TTSConfigError("本地 TTS 返回了空音频。")
    mimetype = response.headers.get("Content-Type", "").split(";")[0].strip()
    if not mimetype.startswith("audio/"):
        # 有些服务把 base64 塞进 JSON，这里明确报错，别把坏数据写进缓存
        raise TTSConfigError(f"本地 TTS 返回的不是音频（{mimetype or '未知类型'}）。")
    return response.content, mimetype
