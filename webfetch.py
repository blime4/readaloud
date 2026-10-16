# -*- coding: utf-8 -*-
"""网页正文抓取：把一个链接变成"人念得下去"的稿子。

这个模块的目标不是"把 HTML 转成文本"，而是"还原出一份能直接朗读的稿子"。
两者差别很大 —— 后者要额外解决四件事：

1. **编码探测**
   很多站点的 Content-Type 只有 `text/html` 不带 charset，requests 会退回
   ISO-8859-1，中文整段变乱码。必须自己按 meta charset / 实际字节去猜。

2. **段落重排**
   英文站（和部分中文站）用硬换行排版，一段话被拆成十几行。直接读出来就是
   "句子说到一半 / 突然停顿 / 再接着下一行"——断句全乱。必须把同一段的碎行接回去。

3. **噪声清理**
   导航条、面包屑、推广语、相关文章、二维码提示、作者简介、版权声明……
   这些东西混在正文里，读出来非常荒谬（"你的鼓励是创作者最好的动力"）。

4. **可信度自评**
   抓到的不一定是正文：可能是"正在安全检测"、登录墙、或者根本是空壳。
   上层需要知道"这次抓得对不对"，才能决定换策略还是让用户手动复制。
"""

from __future__ import annotations

import json
import re
import socket
import html as _html
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

