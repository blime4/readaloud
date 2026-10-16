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
import time
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
    return _glue_bare_markers(out)


# 只有标记、没有内容的行。缩进过的 HTML 很常见：
#     <h1>\n  浅谈 One UI6.0 更新\n</h1>
# 井号和标题会被换行拆成两行 —— 如果不粘回去，下游既认不出这是标题，
# 又会把标题、作者卡片、正文第一句全糊成一段（实测踩过）。
_BARE_MARKER_RE = re.compile(r"^(#{1,6}|>|[-*•·▪◦]|\(?\d{1,2}[.)、])$")


def _glue_bare_markers(lines: list[str]) -> list[str]:
    out: list[str] = []
    pending = ""
    for line in lines:
        m = _BARE_MARKER_RE.match(line)
        if m:
            if pending:                  # 连着两个空标记，各自保留
                out.append(pending)
            pending = m.group(1)
            continue
        if pending:
            line = pending + " " + line
            pending = ""
        out.append(line)
    if pending:
        out.append(pending)
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


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_CL_PUNCT_RE = re.compile(r"[。！？；，、：.!?;,]")


def _is_junk_line(t: str, pre: bool = False) -> bool:
    """这一行是不是网页垃圾。pre=True 表示"段落重排之前"的判断。

    两遍判断的差别只在**长度类**规则上：重排前的"行"是原始排版单位，英文站
    一行两三个词是常态，按长度杀会直接吃掉正文；重排后的"行"已经是段落，
    短就是真短。内容类规则（按钮文案、推广语、整行链接）没有这个问题，两遍都查。
    """
    # 带结构标记的行（标题/引文/列表项）通常短于 14 字也照样有意义：
    # "# 快速面板"、"引文原话" 这些不能被"太短就当菜单丢掉"的规则误伤。
    is_struct = bool(STRUCT_LINE_RE.match(t))
    core = re.sub(r"^\s*(?:#{1,6}\s+|>\s+|[-*•·▪◦]\s+)", "", t).strip()
    cjk = bool(_CJK_RE.search(t))
    length_rules_apply = not (pre and not cjk)

    # 太短：结构行放宽到 2 字（"引言"这种短标题真实存在），普通行要求 4 字
    if length_rules_apply:
        too_short = len(core) < 2 if is_struct else len(t) < 4
        if too_short:
            return True
    # 整行就是"点赞/关注/赞赏"这类按钮文案
    if JUNK_FULL_RE.match(core):
        return True
    # 推广语/互动引导：短行里出现就丢；长段落里出现说明是正常行文，保留
    if len(core) < 70 and JUNK_FRAG_RE.search(core):
        return True
    if JUNK_LINKLINE_RE.match(core):
        return True
    # 纯符号/纯 emoji
    if not re.search(r"[\w\u4e00-\u9fff]", t):
        return True

    if pre:
        # 重排前只多查这一条：短中文行、没有任何句读 → 作者卡片 / 阅读时长 /
        # 标签云。必须在 reflow 把它们粘进正文之前杀掉。
        if cjk and not is_struct and len(t) <= 14 and not _CL_PUNCT_RE.search(t):
            return True
        # 其余规则是"段落级"的判断，用在碎行上会误伤（英文长句的行很容易
        # 落进模板重复行的长度窗口），重排后再查。
        return False

    if is_struct:
        return False
    # 相关阅读墙：带域名、又不是完整句子
    if len(t) < 90 and DOMAIN_RE.search(core) and not REAL_END_RE.search(core):
        return True
    # 模板重复行（作者卡片、标签云）
    if is_template_junk(t):
        return True
    # 短行且没有任何句读 → 多半是菜单/标签
    if len(t) < 14 and not _CL_PUNCT_RE.search(t):
        return True
    return False


def drop_junk(paras: list[str]) -> tuple[list[str], int]:
    """丢掉明显的噪声行，返回 (干净段落, 丢掉的数量)。"""
    return _drop(paras, pre=False)


