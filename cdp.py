# -*- coding: utf-8 -*-
"""借用本机浏览器的登录态：一个零依赖的最小 CDP 客户端。

## 为什么需要它

知乎这类站点的 403 不是"UA 不对"，而是**没有登录态**：

    无 cookie           → HTTP 403（返回一个 zse 挑战页）
    带浏览器 cookie     → HTTP 200（风控过了；但缺 z_c0 仍是软 404）

也就是说，只要把你日常浏览器里已经登录的那套 cookie 拿过来，就能正常抓。

## 为什么不能直接读浏览器的 cookie 数据库

Chrome 127 起启用了 App-Bound Encryption（应用绑定加密）。
本机实测 Chrome 155 的 `Local State` 里 `os_crypt` **只剩
`app_bound_encrypted_key`，连老的 `encrypted_key` 都没有了**——
解密要先走 COM 提权接口拿主密钥，普通用户态进程做不到。
所以只剩两条路：**CDP 实时取**，或者**手工粘贴**。

## 这个模块做什么

- `pick_browser()`   扫本机常见调试端口，找出开着的那个
- `browser_info()`   拿浏览器版本和**真实 UA**
- `cookies(port)`    取全部 cookie（含 HttpOnly 的 z_c0）
- `fetch_text()`     让浏览器自己去访问页面并取回正文（最彻底的兜底）

## 两个容易踩的坑

1. **握手不能带 `Origin` 头**。Chrome DevTools 会直接拒绝升级请求
   （除非启动时显式给了 `--remote-allow-origins`）。很多教程里的
   websocket 库默认带 Origin，会连不上。
2. **换 UA 会让 `__zse_ck` 失效**。这个 cookie 与 UA/IP 绑定，
   所以取 cookie 时必须把浏览器的真实 UA 一起记下来，抓取时配套使用。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from typing import Optional

# Chrome 136+ 不允许在**默认** user-data-dir 上开调试端口（安全策略），
# 所以想让日常浏览器被接管，必须用独立 profile 启动（见 start-browser-login.bat）。
DEFAULT_PORTS = (9222, 9223, 9224, 9225, 9229, 9333)


class CDPError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# WebSocket（最小实现，只够 CDP 用）
# --------------------------------------------------------------------------

class WS:
    """够用的 WebSocket 客户端：文本帧、分片、ping/pong。

    CDP 的消息都是小 JSON，不需要压缩扩展、也不需要子协议协商。
    """

    def __init__(self, url: str, timeout: float = 10.0):
        if not url.startswith("ws://"):
            raise CDPError(f"只支持 ws:// 地址：{url[:60]}")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        self.url = url
        self.sock = socket.create_connection((host, int(port or 80)), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self._id = 0

        key = base64.b64encode(os.urandom(16)).decode()
        # 注意：故意不写 Origin 头 —— 写了 Chrome 会拒绝握手。
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {hostport}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n"
               "\r\n")
        self.sock.sendall(req.encode("ascii"))

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("握手过程中连接被关闭")
            head += chunk
        header, _, tail = head.partition(b"\r\n\r\n")
        status = header.split(b"\r\n")[0].decode("latin-1")
        if "101" not in status:
            raise CDPError(f"WebSocket 握手失败：{status}")
        self.buf = tail

    # ---- 底层收发 ----

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise CDPError("连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(head) + masked

    def send(self, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.sock.sendall(self._frame(0x1, data))

    def recv(self) -> dict:
        payload = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, ln = b2 & 0x80, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            data = self._read(ln) if ln else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise CDPError("浏览器关闭了调试连接")
            if opcode == 0x9:
                self.sock.sendall(self._frame(0xA, data))
                continue
            if opcode == 0xA:
                continue
            payload += data
            if fin:
                if not payload:
                    continue
                try:
                    return json.loads(payload.decode("utf-8"))
                except Exception:
                    continue

    # ---- 会话 ----

    def send_raw(self, msg: dict) -> int:
        """发一条命令，返回它的 id，调用方自己按 id 收响应。"""
        self._id += 1
        msg = dict(msg)
        msg["id"] = self._id
        self.send(msg)
        return self._id

    def wait_for(self, mid: int, timeout: float = 15.0, drain_events: int = 0):
        """等指定 id 的响应，路上收到的事件先丢掉（或交给回调）。"""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise CDPError(f"等待 id={mid} 的响应超时")
            self.sock.settimeout(left)
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"CDP 报错：{msg['error']}")
                return msg.get("result") or {}

    def call(self, method: str, params: Optional[dict] = None,
             timeout: float = 15.0, session_id: str = "") -> dict:
        msg = {"method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        return self.wait_for(self.send_raw(msg), timeout)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# --------------------------------------------------------------------------
# 探测与取数
# --------------------------------------------------------------------------

# 端口探活的短缓存：同一个端口 1.5 秒内不重复探。
# 面板打开时要扫 6 个端口，缓存能让"点刷新"这种连续动作立刻返回。
_PORT_CACHE: dict[int, tuple[float, bool]] = {}


def port_open(port: int, timeout: float = 0.12, cache: float = 8.0) -> bool:
    """这个回环端口上有没有在听。

    **必须自己先探一次 TCP，不能直接 urlopen。** 本机（Windows + Clash）
    对**未监听**的回环端口不做立即拒绝，connect 要挂满约 2 秒才失败 ——
    扫 5 个空端口就是 10 秒。实测 `/api/login` 因此要 10.6 秒才返回，
    面板打开像卡死一样。自己探一次带超时的 TCP，最多 0.12 秒
    （真实在听的端口 connect 是微秒级，这个超时绰绰有余）。
    """
    port = int(port)
    now = time.time()
    hit = _PORT_CACHE.get(port)
    if hit and now - hit[0] < cache:
        return hit[1]
    ok = False
    s = socket.socket()
    s.settimeout(timeout)
    try:
        ok = s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        ok = False
    finally:
        s.close()
    _PORT_CACHE[port] = (now, ok)
    return ok


def http_json(port: int, path: str = "/json/version", timeout: float = 3.0):
    """CDP 的 HTTP 端点。回环地址必须直连，不能走系统代理。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def browser_info(port: int, timeout: float = 1.5) -> Optional[dict]:
    """这个端口上是什么浏览器。不是 Chrome 系或端口没开就返回 None。"""
    if not port_open(port):
        return None
    try:
        v = http_json(port, "/json/version", timeout)
    except Exception:
        return None
    if not v.get("webSocketDebuggerUrl"):
        return None
    ua = v.get("User-Agent") or ""
    # 无头浏览器会自报 HeadlessChrome，知乎一眼认出来。抹掉这个标记。
    ua = ua.replace("HeadlessChrome", "Chrome")
    v["User-Agent"] = ua
    v["port"] = port
    v["headless"] = "Headless" in (v.get("Browser") or "")
    return v


