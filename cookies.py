# -*- coding: utf-8 -*-
"""登录态管理：让抓取带上你浏览器里已经登录的那套 cookie。

## 解决什么问题

知乎、部分微信公众号、掘金专栏这类站点，**没有登录态就 403**。
实测本机（2026-10-16）：

    裸 requests 抓 zhuanlan.zhihu.com  → HTTP 403（zse 挑战页，650 字节）
    带上浏览器的 cookie（未登录）      → HTTP 200（风控过了，但软 404）
    带上浏览器的 cookie（已登录）      → 正常返回全文

所以只要能把你浏览器里的 cookie 拿过来，问题就解决了。

## 两种拿 cookie 的方式

1. **从浏览器一键导入**（推荐，可反复刷新）
   走 CDP 连本机调试端口，`Storage.getCookies` 能连 HttpOnly 的 `z_c0`
   一起拿到 —— 这是 `document.cookie` 做不到的。
   需要浏览器以调试端口启动，见 `start-browser-login.bat`。

2. **手动粘贴 Cookie**
   浏览器 F12 → Network → 随便点一个请求 → 复制 `Cookie` 请求头 → 粘进来。
   从 Network 面板复制能拿到 HttpOnly 的 cookie（从 Console 敲
   `document.cookie` 拿不到 `z_c0`，这点很关键）。

## 两个必须配套的东西

- **UA 要跟着 cookie 一起用**。`__zse_ck` 与 UA/IP 绑定，换 UA 就失效。
  所以导入时把浏览器的真实 UA 一起存下来（`cdp.browser_info()` 会把
  `HeadlessChrome` 抹成 `Chrome`，否则知乎一眼识别无头）。
- **Referer 要用同站首页**。知乎对没有 Referer 的跨站请求更敏感。

## 存储

`cookies.json`，按站点主域分组。只保留需要登录态的站点，
不把你浏览器里的全部 cookie 落盘。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import cdp

BASE = Path(__file__).resolve().parent
STORE_FILE = BASE / "cookies.json"

# 需要登录态的站点：主域 → 展示信息。导入时只挑这些，避免把浏览器
# 全部浏览痕迹写进文件；也顺带告诉用户"这个站要哪几个 cookie 才算登录"。
SITES: dict[str, dict] = {
    "zhihu.com": {
        "name": "知乎",
        "referer": "https://www.zhihu.com/",
        "关键 cookie": ["z_c0", "d_c0", "__zse_ck"],
        "hint": "z_c0 是登录凭证（HttpOnly），少了就只能拿到软 404",
    },
    "mp.weixin.qq.com": {
        "name": "微信公众号",
        "referer": "https://mp.weixin.qq.com/",
        "关键 cookie": ["wap_sid2", "wxuin"],
        "hint": "多数公众号文章不带登录态也能抓，抓不到时再配",
    },
    "juejin.cn": {
        "name": "掘金",
        "referer": "https://juejin.cn/",
        "关键 cookie": ["sessionid"],
        "hint": "掘金风控看频率，登录态能明显降低触发概率",
    },
    "csdn.net": {
        "name": "CSDN",
        "referer": "https://www.csdn.net/",
        "关键 cookie": ["UserToken", "UserNick"],
        "hint": "CSDN 复制限制靠登录态绕过",
    },
    "xiaohongshu.com": {
        "name": "小红书",
        "referer": "https://www.xiaohongshu.com/",
        "关键 cookie": ["web_session", "a1"],
        "hint": "小红书强依赖 a1 + web_session",
    },
    "weibo.com": {
        "name": "微博",
        "referer": "https://weibo.com/",
        "关键 cookie": ["SUB", "SUBP"],
        "hint": "SUB 是登录凭证",
    },
    "bilibili.com": {
        "name": "哔哩哔哩",
        "referer": "https://www.bilibili.com/",
        "关键 cookie": ["SESSDATA", "bili_jct"],
        "hint": "专栏 / 动态正文需要 SESSDATA",
    },
    "36kr.com": {
        "name": "36氪",
        "referer": "https://36kr.com/",
        "关键 cookie": ["userid"],
        "hint": "36氪有安全检测页，登录态 + 慢速请求更稳",
    },
    "douban.com": {
        "name": "豆瓣",
        "referer": "https://www.douban.com/",
        "关键 cookie": ["dbcl2"],
        "hint": "dbcl2 是登录凭证",
    },
    "medium.com": {
        "name": "Medium",
        "referer": "https://medium.com/",
        "关键 cookie": ["sid", "uid"],
        "hint": "付费墙文章需要登录态",
    },
}

# 常见的"这不是我的域名"的辅助域，导入时忽略
IGNORE_HOSTS = ("localhost", "127.0.0.1", "::1", "")


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# 域名归一
# --------------------------------------------------------------------------

def host_of(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return (urlparse(url).hostname or "").lower()


def base_domain(host: str) -> str:
    """把 www.zhihu.com / zhuanlan.zhihu.com 归到 zhihu.com。

    只在已知站点表里做映射 —— 通用"取后两段"会把 co.uk、
    com.cn 这类多段后缀切错，与其猜不如认表。
    """
    host = (host or "").lower()
    best = ""
    for d in SITES:
        if host == d or host.endswith("." + d):
            if len(d) > len(best):
                best = d
    return best


def site_info_for(url: str) -> Optional[dict]:
    d = base_domain(host_of(url))
    if not d:
        return None
    info = dict(SITES[d])
    info["domain"] = d
    return info


def known_domains() -> list[str]:
    return sorted(SITES)


# --------------------------------------------------------------------------
# Cookie 文本解析
# --------------------------------------------------------------------------

_CURL_COOKIE_RE = re.compile(
    r"(?:-H|--header)\s+(['\"])cookie:\s*(.*?)\1", re.I | re.S)
_COOKIE_LINE_RE = re.compile(r"^\s*cookie:\s*(.+)$", re.I | re.M)


def parse_cookie_text(raw: str) -> dict:
    """从用户粘贴的任意形态里抠出 cookie。

    支持三种粘法：
      1. 纯 Cookie 头   `z_c0=xxx; d_c0=yyy`
      2. 从 Network 复制的整行 `Cookie: z_c0=xxx; d_c0=yyy`（可能带前导空白）
      3. "Copy as cURL" 的整段命令（从中找 -H 'cookie: ...'）
    """
    if not raw:
        return {}
    s = raw.strip()
    # cURL 形式：整段命令里找 cookie 头
    m = _CURL_COOKIE_RE.search(s)
    if m:
        s = m.group(2)
    else:
        m = _COOKIE_LINE_RE.search(s)
        if m:
            s = m.group(1)

    out: dict[str, str] = {}
    # 换行也可能当分隔符（有些人从表格里复制会变成一行一个）
    for part in re.split(r"[;\n]+", s):
        part = part.strip().strip(",")
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and not k.startswith(":"):     # ":authority" 这种伪头跳过
            out[k] = v
    return out


def header_string(jar: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

def load() -> dict:
    if not STORE_FILE.exists():
        return {"sites": {}}
    try:
        d = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {"sites": {}}
        d.setdefault("sites", {})
        return d
    except Exception:
        # 文件坏了不该让整个引擎起不来，当成没配置
        return {"sites": {}}


def save(store: dict) -> None:
    store["updated"] = _now()
    STORE_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def set_site(domain: str, jar: dict, *, ua: str = "", source: str = "manual",
             note: str = "", referer: str = "", merge: bool = True) -> dict:
    """写入一个站点的登录态。

    **域名一定要先归一**再存。用户给的是 `www.zhihu.com`，读的时候
    `for_url()` 查的是 `zhihu.com` —— 不归一就会写成两个键，
    表现为"明明粘贴成功了，抓取时还是没带上"。（实测踩过）

    `merge=True` 时与已有 cookie 合并而不是覆盖：用户往往只想补一个
    关键的 z_c0，不该把他之前导入的整套冲掉。从浏览器导入时传
    merge=False —— 那份是完整的一手数据。
    """
    d = base_domain(domain) or (domain or "").strip().lower()
    store = load()
    entry = store["sites"].get(d) or {}
    new_jar = dict((entry.get("cookie") or {}) if merge else {})
    new_jar.update(jar)
    entry.update({
        "cookie": new_jar,
        "updated": _now(),
        "source": source,
        "count": len(new_jar),
        "ua": ua or entry.get("ua", ""),
        "note": note or entry.get("note", ""),
        "referer": referer or entry.get("referer", "") or
                   (SITES.get(d, {}).get("referer") or ""),
    })
    store["sites"][d] = entry
    save(store)
    return entry


def clear(domain: str = "") -> int:
    store = load()
    if not domain:
        n = len(store.get("sites", {}))
        store["sites"] = {}
        save(store)
        return n
    if domain in store.get("sites", {}):
        del store["sites"][domain]
        save(store)
        return 1
    return 0


# --------------------------------------------------------------------------
# 取用
# --------------------------------------------------------------------------

def for_url(url: str) -> Optional[dict]:
    """这个 URL 该用哪套登录态。没有配置就返回 None（而不是空 dict）。"""
    d = base_domain(host_of(url))
    if not d:
        return None
    entry = load()["sites"].get(d)
    if not entry or not entry.get("cookie"):
        return None
    return {
        "domain": d,
        "jar": entry["cookie"],
        "header": header_string(entry["cookie"]),
        "ua": entry.get("ua") or "",
        "referer": entry.get("referer") or SITES.get(d, {}).get("referer") or "",
        "source": entry.get("source") or "manual",
        "updated": entry.get("updated") or 0,
        "age": max(0, _now() - int(entry.get("updated") or 0)),
    }


def status() -> dict:
    """给前端看的：每个认识的站点配没配、什么来源、多久前更新的。"""
    store = load()
    sites = store.get("sites") or {}
    out = []
    for d in known_domains():
        e = sites.get(d)
        meta = SITES[d]
        rec = {
            "domain": d,
            "name": meta["name"],
            "configured": bool(e and e.get("cookie")),
            "count": len((e or {}).get("cookie") or {}),
            "source": (e or {}).get("source") or "",
            "updated": (e or {}).get("updated") or 0,
            "age": max(0, _now() - int((e or {}).get("updated") or 0)) if e else 0,
            "hint": meta.get("hint", ""),
            "keys": meta.get("关键 cookie", []),
            "has_login": _looks_logged_in(d, (e or {}).get("cookie") or {}),
        }
        out.append(rec)
    return {
        "sites": out,
        "configured": [r["domain"] for r in out if r["configured"]],
        "updated": store.get("updated") or 0,
    }


def _looks_logged_in(domain: str, jar: dict) -> bool:
    """关键 cookie 里有没有登录凭证（而不是只有匿名设备号）。"""
    creds = {
        "zhihu.com": ("z_c0",),
        "mp.weixin.qq.com": ("wap_sid2", "wxuin"),
        "juejin.cn": ("sessionid",),
        "csdn.net": ("UserToken",),
        "xiaohongshu.com": ("web_session",),
        "weibo.com": ("SUB",),
        "bilibili.com": ("SESSDATA",),
        "douban.com": ("dbcl2",),
        "medium.com": ("sid", "uid"),
        "36kr.com": ("userid",),
    }.get(domain, ())
    if not creds:
        return bool(jar)
    return any(k in jar and jar.get(k) for k in creds)


# --------------------------------------------------------------------------
# 从浏览器导入
# --------------------------------------------------------------------------

def import_from_cdp(ports=None, timeout: float = 10.0,
                    warmup=None, warmup_wait: float = 5.0) -> dict:
    """连本机调试浏览器，把它登录态里的相关站点 cookie 全套搬过来。

    `warmup` 传一组地址（通常是站点首页）时，会先让浏览器真的去访问一次。
    **这一步很关键**：风控 cookie 寿命极短，直接把凉掉的搬过来会照样 403；
    浏览器刚访问过的话，拿到的是热乎的。

    返回 {ok, port, browser, ua, imported: [...], found: {...}, error}
    """
    info = cdp.pick_browser(ports or cdp.DEFAULT_PORTS)
    if not info:
        return {
            "ok": False,
            "error": "没找到开着调试端口的浏览器。点「打开登录窗口」启动一个，"
                     "在里面登录一次知乎，再回来点导入。",
            "hint": "也可以直接手动粘贴 Cookie（不需要浏览器配合）",
        }

    warmed = None
    if warmup:
        try:
            warmed = cdp.warmup(info["port"], warmup, wait=warmup_wait,
                                ws_url=info.get("webSocketDebuggerUrl"),
                                timeout=max(timeout, 20.0))
        except Exception as e:
            warmed = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}

    try:
        raw = cdp.cookies(info["port"], ws_url=info.get("webSocketDebuggerUrl"),
                          timeout=timeout)
    except Exception as e:
        return {"ok": False, "port": info.get("port"), "warmup": warmed,
                "error": f"连上了但取 cookie 失败：{type(e).__name__}: {str(e)[:160]}"}

    # 按主域归组。同一个域名可能有多条（不同 path/子域），合并。
    grouped: dict[str, dict] = {}
    for c in raw:
        dom = (c.get("domain") or "").lstrip(".").lower()
        if dom in IGNORE_HOSTS:
            continue
        base = base_domain(dom)
        if not base:
            continue
        jar = grouped.setdefault(base, {})
        jar[c["name"]] = c.get("value", "")

    ua = info.get("User-Agent") or ""
    imported, skipped = [], []
    for dom, jar in grouped.items():
        meta = SITES.get(dom) or {}
        # merge=False：浏览器给的是一手完整数据，直接覆盖
        set_site(dom, jar, ua=ua, source="browser", merge=False,
                 referer=meta.get("referer", ""))
        rec = {"domain": dom, "name": meta.get("name", dom), "count": len(jar),
               "logged_in": _looks_logged_in(dom, jar)}
        (imported if rec["logged_in"] or dom == "zhihu.com" else skipped).append(rec)

    return {
        "ok": True,
        "port": info.get("port"),
        "browser": info.get("Browser"),
        "headless": info.get("headless"),
        "ua": ua,
        "warmup": warmed,
        "imported": imported,
        "skipped": skipped,
        "error": "",
    }


def warmup_urls_for(url: str) -> list[str]:
    """抓这个链接之前，先让浏览器访问哪些地址最有用。

    只开首页就够 —— 风控 cookie 是在站点域上种的，首页能种全。
    """
    d = base_domain(host_of(url))
    if not d:
        return []
    ref = (SITES.get(d) or {}).get("referer") or f"https://{d}/"
    return [ref]


def probe_browsers(ports=None) -> list[dict]:
    """扫一遍端口，看看哪些开着（给前端显示"能不能一键导入"）。"""
    out = []
    for p in (ports or cdp.DEFAULT_PORTS):
        info = cdp.browser_info(int(p))
        if info:
            out.append({"port": int(p), "browser": info.get("Browser"),
                        "headless": info.get("headless")})
    return out


def refresh_if_stale(max_age: int = 1800) -> dict:
    """登录态过期了就自动从浏览器刷一遍。

    `__zse_ck` 这类风控 cookie 寿命不长，被抓到 403 时主动刷新一次
    比让用户手动点更省事。
    """
    info = cdp.pick_browser()
    if not info:
        return {"ok": False, "error": "本机没有可用的调试浏览器"}
    store = load().get("sites") or {}
    need = False
    for dom, e in store.items():
        if max_age <= 0 or (_now() - int(e.get("updated") or 0)) > max_age:
            need = True
            break
    if not need:
        return {"ok": True, "refreshed": False, "reason": "登录态还新鲜"}
    r = import_from_cdp()
    r["refreshed"] = True
    return r