def drop_junk_lines(lines: list[str]) -> tuple[list[str], int]:
    """段落重排**之前**先清一遍。

    非做不可的理由：作者卡片、阅读时长、"关注我们"这类碎片行又短又没有句读，
    一旦被 reflow 接进正文段落，就再也分不出来了 —— 输出会长成
    「作者名 少数派作者 7 分钟阅读 12月26日，我的手机收到了…」，
    读出来是灾难。必须在它们被粘起来之前杀掉。
    """
    return _drop(lines, pre=True)


def _drop(items: list[str], pre: bool) -> tuple[list[str], int]:
    kept = []
    dropped = 0
    total = len(items)
    for p in items:
        t = p.strip()
        if _is_junk_line(t, pre=pre):
            dropped += 1
            continue
        kept.append(t)

    # 若砍得太狠（剩下不到 15%），说明规则误伤，退回原样
    if total >= 8 and len(kept) < total * 0.15:
        return items, 0
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


# 真实浏览器发起导航时会带这几个头。知乎、小红书这类风控站对缺失它们的
# 请求更敏感 —— 补上之后"像不像浏览器"就不用靠猜了。
NAV_HEADERS = {
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _get(url: str, mode: str, timeout: float, proxies: Optional[str],
         login: Optional[dict] = None):
    """发一次请求。

    `login` 是 cookies.for_url() 给的登录态，含 cookie / ua / referer。
    **UA 必须和 cookie 配套** —— 知乎的 `__zse_ck` 与 UA 绑定，
    拿浏览器的 cookie 配我们自己的 UA，反而会被判异常。
    """
    headers = dict(HEADERS_MOBILE if mode == "mobile" else HEADERS_DESKTOP)
    if login:
        if login.get("ua"):
            headers["User-Agent"] = login["ua"]
        if login.get("cookie"):
            headers["Cookie"] = login["cookie"]
        if login.get("referer"):
            headers["Referer"] = login["referer"]
        headers.update(NAV_HEADERS)
    kw = {"timeout": timeout, "headers": headers, "allow_redirects": True}
    if proxies:
        kw["proxies"] = {"http": proxies, "https": proxies}
    else:
        # 显式声明"这两个协议都不走代理"。只传 proxies=None 是不够的 ——
        # requests 会回头去读环境变量里的 HTTP_PROXY，把"直连"偷偷变成
        # "走代理"：目标一旦是回环/内网地址，代理只会回一个 502，
        # 于是"本机自己起的服务"反而抓不到。（实测踩过）
        kw["proxies"] = {"http": None, "https": None}
    return requests.get(url, **kw)


# 回环 / 内网地址：这些一律直连，代理对它们没有任何意义
_LOCAL_HOST_RE = re.compile(
    r"^(?:localhost|127\.|0\.0\.0\.0|\[?::1\]?|10\.|192\.168\.|169\.254\.|"
    r"172\.(?:1[6-9]|2\d|3[01])\.)", re.I)


def is_local_url(url: str) -> bool:
    """判断是不是"就在本机/局域网里"的地址 —— 这类目标不该过代理。"""
    m = re.match(r"^https?://([^/:?#]+)", url or "", re.I)
    if not m:
        return False
    host = m.group(1).strip("[]").lower()
    if host.endswith(".local") or "." not in host:
        return True                      # mDNS 名或裸机器名，都是内网
    return bool(_LOCAL_HOST_RE.match(host))


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def strip_leading_title(text: str, title: str) -> str:
    """正文开头常常把标题又抄了一遍，读出来会"念两遍标题"。

    小心：只能在这一行**基本就是标题**时才删。英文站常把标题、日期、正文首句
    挤进同一段（"How to Do Great Work July 2023 If you collected lists…"），
    那时整行删掉等于把开篇正文吃掉了。
    """
    if not title or not text:
        return text
    key = re.sub(r"[\s，。、：:！？!?\-|_—]+", "", title)
    if len(key) < 6:
        return text
    lines = text.split("\n")
    if not lines:
        return text
    # 比较时忽略行首的结构标记（"# 标题" 也要能认出来）
    head_raw = re.sub(r"^\s*(?:#{1,6}\s+|>\s+|[-*•·▪◦]\s+)", "", lines[0]).strip()
    first = re.sub(r"[\s，。、：:！？!?\-|_—]+", "", head_raw)
    if not first:
        return text
    if len(first) > len(key) + 24:
        return text                      # 这行里还有别的内容，不是单纯标题
    if key.startswith(first) or first.startswith(key):
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
            paras, drop_n = drop_junk(paras)
        else:
            # 先按"行"清一遍，再重排 —— 顺序不能反（见 drop_junk_lines 的说明）
            lines, drop_n = drop_junk_lines(html_to_lines(strip_noise(frag)))
            paras, d2 = drop_junk(reflow(lines))
            drop_n += d2
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


# --------------------------------------------------------------------------
# 登录态与浏览器兜底
#
# 有些站（知乎最典型）没登录态就是不给内容：
#     裸请求                     → HTTP 403（一个 zse 挑战页）
#     带浏览器 cookie（未登录）  → HTTP 200，但还是软 404
#     带浏览器 cookie（已登录）  → 正常全文
# 所以抓取前先问一句"这个站配了登录态吗"，失败后再考虑两件事：
# 一是从浏览器刷新一次登录态（cookie 会过期），二是干脆让浏览器自己上。
# --------------------------------------------------------------------------

_REFRESH_AT = {"t": 0.0, "result": None}
_PROBE_AT = {"t": 0.0, "found": None}


def _login_for(url: str) -> Optional[dict]:
    """这个 URL 有没有配好的登录态。只读一个小 json，开销可以忽略。"""
    try:
        import cookies
        return cookies.for_url(url)
    except Exception:
        return None


def _site_known(url: str) -> bool:
    try:
        import cookies
        return bool(cookies.base_domain(cookies.host_of(url)))
    except Exception:
        return False


def _refresh_login(force: bool = False, min_gap: float = 60.0,
                   warmup=None) -> Optional[dict]:
    """从浏览器重新拉一次登录态。

    `__zse_ck` 这类风控 cookie 寿命不长，撞上 403 时主动刷新一次比让
    用户手动点更省事。加节流是因为连抓多篇时不该每次都去连浏览器。

    想拿结果就传 force=True —— 节流挡住的只是"这次去不去拉"，
    不改变语义。

    `warmup` 是一组地址：会让浏览器先去访问一次，把风控 cookie 刷新到
    最新。实测不做这一步的话，搬过来的常常是已经失效的凉 cookie。
    """
    if not force:
        if time.time() - _REFRESH_AT["t"] < min_gap:
            return None
    _REFRESH_AT["t"] = time.time()
    try:
        import cookies
        r = cookies.import_from_cdp(warmup=warmup)
        _REFRESH_AT["result"] = r
        return r
    except Exception:
        return None


def _browser_available() -> Optional[dict]:
    """本机有没有开着调试端口的浏览器。结果缓存 30 秒 —— 扫端口虽快，
    但没必要每抓一篇都扫一遍。"""
    if time.time() - _PROBE_AT["t"] < 30:
        return _PROBE_AT["found"]
    _PROBE_AT["t"] = time.time()
    try:
        import cdp
        _PROBE_AT["found"] = cdp.pick_browser()
    except Exception:
        _PROBE_AT["found"] = None
    return _PROBE_AT["found"]


def _fetch_via_browser(url: str, timeout: float = 40.0) -> Optional[dict]:
    """最后一招：让本机浏览器自己去打开页面，把渲染后的 DOM 交回来。

    浏览器带着登录态、真 UA，自己过 JS 挑战，所以我们只需要从 DOM 里
    捞内容。**带 HTML 回来而不是纯文本**：innerText 会丢掉 h1 / li /
    blockquote，读出来就是平的；带 HTML 回来能走同一条清理管线。
    """
    info = _browser_available()
    if not info:
        return None
    try:
        import cdp
        page = cdp.fetch_text(info["port"], url,
                              ws_url=info.get("webSocketDebuggerUrl"),
                              wait=min(12.0, max(5.0, timeout * 0.4)),
                              timeout=timeout)
    except Exception:
        return None
    if not page.get("ok"):
        return None

    html = page.get("html") or ""
    if html:
        title, text, route, dropped = to_reading_text(html, url)
        route = f"浏览器代抓·{route}"
    else:
        title = page.get("title") or ""
        text = page.get("text") or ""
        route, dropped = "浏览器代抓", 0
    if not title:
        title = page.get("title") or ""
    sc = score(text, title)
    return {
        "ok": sc["chars"] >= 120,
        "title": title, "text": text, "url": page.get("url") or url,
        "chars": sc["chars"], "paras": sc["paras"], "avg_para": sc["avg_para"],
        "quality": sc["score"], "grade": classify(sc["score"]), "route": route,
        "dropped": dropped, "html_bytes": len(html), "blocked": None,
        "mode": "browser", "proxied": False, "browser": True,
        "browser_port": info["port"], "waited": page.get("waited"),
    }


def _good(cand: Optional[dict]) -> bool:
    return bool(cand and cand.get("ok") and (cand.get("quality") or 0) >= 55)


def _run_attempts(url: str, mode: str, timeout: float, proxy: str,
                  login: Optional[dict], diag: dict,
                  best: Optional[dict] = None,
                  quick: bool = False) -> tuple[Optional[dict], str]:
    """按「代理 × 身份」的组合跑一轮，返回(目前最好的候选, 最后的错误)。

    `quick=True` 只打最可能命中的那一次。这不是抠性能，是**必须的**：
    知乎这类站对频率极敏感 —— 实测同一篇文章连打 8 次
    （两轮 × 桌面/手机 × 代理/直连）之后整站返回 403，隔一会儿单发才恢复。
    所以一旦有了登录态（身份已由 cookie 锁定），换 UA 换代理地猛打
    只会把自己打进风控名单。
    """
    modes = ["mobile"] if mode == "mobile" else ["desktop"]
    if mode == "auto":
        modes = ["desktop", "mobile"]
    if quick:
        modes = modes[:1]

    # 本机 / 局域网地址永远直连：代理帮不上忙，只会把它挡成 502
    if is_local_url(url):
        proxy_list: list[Optional[str]] = [None]
        diag["local_url"] = True
    else:
        proxy_list = _proxy_candidates(proxy)
    if quick:
        proxy_list = proxy_list[:1]

    last_err = ""
    for px in proxy_list:
        for md in modes:
            tag = f"{md}{'+proxy' if px else ''}"
            if login:
                tag += "+登录态"
            try:
                r = _get(url, md, timeout, px, login)
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
                    "blocked": blocked, "mode": md,
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
    return best, last_err


def fetch(url: str, mode: str = "auto", timeout: float = 25.0,
          proxy: str = "auto", login: bool = True,
          allow_browser: bool = True) -> dict:
    """抓一个链接并返回可直接朗读的正文 + 诊断信息。

    过程分四级，越往后越慢，所以能早停就早停：

        1. 直接请求（带上已配好的登录态）
        2. 抓得不好 → 从浏览器刷新登录态重试一次（cookie 会过期）
        3. 还是不行 → 让浏览器自己去打开页面，把渲染后的 DOM 交回来
        4. 都不行 → 如实报错，并说清是哪一关卡住了

    返回：
        ok     是否拿到了可用正文
        title  标题
        text   清洗后的正文（段落已重排）
        diag   诊断：字数/段落数/评分/路线/拦截原因/试过的策略
    """
    diag: dict = {"tried": []}
    if not re.match(r"^https?://", url or "", re.I):
        return {"ok": False, "error": "不是有效的 http/https 链接", "diag": diag}

    jar = _login_for(url) if login else None
    if jar:
        diag["login"] = {"domain": jar["domain"], "source": jar["source"],
                         "age": jar["age"], "cookies": len(jar["jar"])}

    # 已经配好登录态、或者这个站明摆着要登录态的，第一轮只打一次。
    # 理由见 _run_attempts 的注释：换着身份猛打会触发风控，
    # 而且带登录态时身份已经由 cookie 锁定了，换 UA 反而有害。
    quick = bool(jar) or _site_known(url)
    best, last_err = _run_attempts(url, mode, timeout, proxy, jar, diag,
                                   quick=quick)

    # 第二级：抓不到 / 抓得差，而这个站又是需要登录态的站点 → 刷新后重试
    if login and not _good(best) and _site_known(url):
        before = jar["header"] if jar else ""
        warm = []
        try:
            import cookies
            warm = cookies.warmup_urls_for(url)
        except Exception:
            pass
        # 让浏览器去访问一次会把自己的频率限制也重置掉一部分，中间留一点
        # 间隔，别把"刚被拒的请求"换个 cookie 立刻再打一遍。
        time.sleep(1.0)
        refreshed = _refresh_login(warmup=warm)
        jar2 = _login_for(url)
        warmed_ok = bool(((refreshed or {}).get("warmup") or {}).get("ok"))
        # cookie 值没变也值得重试 —— 浏览器"真的访问过一次"这件事本身
        # 就会让服务端侧的会话状态刷新（实测预热前后 cookie 一模一样，
        # 但请求从 403 变成 200）。
        if jar2 and (jar2["header"] != before or warmed_ok):
            diag["login_refreshed"] = True
            diag["tried"].append({"mode": "刷新登录态后重试"})
            best, err2 = _run_attempts(url, mode, timeout, proxy, jar2, diag,
                                       best, quick=True)
            last_err = err2 or last_err

    # 第三级：让浏览器自己上。到这一步 requests 已经没辙了。
    if allow_browser and not _good(best) and not is_local_url(url):
        b3 = _fetch_via_browser(url, timeout=max(timeout, 35.0))
        if b3 and (_good(b3) or not _good(best)):
            diag["tried"].append({"mode": "浏览器代抓", "chars": b3.get("chars")})
            best = b3

    if best is None:
        # 连页面都没拿到（比如一直是 403）。把"该配登录态"直接说出来，
        # 而不是让用户对着"稍后重试"反复撞墙。
        hint = ""
        site = _site_meta(url)
        if site:
            if jar:
                hint = (f"。{site['name']} 的登录态被拒了（{last_err}），"
                        f"多半是 cookie 过期 —— 点「登录态」重新导入一次即可")
            else:
                hint = (f"。{site['name']} 需要登录态才能读：点「登录态」→"
                        f"「从浏览器导入」，或在浏览器里复制 Cookie 粘进来")
        diag["picked"] = None
        return {"ok": False, "diag": diag, "need_login": bool(site),
                "error": f"抓取失败：{last_err or '未知原因'}{hint}"}

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
        site = _site_meta(url)
        if site and not jar:
            extra = (f"。这个站需要登录态：点「登录态」里的「从浏览器导入」，"
                     f"或在浏览器里复制 Cookie 粘进来"
                     f"（{site['name']} 要 {'、'.join(site.get('关键 cookie') or [])}）")
        elif site and not _looks_logged_in(jar):
            extra = (f"。已配的 {site['name']} 登录态里没有登录凭证，"
                     f"可能只导入了匿名 cookie，重新导入一次试试")
        else:
            extra = "。稍等片刻重试，或用浏览器打开后复制正文。"
        best["error"] = f"没能提取到正文：{reason}{extra}"
        best["need_login"] = bool(site and not _good(best))
    diag["picked"] = {"mode": best.get("mode"), "route": best.get("route"),
                      "quality": best.get("quality"), "chars": best.get("chars")}
    best["diag"] = diag
    return best


def _site_meta(url: str) -> Optional[dict]:
    try:
        import cookies
        return cookies.site_info_for(url)
    except Exception:
        return None


def _looks_logged_in(jar: Optional[dict]) -> bool:
    if not jar:
        return False
    try:
        import cookies
        return cookies._looks_logged_in(jar["domain"], jar["jar"])
    except Exception:
        # 判断不了就当"有"，免得让用户白折腾
        return bool(jar.get("jar"))


if __name__ == "__main__":  # 手动调试：python webfetch.py <url>
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://sspai.com/post/86000"
    d = fetch(u)
    print(json.dumps({k: v for k, v in d.items() if k != "text"},
                     ensure_ascii=False, indent=2))
    print("--- 正文 ---")
    print(d.get("text", "")[:1500])