def pick_browser(ports=None, timeout: float = 0.2) -> Optional[dict]:
    """扫一遍常见端口，返回第一个能用的调试浏览器。"""
    for p in (ports or DEFAULT_PORTS):
        if not port_open(int(p), timeout):
            continue
        info = browser_info(int(p))
        if info:
            return info
    return None


def cookies(port: int, ws_url: str = "", timeout: float = 10.0) -> list[dict]:
    """取浏览器里全部 cookie（含 HttpOnly）。

    `Storage.getCookies` 是浏览器级域，不需要 attach 到具体页面 ——
    比 `Network.getCookies` 省事，也不受"当前开没开这个站"影响。
    """
    info = browser_info(port)
    url = ws_url or (info or {}).get("webSocketDebuggerUrl")
    if not url:
        raise CDPError(f"127.0.0.1:{port} 上没有可用的调试接口")
    with WS(url, timeout) as ws:
        res = ws.call("Storage.getCookies", timeout=timeout)
    return res.get("cookies") or []


def pages(port: int, ws_url: str = "", timeout: float = 6.0) -> list[dict]:
    info = browser_info(port)
    url = ws_url or (info or {}).get("webSocketDebuggerUrl")
    if not url:
        raise CDPError(f"127.0.0.1:{port} 上没有可用的调试接口")
    with WS(url, timeout) as ws:
        res = ws.call("Target.getTargets", timeout=timeout)
    return [t for t in (res.get("targetInfos") or []) if t.get("type") == "page"]


def warmup(port: int, urls, wait: float = 5.0, ws_url: str = "",
           timeout: float = 30.0, keep_open: bool = False) -> dict:
    """让浏览器真的去访问这些地址，把风控 cookie 刷新到最新。

    这一步不是可有可无的。实测：直接把浏览器里"十分钟前"的 cookie 搬过来，
    抓知乎照样 403；让浏览器重新访问一次首页再取，立刻变 200。
    因为 `__zse_ck` 这类风控 cookie 寿命极短，不活动就失效 ——
    拿一套凉掉的 cookie 去请求，等于没配。

    用一个标签串行导航，不弹一堆窗口。
    """
    info = browser_info(port)
    root = ws_url or (info or {}).get("webSocketDebuggerUrl")
    if not root:
        raise CDPError(f"127.0.0.1:{port} 上没有可用的调试接口")
    if isinstance(urls, str):
        urls = [urls]

    done, failed = [], []
    with WS(root, timeout) as ws:
        tid = ws.call("Target.createTarget", {"url": "about:blank"}, timeout)["targetId"]
        sid = ws.call("Target.attachToTarget",
                      {"targetId": tid, "flatten": True}, timeout)["sessionId"]
        try:
            ws.call("Page.enable", session_id=sid, timeout=timeout)
            for u in urls:
                try:
                    ws.call("Page.navigate", {"url": u}, session_id=sid, timeout=timeout)
                except CDPError as e:
                    failed.append({"url": u, "error": str(e)[:100]})
                    continue
                t0 = time.time()
                while time.time() - t0 < wait:
                    time.sleep(0.4)
                    try:
                        st = ws.call("Runtime.evaluate", {
                            "expression": "document.readyState",
                            "returnByValue": True}, session_id=sid,
                            timeout=timeout).get("result", {}).get("value")
                    except CDPError:
                        break
                    if st == "complete" and time.time() - t0 > 1.2:
                        break
                done.append(u)
        finally:
            if not keep_open:
                try:
                    ws.call("Target.closeTarget", {"targetId": tid}, timeout=6)
                except Exception:
                    pass
    return {"ok": bool(done), "done": done, "failed": failed,
            "port": port, "tab_id": None if not keep_open else tid}


