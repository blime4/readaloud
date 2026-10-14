# -*- coding: utf-8 -*-
"""ReadAloud 公共工具：剪贴板读取、与本地服务通信。"""

import ctypes
import json
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

SERVER = "http://127.0.0.1:8756"
BASE = Path(__file__).resolve().parent
LOG = BASE / "hotkey.log"


def log(msg: str) -> None:
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_clipboard() -> str:
    """读剪贴板文本（Win32 原生优先，无第三方依赖）。"""
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        # 64 位下句柄必须按指针处理，否则会被截断
        u32.GetClipboardData.restype = ctypes.c_void_p
        u32.GetClipboardData.argtypes = [ctypes.c_uint]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        CF_UNICODETEXT = 13
        text = ""
        for _ in range(6):                       # 剪贴板可能被别的程序短暂占用，重试几次
            if u32.OpenClipboard(None):
                try:
                    h = u32.GetClipboardData(CF_UNICODETEXT)
                    if h:
                        p = k32.GlobalLock(h)
                        if p:
                            try:
                                text = ctypes.wstring_at(p)
                            finally:
                                k32.GlobalUnlock(h)
                finally:
                    u32.CloseClipboard()
                if text:
                    return text
            time.sleep(0.08)
        if text:
            return text
    except Exception as e:
        log(f"Win32 读剪贴板失败: {e}")
    try:                                          # 备用：tkinter（若环境带 tcl/tk）
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        try:
            return r.clipboard_get()
        finally:
            r.destroy()
    except Exception:
        return ""


def post(path: str, payload: dict, timeout: float = 8.0):
    req = urllib.request.Request(
        SERVER + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def health(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(SERVER + "/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def broadcast(text: str, ai: str = "none", auto: bool = True) -> int:
    """推送给已打开的播放页面，返回接收端数量。"""
    try:
        return int(post("/api/broadcast", {"text": text, "ai": ai, "auto": auto}).get("delivered", 0))
    except Exception as e:
        log(f"broadcast 失败: {e}")
        return -1


def open_player(text: str = "", ai: str = "none", auto: bool = True) -> None:
    """打开网页播放器；文本很长时改用广播方式补发。"""
    if not text:
        webbrowser.open(SERVER + "/")
        return
    url = SERVER + "/?" + urllib.parse.urlencode({"text": text, "ai": ai, "auto": "1" if auto else "0"})
    if len(url) > 6000:                      # 超长文本走广播补发
        webbrowser.open(SERVER + "/")
        for _ in range(8):
            time.sleep(1.0)
            if broadcast(text, ai, auto) > 0:
                return
        return
    webbrowser.open(url)


def speak(text: str, ai: str = "none") -> str:
    """核心动作：把文本送去朗读。返回一句人类可读的结果。"""
    text = (text or "").strip()
    if not text:
        return "剪贴板里没有文字"
    if not health():
        return "朗读服务没在运行，请先双击 start-server.bat"
    n = broadcast(text, ai)
    if n > 0:
        tag = {"summarize": "（总结后）", "polish": "（润色后）"}.get(ai, "")
        return f"已发给 {n} 个播放端朗读{tag}"
    open_player(text, ai)
    tag = {"summarize": "总结后", "polish": "润色后"}.get(ai, "")
    return f"已打开播放页面并朗读{tag}"


def notify(title: str, msg: str) -> None:
    """右下角气泡提示（不影响使用，失败就忽略）。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:
        pass
