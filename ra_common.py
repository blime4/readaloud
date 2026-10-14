# -*- coding: utf-8 -*-
"""ReadAloud 公共工具：剪贴板/选区读取、网页抓取、与本地服务通信。"""

import ctypes
import json
import re
import sys
import threading
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


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://\S+$", (s or "").strip(), re.I))


def _foreground_is_console() -> bool:
    """前台是不是终端窗口。在终端里盲发 Ctrl+C 会打断正在跑的程序，必须先判断。"""
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, buf, 256)
        return buf.value in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
                             "mintty", "WindowsTerminal")
    except Exception:
        return False


def copy_selection(wait: float = 0.28) -> str:
    """读"当前选中的文字"：先记下剪贴板，发一次 Ctrl+C，看剪贴板有没有变。

    没选中任何东西时剪贴板不变，就退回原来的剪贴板内容 —— 所以这个热键
    在"有选中"和"没选中"两种情况下都不会出错。
    """
    if _foreground_is_console():
        return read_clipboard()          # 终端里不敢乱发 Ctrl+C，直接用剪贴板
    before = read_clipboard()
    try:
        u32 = ctypes.windll.user32
        VK_CONTROL, VK_C, KEYUP = 0x11, 0x43, 0x0002
        u32.keybd_event(VK_CONTROL, 0, 0, 0)
        u32.keybd_event(VK_C, 0, 0, 0)
        u32.keybd_event(VK_C, 0, KEYUP, 0)
        u32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
        time.sleep(wait)
    except Exception as e:
        log(f"发送 Ctrl+C 失败: {e}")
    after = read_clipboard()
    return after if after != before else (after or before)


def fetch_url(url: str, timeout: float = 25.0):
    """让引擎把网页正文抓下来（抓到就能直接读）。"""
    try:
        req = urllib.request.Request(
            SERVER + "/api/fetch?" + urllib.parse.urlencode({"url": url}))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"抓取网页失败: {e}")
        return {"ok": False, "error": str(e)}


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


def broadcast(text: str, ai: str = "none", auto: bool = True, hint: str = "") -> int:
    """推送给已打开的播放页面，返回接收端数量。hint 会作为提示显示在页面上。"""
    try:
        return int(post("/api/broadcast", {"text": text, "ai": ai, "auto": auto,
                                           "hint": hint}).get("delivered", 0))
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


LONG_HINT_CHARS = 1500          # 超过这么多字就提醒一下"可以听总结版"


def speak(text: str, ai: str = "none", hint: str = "") -> str:
    """核心动作：把文本送去朗读。返回一句人类可读的结果。

    会比以前多做三件"聪明事"：
      1. 内容是个链接 -> 先抓正文再读（不用手动复制全文）；
      2. 内容很长而你没选总结 -> 提示一句"可以按 Ctrl+Alt+S 听总结版"；
      3. 服务没起来 / 出错 -> 弹一个会自动消失的提示（以前是静默的，按了没反应最气人）。
    """
    text = (text or "").strip()
    if not text:
        return "剪贴板里没有文字"

    if is_url(text):
        d = fetch_url(text)
        if d.get("ok"):
            title = (d.get("title") or "").strip()
            text = d["text"]
            hint = hint or f"已抓取网页正文（{d['chars']} 字）"
            log(f"链接已抓取: {title[:40]} -> {d['chars']} 字")
        else:
            msg = f"这个链接没能抓到正文：{d.get('error', '未知原因')}"
            notify("ReadAloud", msg)
            return msg

    if ai == "none" and not hint and len(text) >= LONG_HINT_CHARS:
        hint = f"内容较长（{len(text)} 字），按 Ctrl+Alt+S 可以听总结版"

    if not health():
        msg = "朗读服务没在运行。双击 start-server.bat 启动，或先在浏览器里点「一键启动」。"
        notify("ReadAloud", msg)
        return msg

    n = broadcast(text, ai, hint=hint)
    if n > 0:
        tag = {"summarize": "（总结后）", "polish": "（润色后）"}.get(ai, "")
        return f"已发给 {n} 个播放端朗读{tag}"
    if n == 0 and hint:
        open_player(text, ai)
        return f"已打开播放页面并朗读（{hint}）"
    open_player(text, ai)
    tag = {"summarize": "总结后", "polish": "润色后"}.get(ai, "")
    return f"已打开播放页面并朗读{tag}"


def notify(title: str, msg: str, seconds: float = 4.0) -> None:
    """弹一个几秒后自动消失的提示框。

    只在"出错了"的时候用 —— 按了热键却毫无反应是最让人困惑的。
    优先用 MessageBoxTimeoutW（系统自带、不会卡住热键线程）；
    万一这个未公开接口不可用，退回 MessageBoxW + 后台线程关窗。
    """
    u32 = ctypes.windll.user32
    MB_ICONWARNING, MB_TOPMOST, MB_SETFOREGROUND = 0x30, 0x40000, 0x10000
    flags = MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
    try:
        u32.MessageBoxTimeoutW(0, msg, title, flags, 0, int(max(1.0, seconds) * 1000))
        return
    except Exception:
        pass
    try:
        def closer():
            time.sleep(seconds)
            try:
                hwnd = u32.FindWindowW(None, title)
                if hwnd:
                    u32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE
            except Exception:
                pass
        threading.Thread(target=closer, daemon=True).start()
        u32.MessageBoxW(0, msg, title, flags)
    except Exception as e:
        log(f"提示框失败: {e}")