HEADERS_DESKTOP = {
    "User-Agent": UA_DESKTOP,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
HEADERS_MOBILE = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 「这不是正文」的页面特征 —— 命中就说明被拦了或需要登录
BLOCK_SIGNS = (
    ("安全检测", "站点正在做安全检测，请稍后重试或用浏览器打开"),
    ("环境异常", "站点判定访问环境异常，需要人工验证"),
    ("请开启JavaScript", "页面需要执行 JavaScript 才能显示内容"),
    ("Please enable JavaScript", "页面需要执行 JavaScript 才能显示内容"),
    ("Just a moment", "站点在做真人校验（Cloudflare）"),
    ("Verify you are human", "站点要求人机验证"),
    ("Please wait...", "站点在做风控校验（返回的是等待页）"),
    ("argus-csp-token", "站点在做风控校验"),
    ("滑动验证", "站点要求滑块验证"),
    ("登录后查看", "内容需要登录才能查看"),
    ("请先登录", "内容需要登录才能查看"),
    ("Sign in to continue", "内容需要登录才能查看"),
    ("Subscribe to continue", "内容被订阅墙拦住"),
    ("该内容已被发布者删除", "原内容已被删除"),
    ("此内容因违规无法查看", "原内容已被平台下架"),
    ("404 Not Found", "页面不存在"),
    ("页面不存在", "页面不存在"),
)

# 整行就是噪声（全行匹配才算）
JUNK_FULL_RE = re.compile(
    r"^(?:"
    r"点赞|在看|分享|收藏|转发|关注|订阅|广告|赞助|评论|举报|更多|目录|导航|返回|顶部|底部|"
    r"充电|赞赏|打赏|充电鼓励|赞赏支持|打赏支持|"
    r"编辑\s*[:：]\s*$|校对\s*[:：]\s*$|审核\s*[:：]\s*$|"
    r"注\s*[:：]\s*$|声明\s*[:：]\s*$|作者\s*[:：]\s*$"
    r")$",
    re.I)

# 短行里出现即视为噪声（推广语、互动引导、版权尾巴……）
JUNK_FRAG_RE = re.compile(
    r"点击上方.{0,10}关注|扫码关注|长按识别.{0,10}|微信扫一扫|"
    r"关注我们|关注公众号|订阅我们|"
    r"你的鼓励|创作者最好的动力|创作不易|码字不易|点个在看|一键三连|"
    r"本文正在参加|本文首发于|首发于.{0,10}公众号|"
    r"转载请注明|版权归|免责声明|特别声明|以上内容.{0,6}不代表|"
    r"投稿邮箱|商务合作|合作请|联系作者|"
    r"阅读原文|查看原文|原文链接|往期推荐|往期回顾|推荐阅读|相关阅读|相关文章|"
    r"延伸阅读|更多精彩.{0,10}|更多内容.{0,10}|精选文章|热门推荐|"
    r"返回顶部|回到顶部|上一篇|下一篇|"
    r"扫码.{0,8}加入|加入社群|微信群|读者群|"
    r"关注我的|我的公众号|个人公众号|可以帮忙关注|点赞、收藏|点赞收藏|"
    r"专栏历史文章|历史文章\s*[：:]|后续将分享|可以帮忙点|"
    r"本文{0,2}未经授权|未经允许.{0,10}转载",
    re.I)

# 整行是链接/域名/日期
JUNK_LINKLINE_RE = re.compile(
    r"^(?:[\w\u4e00-\u9fff\s]{0,12})?(?:https?://|www\.)\S+|"
    r"^\S+\.(?:com|cn|net|org|io|me)(?:/\S*)?$|"
    r"^[\d\s\-/.年月日:：]{6,24}$")

# 行内出现的域名（判断"这是不是一条链接残片"）
DOMAIN_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b[\w\-]{2,}\.(?:com|cn|net|org|io|me|co|dev|app|xyz|"
    r"tech|info|top|site|ai|tv|fun)(?:/\S*)?",
    re.I)

# 页面结构噪声容器
NOISE_BLOCK_RE = re.compile(
    r"<(script|style|noscript|svg|iframe|template|nav|header|footer|aside|form|"
    r"select|button|canvas|video|audio|figure|figcaption)\b[^>]*>.*?</\1\s*>",
    re.S | re.I)
NOISE_ATTR_RE = re.compile(
    r"<(div|section|ul|ol|span|p|dl|table)\b[^>]*(?:id|class)\s*=\s*[\"'][^\"']*"
    r"(?:nav|menu|sidebar|breadcrumb|comment|copyright|footer|header|toolbar|share|"
    r"related|recommend|advert|\bads?\b|banner|pagination|tag-list|tags|meta-info|"
    r"catalog|toc|qrcode|qr-code|reward|subscribe|follow|author-card|profile)\b"
    r"[^\"']*[\"'][^>]*>.*?</\1\s*>",
    re.S | re.I)

# 站点适配：优先只取这些容器
SITE_HINTS = (
    ("mp.weixin.qq.com", (r'<div[^>]*id=["\']js_content["\']',)),
    ("zhihu.com", (r'<div[^>]*class=["\'][^"\']*RichContent-inner',
                   r'<div[^>]*class=["\'][^"\']*RichText',
                   r'<div[^>]*itemprop=["\']text["\']')),
    ("csdn.net", (r'<div[^>]*id=["\']content_views["\']',
                  r'<article[^>]*class=["\'][^"\']*baidu_pl')),
    ("juejin.cn", (r'<div[^>]*class=["\'][^"\']*markdown-body',)),
    ("sspai.com", (r'<div[^>]*class=["\'][^"\']*content',)),
    ("36kr.com", (r'<div[^>]*class=["\'][^"\']*articleDetail',)),
    ("toutiao.com", (r'<div[^>]*class=["\'][^"\']*article-content',)),
    ("medium.com", (r'<article\b',)),
    ("substack.com", (r'<div[^>]*class=["\'][^"\']*available-content',)),
    ("github.com", (r'<article[^>]*class=["\'][^"\']*markdown-body',)),
    ("wikipedia.org", (r'<div[^>]*id=["\']mw-content-text["\']',)),
    ("cnblogs.com", (r'<div[^>]*id=["\']cnblogs_post_body["\']',)),
    ("jianshu.com", (r'<article\b',)),
)

# 通用正文容器（按优先级）
GENERIC_SELECTORS = (
    r'<article\b[^>]*>',
    r'<div[^>]*(?:id|class)\s*=\s*["\'][^"\']*'
    r'(?:article-content|post-content|entry-content|content-body|main-content|'
    r'rich_media_content|markdown-body|article__content|post__content)[^"\']*["\'][^>]*>',
    r'<div[^>]*itemprop=["\']articleBody["\'][^>]*>',
    r'<main\b[^>]*>',
)

# 块级标签收尾 → 换行；<li> 前补一个列表符号，保住条目结构
BLOCK_END_RE = re.compile(r"</(p|div|li|h[1-6]|tr|section|article|blockquote|dd|dt|pre)\s*>", re.I)
LI_OPEN_RE = re.compile(r"<li\b[^>]*>", re.I)
BR_RE = re.compile(r"<br\s*/?>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
# 标题和引文要留下结构标记：不然后面"标题慢读、条目紧凑、引用留白"这套语气
# 分层对网页文章就完全失效 —— 抓来的每一段都变成普通段落，读着是平的。
H_OPEN_RE = re.compile(r"<h([1-6])\b[^>]*>", re.I)
QUOTE_OPEN_RE = re.compile(r"<blockquote\b[^>]*>", re.I)

# 带结构标记的行（标题 / 引文 / 列表项）
STRUCT_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|>\s|[-*•·▪◦]\s|\(?\d{1,2}[.)、]\s?)")

# 列表项前缀（这些行不能被合并进上一段）
LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*•·▪◦]\s|\(?\d{1,2}[.)、]\s?|[一二三四五六七八九十]{1,3}[、.)]\s?|[（(][一二三四五六七八九十\d]{1,3}[）)]\s?)")