# 让浏览器自己去取正文时，不同站点正文所在的容器（按优先级）
BODY_SELECTORS = (
    ".RichText",                    # 知乎文章 / 回答
    ".Post-RichTextContainer",      # 知乎旧版
    "#js_content",                  # 微信公众号
    ".article-content", ".post-content", ".entry-content",
    "[itemprop='articleBody']", "article", "main", "#content",
)

# 判"是不是人机校验页"的 JS（浏览器已经执行过 JS 了，这里只是复核）
OBSTACLE_JS = (
    "/安全检测|环境异常|请开启JavaScript|Verify you are human"
    "|Just a moment|滑动验证|访问受限|请求存在异常/i"
    ".test(document.body.innerText||'')"
)


def fetch_text(port: int, url: str, ws_url: str = "",
               wait: float = 12.0, min_chars: int = 300,
               poll: float = 0.5, timeout: float = 40.0) -> dict:
    """让浏览器打开这个页面，把渲染后的正文取回来。

    这是最彻底的一招：浏览器带着登录态、真 UA，自己过 JS 挑战，
    我们只需要从 DOM 里把内容捞出来。

    返回里同时给 `html` 和 `text`：
    - `html` 是正文容器的 outerHTML，交给 webfetch 走**同一条清理管线**，
      这样浏览器代抓回来的稿子照样有"标题慢读、条目紧凑"的语气分层；
    - `text` 是 innerText，作为没找到容器时的兜底。
    """
    info = browser_info(port)
    root = ws_url or (info or {}).get("webSocketDebuggerUrl")
    if not root:
        raise CDPError(f"127.0.0.1:{port} 上没有可用的调试接口")

    sel = json.dumps(list(BODY_SELECTORS))
    # 在浏览器里就把容器挑好，并把 HTML 一起带回来 —— 光拿 innerText
    # 会丢掉 h1/li/blockquote，读出来是平的。
    read_js = (
        "(() => {"
        f"  const sels = {sel};"
        "  for (const s of sels) {"
        "    const el = document.querySelector(s);"
        "    if (el && el.innerText && el.innerText.trim().length > 120)"
        "      return {html: el.outerHTML, text: el.innerText, found: s};"
        "  }"
        "  const b = document.body;"
        "  return {html: b ? b.innerHTML : '',"
        "          text: b ? b.innerText : '', found: ''};"
        "})()"
    )

    with WS(root, timeout) as ws:
        tid = ws.call("Target.createTarget", {"url": "about:blank"}, timeout)["targetId"]
        sid = ws.call("Target.attachToTarget",
                      {"targetId": tid, "flatten": True}, timeout)["sessionId"]
        try:
            ws.call("Page.enable", session_id=sid, timeout=timeout)
            ws.call("Page.navigate", {"url": url}, session_id=sid, timeout=timeout)
            t0 = time.time()
            got: dict = {}
            title, blocked = "", False
            while time.time() - t0 < wait:
                time.sleep(poll)
                try:
                    state = ws.call("Runtime.evaluate", {
                        "expression": "document.readyState", "returnByValue": True,
                    }, session_id=sid, timeout=timeout).get("result", {}).get("value")
                except CDPError:
                    break
                if state != "complete":
                    continue
                title = ws.call("Runtime.evaluate", {
                    "expression": "document.title", "returnByValue": True,
                }, session_id=sid, timeout=timeout).get("result", {}).get("value") or ""
                got = ws.call("Runtime.evaluate", {
                    "expression": read_js, "returnByValue": True,
                }, session_id=sid, timeout=timeout).get("result", {}).get("value") or {}
                blocked = bool(ws.call("Runtime.evaluate", {
                    "expression": OBSTACLE_JS, "returnByValue": True,
                }, session_id=sid, timeout=timeout).get("result", {}).get("value"))
                if blocked:
                    break
                if len(got.get("text") or "") >= min_chars:
                    break
                # 有些站是"首屏骨架 → 正文异步注入"，多等几轮
            final_url = ws.call("Runtime.evaluate", {
                "expression": "location.href", "returnByValue": True,
            }, session_id=sid, timeout=timeout).get("result", {}).get("value") or url
            text = got.get("text") or ""
            return {
                "ok": bool(text and len(text) >= 60),
                "html": got.get("html") or "",
                "text": text,
                "found": got.get("found") or "",
                "title": title or "",
                "url": final_url,
                "chars": len(text),
                "blocked": blocked,
                "waited": round(time.time() - t0, 2),
                "port": port,
            }
        finally:
            try:
                ws.call("Target.closeTarget", {"targetId": tid}, timeout=6)
            except Exception:
                pass