# 句末标点：以此为结尾说明这段话说完了
SENT_TAIL_RE = re.compile(r"[。！？!?…；;]\s*$|[.!?][\"'”’)\]]?\s*$")
# 真正的句子收尾（不含省略号 —— 链接被截断时也会以 … 结尾）
REAL_END_RE = re.compile(r"[。！？!?；;]\s*$|[.!?][\"'”’)\]]?\s*$")


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------

def pick_encoding(resp: requests.Response) -> str:
    """按「HTTP 头 → meta 标签 → 字节猜测」的顺序定编码。

    很多站点 Content-Type 只写 text/html 不给 charset，requests 会退回
    ISO-8859-1，中文整段乱码 —— 36氪就是典型受害者。
    """
    # 1) HTTP 头里明确写了，且不是 requests 的兜底值
    ctype = resp.headers.get("Content-Type", "")
    m = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", ctype, re.I)
    if m:
        enc = m.group(1).lower()
        if enc not in ("iso-8859-1", "latin-1", "ascii", "us-ascii"):
            return enc

    # 2) HTML 里的 meta charset（只看前 4KB）
    head = resp.content[:4096]
    for pat in (rb'<meta[^>]+charset\s*=\s*["\']?\s*([\w\-]+)',
                rb'<\?xml[^>]+encoding\s*=\s*["\']([\w\-]+)'):
        m = re.search(pat, head, re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore").lower()
            if enc and enc not in ("iso-8859-1", "latin-1"):
                return enc

    # 3) 让 chardet/chardet 系猜（requests 内置 charset_normalizer）
    try:
        if resp.apparent_encoding:
            return resp.apparent_encoding
    except Exception:
        pass
    return "utf-8"


def decode(resp: requests.Response) -> str:
    """拿到正确解码的 HTML，且保证解码后中文不是乱码。"""
    enc = pick_encoding(resp)
    try:
        text = resp.content.decode(enc, errors="replace")
    except (LookupError, TypeError):
        text = resp.content.decode("utf-8", errors="replace")

    # 兜底校验：如果解出来全是替换符，换 utf-8 / gbk 再试
    if text.count("\ufffd") > max(20, len(text) * 0.002):
        for alt in ("utf-8", "gbk", "big5", "latin-1"):
            try:
                cand = resp.content.decode(alt, errors="replace")
            except Exception:
                continue
            if cand.count("\ufffd") < text.count("\ufffd"):
                text = cand
                if not text.count("\ufffd"):
                    break
    return text


# --------------------------------------------------------------------------
# 结构处理
# --------------------------------------------------------------------------

def extract_balanced(html: str, start: int, tag: str = "div") -> str:
    """从 start（指向某个 <div 的 '<'）开始，取出标签配平的片段。

    正则做不了嵌套 div 的配平，而正文容器几乎都嵌套。数一数就好了。
    """
    open_re = re.compile(r"<" + tag + r"\b", re.I)
    close_re = re.compile(r"</" + tag + r"\s*>", re.I)
    depth = 0
    i = start
    n = len(html)
    while i < n:
        o = open_re.search(html, i)
        c = close_re.search(html, i)
        if not c:
            return html[start:]
        if o and o.start() < c.start():
            depth += 1
            i = o.end()
        else:
            depth -= 1
            i = c.end()
            if depth <= 0:
                return html[start:i]
    return html[start:]


def site_container(url: str, html: str) -> Optional[str]:
    """站点专属容器：命中就只留这块，噪声天然少一大截。"""
    host = (urlparse(url).hostname or "").lower()
    pats: Iterable[str] = ()
    for key, p in SITE_HINTS:
        if key in host:
            pats = p
            break
    for pat in pats:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        tag = "article" if m.group(0).lower().startswith("<article") else "div"
        frag = extract_balanced(html, m.start(), tag)
        if len(TAG_RE.sub("", frag)) >= 150:
            return frag
    return None


def generic_container(html: str) -> Optional[str]:
    """通用容器：按常见正文 class/id 找，取「剥完标签后最长」的那个。"""
    best = None
    best_len = 0
    for pat in GENERIC_SELECTORS:
        for m in re.finditer(pat, html, re.I):
            tag = "article" if m.group(0).lower().startswith("<article") else \
                  "main" if m.group(0).lower().startswith("<main") else "div"
            frag = extract_balanced(html, m.start(), tag)
            ln = len(TAG_RE.sub("", frag))
            if ln > best_len:
                best, best_len = frag, ln
    return best if best_len >= 150 else None


def strip_noise(html: str) -> str:
    s = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    s = NOISE_BLOCK_RE.sub(" ", s)
    # 属性噪声是嵌套的，多跑两轮能清得更干净
    for _ in range(2):
        new = NOISE_ATTR_RE.sub(" ", s)
        if new == s:
            break
        s = new
    return s


def html_to_lines(html: str) -> list[str]:
    """HTML → 行列表。保留标题/引文/列表条目的结构标记，保住"语气层次"。"""
    s = html
    # 标题 → Markdown 井号，引文 → 尖括号，列表项 → 短横线。
    # 下游 text_nlp.parse_blocks 认这三种标记，转换成对应的朗读停顿与语速。
    s = H_OPEN_RE.sub(lambda m: "\n" + "#" * int(m.group(1)) + " ", s)
    s = QUOTE_OPEN_RE.sub("\n> ", s)
    s = LI_OPEN_RE.sub("\n- ", s)
    s = BR_RE.sub("\n", s)
    s = BLOCK_END_RE.sub("\n", s)
    s = TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    s = s.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    out = []
    for line in s.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if line:
            out.append(line)
    return out


def reflow(lines: list[str]) -> list[str]:
    """段落重排：把被硬换行切碎的句子接回完整段落。

    英文站（如 paulgraham.com）一段话被拆成十几行，直接读就是断句灾难。
    规则：
      - 带结构标记的行（标题 / 引文 / 列表项）独立成段，不参与合并；
      - 上一行以句末标点收尾 → 另起一段；
      - 否则接到上一行后面（中文直接接，西文补一个空格）。
    """
    paras: list[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            paras.append(buf)
            buf = ""

    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue

        if STRUCT_LINE_RE.match(ln):
            flush()
            paras.append(ln)
            continue

        if not buf:
            buf = ln
            continue

        if SENT_TAIL_RE.search(buf) and len(buf) >= 8:
            flush()
            buf = ln
            continue

        # 接上去：中文之间不插空格，西文之间插空格
        if buf[-1].isascii() and ln[0].isascii():
            buf += " " + ln
        else:
            buf += ln

    flush()
    return paras


def trim_tail_wall(paras: list[str]) -> tuple[list[str], int]:
    """砍掉文章末尾的「相关阅读墙 / 目录」。

    站点习惯在正文容器末尾塞一坨其他文章标题，特征是连续好几行都又短、
    又没有句末标点（标题本来就不带句号）。它前面必然还有正文，所以只要
    尾部连续成串就截掉，不碰中间内容。
    """
    if len(paras) < 8:
        return paras, 0
    i = len(paras)
    run = 0
    while i > 0:
        t = paras[i - 1]
        if len(t) <= 42 and not REAL_END_RE.search(t):
            run += 1
            i -= 1
        else:
            break
    if run >= 3 and i >= 3:
        return paras[:i], run
    return paras, 0


def tidy_wiki_annotations(text: str) -> str:
    """维基类站点会把链接注释渲染成「（英语 ： XXX ）」，读出来很碎。"""
    text = re.sub(r"（\s*(?:英语|中文|日语|德语|法语|俄语|西班牙语)\s*[：:]\s*", "（", text)
    text = re.sub(r"（\s+", "（", text)
    text = re.sub(r"\s+）", "）", text)
    return text


def is_template_junk(t: str) -> bool:
    """模板重复行：同一小撮字在一行里翻来覆去。

    站点把作者卡片/标签云塞进正文容器时就会长这样：
    「趁着雨少数派作者趁着雨趁着雨少数派作者联合作者趁着雨」
    特征很明确 —— 字很少、长度不短、字符种类极度贫乏。
    """
    if len(t) < 18 or len(t) > 160:
        return False
    if re.search(r"[。！？!?]", t):
        return False
    uniq = len(set(t))
    return uniq / len(t) < 0.38


def drop_junk(paras: list[str]) -> tuple[list[str], int]:
    """丢掉明显的噪声行，返回 (干净段落, 丢掉的数量)。"""
    kept = []
    dropped = 0
    total = len(paras)
    for p in paras:
        t = p.strip()
        # 带结构标记的行（标题/引文/列表项）通常短于 14 字也照样有意义：
        # "# 快速面板"、"引文原话" 这些不能被"太短就当菜单丢掉"的规则误伤。
        is_struct = bool(STRUCT_LINE_RE.match(t))
        core = re.sub(r"^\s*(?:#{1,6}\s+|>\s+|[-*•·▪◦]\s+)", "", t).strip()
        # 结构行带标记，说明上游确实是从 <h?> / <li> / <blockquote> 里提出来的，
        # 门槛放宽到 2 字（"引言"这种短标题真实存在）；普通行仍要求 4 字。
        too_short = len(core) < 2 if is_struct else len(t) < 4
        if too_short:
            dropped += 1
            continue
        # 整行就是"点赞/关注/赞赏"这类按钮文案
        if JUNK_FULL_RE.match(core):
            dropped += 1
            continue
        # 推广语/互动引导：短行里出现就丢；长段落里出现说明是正常行文，保留
        if len(core) < 70 and JUNK_FRAG_RE.search(core):
            dropped += 1
            continue
        if JUNK_LINKLINE_RE.match(core):
            dropped += 1
            continue
        # 相关阅读墙：带域名、又不是完整句子
        if len(t) < 90 and DOMAIN_RE.search(core) and not REAL_END_RE.search(core):
            dropped += 1
            continue
        # 模板重复行（作者卡片、标签云）
        if is_template_junk(t):
            dropped += 1
            continue
        # 纯符号/纯 emoji
        if not re.search(r"[\w\u4e00-\u9fff]", t):
            dropped += 1
            continue
        # 短行且没有任何句读 → 多半是菜单/标签（结构行除外）
        if not is_struct and len(t) < 14 and not re.search(r"[。！？；，、：.!?;,]", t):
            dropped += 1
            continue
        kept.append(t)

    # 若砍得太狠（剩下不到 15%），说明规则误伤，退回原样
    if total >= 8 and len(kept) < total * 0.15:
        return paras, 0
    return kept, dropped


# --------------------------------------------------------------------------
# 质量评估
# --------------------------------------------------------------------------

def looks_blocked(text: str) -> Optional[str]:
    """判断抓到的到底是不是正文，返回拦截原因（没被拦就返回 None）。"""
    head = text[:2500]
    for sign, reason in BLOCK_SIGNS:
        if sign.lower() in head.lower():
            return reason
    return None


def score(text: str, title: str = "") -> dict:
    """给抓取结果打分（0-100）并给出诊断。"""
    chars = len(re.sub(r"\s", "", text))
    paras = [p for p in text.split("\n") if p.strip()]
    n = len(paras)
    avg = (chars / n) if n else 0

    s = 0.0
    s += min(45.0, chars / 40.0)              # 字数：1800 字满分
    s += min(25.0, n * 1.2)                   # 段落数
    s += min(20.0, avg / 3.0)                 # 平均段长（太碎说明没重排好）
    if title:
        s += 10.0
    return {"score": int(max(0, min(100, s))), "chars": chars,
            "paras": n, "avg_para": round(avg, 1)}


def classify(score_val: int) -> str:
    if score_val >= 65:
        return "good"
    if score_val >= 40:
        return "fair"
    return "poor"


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def _proxy_candidates(configured: str = "auto") -> list[Optional[str]]:
    """按配置决定用不用本地代理。

    「auto」= 探测 127.0.0.1:7897 有没有在听，在听就用 —— 本机 Clash 开了
    就能同时照顾到国内站点和需要出网的目标；没开就直连。
    """
    if configured and configured not in ("auto", "", None):
        return [configured, None]
    if configured in ("", None):
        return [None]
    for host, port in (("127.0.0.1", 7897),):
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return [f"http://{host}:{port}", None]
        except OSError:
            continue
    return [None]


def _get(url: str, mode: str, timeout: float, proxies: Optional[str]):
    headers = HEADERS_MOBILE if mode == "mobile" else HEADERS_DESKTOP
    kw = {"timeout": timeout, "headers": headers, "allow_redirects": True}
    if proxies:
        kw["proxies"] = {"http": proxies, "https": proxies}
    return requests.get(url, **kw)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def strip_leading_title(text: str, title: str) -> str:
    """正文开头常常把标题又抄了一遍，读出来会"念两遍标题"。"""
    if not title or not text:
        return text
    key = re.sub(r"[\s，。、：:！？!?\-|_—]+", "", title)
    if len(key) < 6:
        return text
    lines = text.split("\n")
    if not lines:
        return text
    first = re.sub(r"[\s，。、：:！？!?\-|_—]+", "", lines[0])
    if first and (key.startswith(first) or first.startswith(key)):
        return "\n".join(lines[1:]).lstrip()
    return text


def to_reading_text(html: str, url: str) -> tuple[str, str, str, int]:
    """HTML → (标题, 正文, 用了哪条提取路线, 丢掉的噪声行数)。"""
    title = ""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.I)
    if m:
        title = _html.unescape(m.group(1)).strip()
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        if m:
            title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    # 标题里常见的站点后缀
    title = re.sub(r"\s*[-|_—]\s*(微信公众平台|知乎|CSDN博客|掘金|少数派|36氪|简书|"
                   r"博客园|Google\s*GitHub)\s*$", "", title).strip()

    candidates: list[tuple[str, str]] = []

    frag = site_container(url, html)
    if frag:
        candidates.append(("站点适配", frag))
    frag2 = generic_container(html)
    if frag2:
        candidates.append(("通用容器", frag2))
    candidates.append(("整页", html))

    # 有 trafilatura 就多一路专业提取，让它和质量评分一起决定谁胜出
    try:
        import trafilatura  # type: ignore
        art = trafilatura.extract(html, url=url, include_comments=False,
                                  include_tables=False, favor_recall=True)
        if art and len(art.strip()) > 120:
            candidates.append(("trafilatura", art))
    except Exception:
        pass

    best_text, best_route, best_score, best_drop = "", "", -1, 0
    for route, frag in candidates:
        if route == "trafilatura":
            # trafilatura 已经输出 markdown 文本，只需再清一遍噪声行
            paras = [ln.strip() for ln in frag.split("\n") if ln.strip()]
        else:
            paras = reflow(html_to_lines(strip_noise(frag)))
        paras, drop_n = drop_junk(paras)
        paras, tail_n = trim_tail_wall(paras)
        drop_n += tail_n
        text = tidy_wiki_annotations("\n".join(paras))
        text = strip_leading_title(text, title)
        sc = score(text, title)["score"]
        # 略微偏爱正文容器：同样分数下容器比整页干净
        sc += 6 if route in ("站点适配", "通用容器") else 0
        sc += 4 if route == "trafilatura" else 0
        if sc > best_score:
            best_text, best_route, best_score, best_drop = text, route, sc, drop_n

    return title, best_text, best_route, best_drop


def fetch(url: str, mode: str = "auto", timeout: float = 25.0,
          proxy: str = "auto") -> dict:
    """抓一个链接并返回可直接朗读的正文 + 诊断信息。

    返回：
        ok     是否拿到了可用正文
        title  标题
        text   清洗后的正文（段落已重排）
        diag   诊断：字数/段落数/评分/路线/拦截原因/试过的策略
    """
    diag: dict = {"tried": []}
    if not re.match(r"^https?://", url or "", re.I):
        return {"ok": False, "error": "不是有效的 http/https 链接", "diag": diag}

    modes = ["mobile"] if mode == "mobile" else ["desktop"]
    if mode == "auto":
        modes = ["desktop", "mobile"]

    best: Optional[dict] = None
    last_err = ""

    for px in _proxy_candidates(proxy):
        for md in modes:
            tag = f"{md}{'+proxy' if px else ''}"
            try:
                r = _get(url, md, timeout, px)
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
                diag["tried"].append({"mode": tag, "error": last_err})
                continue

            diag["tried"].append({"mode": tag, "status": r.status_code,
                                  "bytes": len(r.content)})
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}"
                continue

            html = decode(r)
            title, text, route, dropped = to_reading_text(html, r.url)
            sc = score(text, title)
            # 只在"没抓到多少东西"时才怀疑被拦 —— 否则正文里出现
            # "登录后查看"之类的字样会被误判成拦截页。
            blocked = None
            if sc["chars"] < 600:
                blocked = looks_blocked(text) or looks_blocked(html[:4000])

            cand = {"ok": not blocked and sc["chars"] >= 120,
                    "title": title, "text": text, "url": r.url,
                    "chars": sc["chars"], "paras": sc["paras"],
                    "avg_para": sc["avg_para"], "quality": sc["score"],
                    "grade": classify(sc["score"]), "route": route,
                    "dropped": dropped, "html_bytes": len(r.content),
                    "blocked": blocked, "diag": diag, "mode": md,
                    "proxied": bool(px)}
            if cand["ok"]:
                # 桌面端拿到的通常比移动端完整，同分优先保留先拿到的
                if best is None or cand["quality"] > best["quality"]:
                    best = cand
            elif best is None:
                best = cand

            # 已经抓得不错就别再折腾了
            if cand["ok"] and cand["quality"] >= 70:
                best = cand
                break
        if best and best.get("ok") and best.get("quality", 0) >= 70:
            break

    if best is None:
        return {"ok": False, "diag": diag,
                "error": f"抓取失败：{last_err or '未知原因'}"}

    if not best["ok"]:
        reason = best.get("blocked")
        if not reason:
            # 页面本身挺大（>20KB）却几乎提不出文字 —— 这不是"正文短"，
            # 而是内容由脚本渲染，或者站点在限流/校验。说清楚用户才知道怎么办。
            if best.get("html_bytes", 0) > 20000:
                reason = ("页面拿到了但正文是脚本动态渲染的，"
                          "也可能是站点在限流或做校验")
            else:
                reason = f"正文太短（{best.get('chars', 0)} 字）"
        best["error"] = (f"没能提取到正文：{reason}。"
                         f"稍等片刻重试，或用浏览器打开后复制正文。")
    diag["picked"] = {"mode": best.get("mode"), "route": best.get("route"),
                      "quality": best.get("quality"), "chars": best.get("chars")}
    return best


if __name__ == "__main__":  # 手动调试：python webfetch.py <url>
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://sspai.com/post/86000"
    d = fetch(u)
    print(json.dumps({k: v for k, v in d.items() if k != "text"},
                     ensure_ascii=False, indent=2))
    print("--- 正文 ---")
    print(d.get("text", "")[:1500])
