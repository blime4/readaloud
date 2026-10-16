# -*- coding: utf-8 -*-
"""ReadAloud 文本智能层

把「一段文字」变成「一份朗读稿」，这一层负责四件事：

1. 结构解析（parse_blocks）
   先认出标题 / 列表 / 引用 / 代码 / 表格 / 段落，再决定怎么读。
   旧版的毛病是先无脑删标记再猜结构，于是标题和正文糊成一团。
   朗读时标题该慢、该停，列表项该一条条念 —— 这是"像人念"的基础。

2. 逐句切分（split_items）
   真·逐句：默认一段就切一句（约 60 字内），而不是凑满 160 字才切一刀。
   旧版的分句实际上是「攒够 160 字再切」，所以逐句高亮其实在逐段高亮。
   切句时会保护引号/括号内部，不会把「他说：“…。”」拦腰截断。

3. 读音规整（normalize）
   跟 AI 解耦：不开 AI 也能把 2026-10-14 / 62% / ¥1,280 / HTTP/2 / 3.5–4.2
   读顺。纯规则、零延迟、离线可用，且结果可预期。
   用户还能用 pronounce.json 自定义发音（K8s → Kubernetes 之类）。

4. 音色推荐（recommend_voice）
   按内容挑音色：中英夹读用多语种音色、技术文档用沉稳播报音、故事用叙事音、
   粤语内容用粤语音色。旧版把 66 个音色直接丢给用户自己猜。
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
PRON_PATH = BASE / "pronounce.json"

# ---------------------------------------------------------------- 常量

SENT_END = "。！？!?；;…"          # 句末标点（中文半角混排都算）
SENT_END_FULL = SENT_END + "."     # 含英文句号：判断"这句话说完了"用这个
SOFT_BREAK = "，,、：:—"          # 句内可断处（超长句才用）

PAUSE = {                          # 每种边界之后停多久（毫秒）
    "sentence": 330,
    "soft": 170,
    "list": 400,
    "heading": 760,
    "quote": 540,
    "paragraph": 660,
}

RATE_DELTA = {                     # 每种块的语速微调（%），让念稿有语气层次
    "heading": -8,
    "quote": -6,
    "list": 2,
    "paragraph": 0,
}

OPEN_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』",
              "（": "）", "《": "》", "【": "】", "(": ")", "[": "]", "{": "}"}
CLOSERS = set(OPEN_PAIRS.values()) | {'"'}

# 解析阶段用到的标记
RE_FENCE = re.compile(r"^\s*(```|~~~)")
RE_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
RE_HR = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$")
RE_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
RE_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
RE_TABLE = re.compile(r"^\s*\|.*\|\s*$")
RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\s*\n", re.S)

# 行内标记
RE_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
RE_WIKI_ALIAS = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
RE_WIKI = re.compile(r"\[\[([^\]]*)\]\]")
RE_HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]{0,80}>")
RE_FOOTNOTE = re.compile(r"\[\^[^\]]*\]|\^\[[^\]]*\]")
RE_CITE = re.compile(r"\[\d{1,3}(?:[,\-–]\s?\d{1,3})*\]")
RE_HIGHLIGHT = re.compile(r"==([^=]+)==")
RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
RE_UNDER = re.compile(r"__([^_]+)__")
RE_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
RE_STRIKE = re.compile(r"~~([^~]+)~~")
RE_CODE_INLINE = re.compile(r"`([^`]*)`")
RE_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200d]")
RE_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
RE_MULTI_BLANK = re.compile(r"\n{3,}")

# 读音规整用的正则
RE_URL = re.compile(r"https?://[^\s<>\"'）)】]+")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
RE_DATE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
RE_DATE_CN = re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日")
RE_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)")
RE_RANGE = re.compile(r"(?<=[\d.])\s*[–—~～]+\s*(?=[\d.])|(?<=\d)\s*-\s*(?=[\d.])")
RE_THOUSAND = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
RE_MONEY = [("US$", "美元"), ("USD", "美元"), ("￥", "元"), ("¥", "元"), ("$", "美元"),
            ("€", "欧元"), ("£", "英镑")]
RE_CURRENCY_TAIL = re.compile(r"(\d+(?:\.\d+)?)\s*(美元|元|欧元|英镑|人民币)")
RE_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
RE_UNITS = [("㎡", "平方米"), ("m²", "平方米"), ("m³", "立方米"), ("℃", "摄氏度"),
            ("°C", "摄氏度"), ("\bkg\b", "千克"), ("\bkm\b", "公里"), ("\bcm\b", "厘米"),
            ("\bmm\b", "毫米"), ("\bml\b", "毫升"), ("\bms\b", "毫秒")]
RE_VERSION = re.compile(r"(?<![\w.])[vV](\d+(?:\.\d+){1,3})(?![\w.])")
RE_SLASH_ALPHA = re.compile(r"(?<=[A-Za-z])/(?=[A-Za-z])")
RE_SLASH_MIX = re.compile(r"(?<=[A-Za-z])/(?=\d)|(?<=\d)/(?=[A-Za-z])")
RE_SYMBOLS = [("→", "到"), ("->", "到"), ("×", "乘"), ("≈", "约等于"),
              ("≥", "大于等于"), ("≤", "小于等于"), ("≠", "不等于"), ("±", "正负"),
              ("&", "和"), ("@", "艾特"), ("°", "度")]
RE_HASH_NUM = re.compile(r"#\s*(\d+)")
RE_CN_PAREN = re.compile(r"[（(]\s*([^（()）]*?)\s*[)）]")
RE_PLUS_SIGN = re.compile(r"(?<![\w\d.])\+\s*(?=[\d０-９]|百分之)")
# 只在「汉字 ↔ 拉丁字母」之间插空格（数字前后不插，否则 2026年 会被拆成 2026 年）
RE_CJK_LATIN = re.compile(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z])")
RE_LATIN_CJK = re.compile(r"(?<=[A-Za-z])(?=[\u4e00-\u9fff])")
RE_DUP_PUNCT = re.compile(r"([，。！？；：、])\1+")
RE_SPACE_BEFORE_PUNCT = re.compile(r"\s+([，。！？；：、）】」』])")
RE_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")

DEFAULT_PRON = {
    "K8s": "Kubernetes",
    "k8s": "Kubernetes",
    "C++": "C 加加",
    "C#": "C 井",
    ".NET": "道耐特",
    "iOS": "iOS",
}

_pron_cache = {"stamp": None, "data": {}}


# ---------------------------------------------------------------- 发音词典

def load_pron() -> dict:
    """用户词典（pronounce.json）。改了文件立刻生效，不用重启。"""
    try:
        stamp = PRON_PATH.stat().st_mtime if PRON_PATH.exists() else None
    except OSError:
        stamp = None
    if _pron_cache["stamp"] == stamp and _pron_cache["data"]:
        return _pron_cache["data"]
    data = dict(DEFAULT_PRON)
    if stamp is not None:
        try:
            user = json.loads(PRON_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                data.update({str(k): str(v) for k, v in user.items()})
        except Exception:
            pass
    _pron_cache["stamp"], _pron_cache["data"] = stamp, data
    return data


def apply_pron(text: str) -> str:
    d = load_pron()
    if not d:
        return text
    pat = re.compile("|".join(re.escape(k) for k in sorted(d, key=len, reverse=True)))
    return pat.sub(lambda m: d[m.group(0)], text)


# ---------------------------------------------------------------- 1) 结构解析

def parse_blocks(raw: str) -> list:
    """把原始文本切成有类型的块：[{kind, text}]，kind ∈ heading/list/quote/para。"""
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    s = RE_FRONTMATTER.sub("", s)            # 去掉 YAML front matter
    s = RE_INVISIBLE.sub("", s)

    blocks, buf, kind, in_code = [], [], None, False

    def flush():
        nonlocal buf, kind
        if buf:
            blocks.append({"kind": kind or "para", "text": "\n".join(buf).strip()})
        buf, kind = [], None

    for line in s.split("\n"):
        if RE_FENCE.match(line):
            in_code = not in_code
            flush()
            continue
        if in_code:
            continue
        if RE_TABLE.match(line) or RE_HR.match(line):
            flush()
            continue
        if not line.strip():
            flush()
            continue

        m = RE_HEADING.match(line)
        if m:
            flush()
            blocks.append({"kind": "heading", "text": m.group(2).strip()})
            continue
        m = RE_QUOTE.match(line)
        if m:
            if kind != "quote":
                flush()
                kind = "quote"
            buf.append(m.group(1))
            continue
        m = RE_LIST.match(line)
        if m:
            flush()
            blocks.append({"kind": "list", "text": m.group(3).strip()})
            continue
        if kind not in (None, "para"):
            flush()
        kind = kind or "para"
        buf.append(line)
    flush()
    return [b for b in blocks if b["text"].strip()]


# ---------------------------------------------------------------- 行内清洗

def clean_inline(text: str) -> str:
    """只处理行内标记，不动结构（结构在上一步已经认出来了）。"""
    s = html.unescape(text)
    s = RE_IMG.sub(" ", s)
    s = RE_CODE_INLINE.sub(r"\1", s)
    s = RE_FOOTNOTE.sub("", s)
    s = RE_CITE.sub("", s)
    s = RE_HIGHLIGHT.sub(r"\1", s)
    s = RE_BOLD.sub(r"\1", s)
    s = RE_UNDER.sub(r"\1", s)
    s = RE_ITALIC.sub(r"\1", s)
    s = RE_STRIKE.sub(r"\1", s)
    s = RE_HTML_TAG.sub(" ", s)
    s = RE_WIKI_ALIAS.sub(r"\1", s)
    s = RE_WIKI.sub(r"\1", s)
    s = RE_LINK.sub(r"\1", s)
    s = RE_EMOJI.sub(" ", s)
    s = s.replace("**", "").replace("~~", "")
    # 删掉标记后常留下重复标点（「、^[脚注]、」-> 「、、」），收一下
    s = re.sub(r"([，。、；：,;])\s*\1+", r"\1", s)
    s = re.sub(r"([，、])\s*([，、])", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^[，、\s]+", "", s).strip()


def clean(raw: str) -> str:
    """兼容旧接口：纯文本（已去结构标记，未做读音规整）。"""
    parts = [clean_inline(b["text"]) for b in parse_blocks(raw)]
    return re.sub(RE_MULTI_BLANK, "\n\n", "\n\n".join(p for p in parts if p)).strip()


# ---------------------------------------------------------------- 2) 读音规整

def _num_cn_group(raw: str) -> str:
    """给货币数字加中文量级后缀：12.8 美元 -> 1280 万元 之类的换算不做，
    只做「万/亿」这种中国人习惯的单位补齐。"""
    try:
        val = float(raw)
    except ValueError:
        return raw
    if val >= 1e8:
        return f"{val / 1e8:g}亿"
    if val >= 1e4:
        return f"{val / 1e4:g}万"
    return raw


def normalize(text: str, enabled: bool = True) -> str:
    """把符号/数字/日期读法规整成"念出来自然"的样子。

    刻意保守：只处理"朗读必定出问题"的写法，且每条规则都可解释。
    真正的疑难杂症交给 pronounce.json 用户词典。
    """
    if not text:
        return ""
    if not enabled:
        return re.sub(RE_MULTI_SPACE, " ", text).strip()

    s = text

    # 0) URL / 邮箱：念出来是灾难，替换成可读词
    urls = []
    def _stash(m):
        urls.append(m.group(0))
        return "\x00U%d\x00" % (len(urls) - 1)
    s = RE_URL.sub(_stash, s)
    s = RE_EMAIL.sub(_stash, s)

    # 1) 全角数字/字母/常用符号 -> 半角
    table = {ord("０") + i: str(i) for i in range(10)}
    table.update({ord("Ａ") + i: chr(ord("A") + i) for i in range(26)})
    table.update({ord("ａ") + i: chr(ord("a") + i) for i in range(26)})
    table.update({ord("％"): "%", ord("＋"): "+", ord("－"): "-", ord("＝"): "=",
                  ord("＄"): "$", ord("＃"): "#", ord("＆"): "&", ord("／"): "/"})
    s = s.translate(table)

    # 2) 日期 / 时间
    s = RE_DATE.sub(lambda m: f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日", s)
    s = RE_TIME.sub(
        lambda m: f"{int(m.group(1))}点{m.group(2)}分" + (f"{m.group(3)}秒" if m.group(3) else ""), s)

    # 3) 数字区间：3.5–4.2 / 1280-1500 -> 「到」
    s = RE_RANGE.sub("到", s)

    # 4) 千分位去掉（否则可能被读成断句）
    for _ in range(3):
        s = RE_THOUSAND.sub("", s)

    # 5) 货币 / 百分比
    s = RE_PERCENT.sub(lambda m: f"百分之{m.group(1)}", s)
    s = re.sub(r"\$\s*(\d+(?:\.\d+)?)", r"\1美元", s)      # $1,280 -> 1280美元
    s = re.sub(r"[￥¥]\s*(\d+(?:\.\d+)?)", r"\1元", s)
    s = re.sub(r"€\s*(\d+(?:\.\d+)?)", r"\1欧元", s)
    s = re.sub(r"£\s*(\d+(?:\.\d+)?)", r"\1英镑", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*(美元|元|欧元|英镑|人民币)",
               lambda m: _num_cn_group(m.group(1)) + m.group(2), s)

    # 6) 单位 / 版本号
    for a, b in RE_UNITS:
        s = re.sub(a, b, s)
    s = RE_VERSION.sub(lambda m: "版本" + m.group(1), s)

    # 7) 符号
    for a, b in RE_SYMBOLS:
        s = s.replace(a, b)
    s = RE_HASH_NUM.sub(r"第\1", s)
    s = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", "加", s)     # 3 + 2 -> 3加2
    s = RE_PLUS_SIGN.sub("", s)                      # 「同比 +30%」的加号念不出意思，去掉
    s = s.replace("*", " ").replace("^", " ")

    # 8) 斜杠：AI/ML -> AI和ML ；HTTP/2 -> HTTP 2
    s = RE_SLASH_ALPHA.sub("和", s)
    s = RE_SLASH_MIX.sub(" ", s)

    # 9) 括号内容前后改成逗号，念起来有停顿（这是"像人念"的关键细节）
    s = RE_CN_PAREN.sub(lambda m: "，" + m.group(1) + "，", s)

    # 10) 中英之间插空格：让 TTS 正确切语言，避免「AI产品」糊成一坨
    s = RE_CJK_LATIN.sub(" ", s)
    s = RE_LATIN_CJK.sub(" ", s)

    # 11) 标点与空白整理
    s = RE_SPACE_BEFORE_PUNCT.sub(r"\1", s)
    s = RE_DUP_PUNCT.sub(r"\1", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"，\s*([。！？；])", r"\1", s)
    s = RE_MULTI_SPACE.sub(" ", s)

    # 12) 用户发音词典
    s = apply_pron(s)

    # 13) 还原 URL / 邮箱
    for i, u in enumerate(urls):
        s = s.replace("\x00U%d\x00" % i, "网页链接")
    s = re.sub(r"[，、]{2,}", "，", s)
    s = re.sub(r"^[，、\s]+", "", s)
    s = re.sub(r"\s+([。！？；，、])", r"\1", s)
    return s.strip()


# ---------------------------------------------------------------- 3) 逐句切分

def split_sentences(text: str) -> list:
    """切句，并保护引号/括号内部不被截断。"""
    out, buf, stack = [], "", []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        buf += ch
        if ch in OPEN_PAIRS:
            stack.append(OPEN_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif not stack and ch in SENT_END:
            j = i + 1
            while j < n and (text[j] in SENT_END or text[j] in CLOSERS or text[j] in "”’\"'"):
                buf += text[j]
                j += 1
            out.append(buf.strip())
            buf, i = "", j
            continue
        elif not stack and ch == ".":
            prev = buf[-2] if len(buf) >= 2 else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if prev.isdigit() and nxt.isdigit():
                pass                                    # 小数点
            elif re.search(r"\b(Mr|Mrs|Ms|Dr|St|vs|etc|e\.g|i\.e|No|Fig|Inc|Ltd|Jr|Sr|approx|cf|U\.S|a\.m|p\.m)\.$",
                           buf, re.I):
                pass                                    # 英文缩写
            elif nxt == "" or nxt.isspace() or nxt in CLOSERS or nxt in "\"'”’":
                out.append(buf.strip())
                buf = ""
        i += 1
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def _hard_split(s: str, limit: int) -> tuple:
    """超长句按软停顿处切开，返回 (片段列表, 是否发生过硬切)。"""
    if len(s) <= limit:
        return [s], False
    parts, buf, cut = [], "", False
    for token in re.split(r"(?<=[%s])" % SOFT_BREAK, s):
        if not token:
            continue
        if len(buf) + len(token) <= limit:
            buf += token
            continue
        if buf:
            parts.append(buf)
        while len(token) > limit:
            piece = token[:limit]
            idx = max((piece.rfind(m) for m in SOFT_BREAK), default=-1)
            if idx > limit * 0.5:
                piece = piece[: idx + 1]
            parts.append(piece)
            token = token[len(piece):]
            cut = True
        buf = token
    if buf:
        parts.append(buf)
    return parts, cut


def split_items(text: str, max_len: int = 62, min_len: int = 9,
                kinds: bool = True, blocks: list | None = None) -> list:
    """产出朗读条目：[{text, kind, pause, rate}]。

    跟旧实现的区别：默认每句一条（max_len 是"单句上限"而不是"攒够才切"），
    所以逐句高亮能真的跟着声音走，也才谈得上句间停顿。
    """
    items = []
    src = blocks if blocks is not None else [{"kind": "para", "text": text}]
    for bi, b in enumerate(src):
        kind = b["kind"] if kinds else "para"
        body = b["text"] if blocks is not None else text
        sents = split_sentences(body)
        expanded = []
        for s in sents:
            pieces, cut = _hard_split(s, max_len)
            for pi, p in enumerate(pieces):
                p = p.strip()
                if not p:
                    continue
                expanded.append((p, cut and pi < len(pieces) - 1))
        # 只合并"半截话"：即硬切出来的碎片，或者极短且不以句末标点收尾的碎块。
        # 完整句子（以 。！？ 收尾）永远独立成段 —— 这样逐句高亮才能对上声音。
        merged = []
        for txt, is_soft in expanded:
            prev = merged[-1][0] if merged else ""
            tail_closed = prev.endswith(tuple(SENT_END_FULL)) or prev.endswith(tuple(CLOSERS))
            if merged and not tail_closed and len(prev) + len(txt) <= max_len:
                merged[-1] = (prev + txt, merged[-1][1] or is_soft)
            else:
                merged.append((txt, is_soft))
        is_last_block = bi == len(src) - 1
        for i, (txt, is_soft) in enumerate(merged):
            last = i == len(merged) - 1
            if kind == "heading":
                pause = PAUSE["heading"]
            elif kind == "quote":
                pause = PAUSE["quote"]
            elif kind == "list":
                pause = PAUSE["list"] + (PAUSE["paragraph"] if last else 0)
            else:
                pause = (PAUSE["paragraph"] if last else
                         (PAUSE["soft"] if is_soft else PAUSE["sentence"]))
            if last and not is_last_block and kind == "paragraph":
                pause = max(pause, PAUSE["paragraph"])
            items.append({
                "text": txt,
                "kind": kind,
                "pause": int(pause),
                "rate": RATE_DELTA.get(kind, 0),
            })
    return items


# ---------------------------------------------------------------- 4) 音色推荐

RE_KANA = re.compile(r"[\u3040-\u30ff]")
RE_HANGUL = re.compile(r"[\uac00-\ud7af]")
RE_CJK = re.compile(r"[\u3400-\u9fff]")
RE_LATIN = re.compile(r"[A-Za-z]")
CANTONESE_HINTS = ("嘅", "唔", "係", "咁", "喺", "哋", "嘢", "睇", "冇", "咗", "嗰")
TECH_HINTS = ("API", "SDK", "HTTP", "JSON", "函数", "接口", "部署", "配置", "报错",
              "代码", "版本", "数据库", "服务器", "参数", "日志", "编译", "调试",
              "架构", "异步", "并发", "缓存", "算法", "依赖")
# 财经/商业：以前没有这一类，财报会被判成"通用内容"退回默认女声
FINANCE_HINTS = ("营收", "利润", "毛利", "净利", "同比", "环比", "增长", "下滑", "估值",
                 "市盈率", "股价", "基金", "投资", "收益率", "财报", "业绩", "成本",
                 "预算", "融资", "现金流", "资产", "负债", "市值", "份额", "营收额")
NEWS_HINTS = ("记者", "报道", "通讯", "据悉", "发布会", "声明", "通报",
              "发言人", "官方", "今日", "昨日", "上午", "下午")
ACADEMIC_HINTS = ("研究", "论文", "实验", "样本", "假设", "文献", "综述", "方法论",
                  "理论", "数据表明", "结果表明", "统计分析", "显著性")
# 注意：这里刻意不放"他/她"——它们太常见，会把任何叙事都误判成小说
STORY_HINTS = ("故事", "小说", "曾经", "那年", "回忆", "旅途", "传说",
               "很久以前", "小时候", "那一夜")
LIVELY_HINTS = ("哈哈", "开心", "太棒", "好玩", "冲鸭", "!", "！")

PREFERRED = {
    "zh-finance": ["zh-CN-YunyangNeural", "zh-CN-YunjianNeural", "zh-CN-XiaoxiaoNeural"],
    "zh-news": ["zh-CN-YunyangNeural", "zh-CN-XiaoxiaoNeural"],
    "zh-academic": ["zh-CN-YunxiNeural", "zh-CN-YunyangNeural", "zh-CN-XiaoxiaoNeural"],
    "zh-tech": ["zh-CN-YunyangNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural"],
    "zh-story": ["zh-CN-YunjianNeural", "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"],
    "zh-lively": ["zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural"],
    "zh-warm": ["zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural"],
    "zh-mixed": ["en-US-EmmaMultilingualNeural", "en-US-AvaMultilingualNeural",
                 "en-US-AndrewMultilingualNeural", "zh-CN-XiaoxiaoNeural"],
    "yue": ["zh-HK-WanLungNeural", "zh-HK-HiuGaaiNeural"],
    "ja": ["ja-JP-NanamiNeural", "ja-JP-KeitaNeural"],
    "ko": ["ko-KR-HyunsuMultilingualNeural"],
    "en": ["en-US-AndrewNeural", "en-US-AriaNeural", "en-US-EmmaNeural"],
}


def detect_lang(text: str) -> dict:
    t = text or ""
    total = max(1, len(re.sub(r"\s", "", t)))
    cjk = len(RE_CJK.findall(t))
    kana = len(RE_KANA.findall(t))
    han = len(RE_HANGUL.findall(t))
    latin = len(RE_LATIN.findall(t))
    if kana >= 5 and kana > cjk * 0.3:
        primary = "ja"
    elif han >= 5 and han > cjk * 0.3:
        primary = "ko"
    elif latin > cjk * 2 and latin >= 8:
        primary = "en"
    else:
        primary = "zh"
    return {"primary": primary, "cjk": round(cjk / total, 2),
            "latin": round(latin / total, 2),
            "mixed": primary == "zh" and latin >= 12}


def recommend_voice(text: str, available: list | None = None) -> dict:
    """按内容推荐音色，返回 {voice, reason, lang}。"""
    avail = set(available or [])
    def pick(cands):
        for c in cands:
            if not avail or c in avail:
                return c
        return cands[0]

    lang = detect_lang(text)
    t = text or ""
    if lang["primary"] == "ja":
        return {"voice": pick(PREFERRED["ja"]), "reason": "检测到日文内容，用日语音色", "lang": lang}
    if lang["primary"] == "ko":
        return {"voice": pick(PREFERRED["ko"]), "reason": "检测到韩文内容，用韩语音色", "lang": lang}
    if lang["primary"] == "en":
        return {"voice": pick(PREFERRED["en"]), "reason": "英文内容为主，用英文音色", "lang": lang}
    if sum(t.count(w) for w in CANTONESE_HINTS) >= 3:
        return {"voice": pick(PREFERRED["yue"]), "reason": "检测到粤语用词，用粤语音色", "lang": lang}
    if lang["mixed"]:
        return {"voice": pick(PREFERRED["zh-mixed"]),
                "reason": "中英夹读较多，用多语种音色更自然", "lang": lang}
    # 下面按"内容类型"挑音色。顺序有意义：财经/技术的播报感要求最明确，先判。
    if sum(t.count(w) for w in FINANCE_HINTS) >= 3:
        return {"voice": pick(PREFERRED["zh-finance"]),
                "reason": "财经数据类内容，用沉稳的播报音", "lang": lang}
    if sum(t.count(w) for w in TECH_HINTS) >= 3:
        return {"voice": pick(PREFERRED["zh-tech"]),
                "reason": "技术类内容，用沉稳播报音", "lang": lang}
    if sum(t.count(w) for w in ACADEMIC_HINTS) >= 3:
        return {"voice": pick(PREFERRED["zh-academic"]),
                "reason": "学术论述类内容，用清晰平实的音色", "lang": lang}
    if sum(t.count(w) for w in NEWS_HINTS) >= 3:
        return {"voice": pick(PREFERRED["zh-news"]),
                "reason": "新闻播报类内容，用标准播报音", "lang": lang}
    # 叙事类阈值放宽到 2：下面这些词都是"叙事专属"标记，
    # 不像"数据/模型"那样随处可见，命中两个基本就确定是讲故事了。
    if sum(t.count(w) for w in STORY_HINTS) >= 2:
        return {"voice": pick(PREFERRED["zh-story"]),
                "reason": "叙事类内容，用讲述感音色", "lang": lang}
    liveliness = sum(t.count(w) for w in LIVELY_HINTS)
    if liveliness >= 4:
        return {"voice": pick(PREFERRED["zh-lively"]),
                "reason": "轻松活泼的内容，用明亮音色", "lang": lang}
    return {"voice": pick(PREFERRED["zh-warm"]),
            "reason": "通用内容，用温暖自然的女声", "lang": lang}


# ---------------------------------------------------------------- 一站式

def prepare(raw: str, max_len: int = 62, min_len: int = 9, do_normalize: bool = True) -> dict:
    """原文 -> {text, items, segments, blocks}。"""
    blocks = parse_blocks(raw)
    cooked = []
    for b in blocks:
        t = clean_inline(b["text"])
        if not t:
            continue
        t = normalize(t, do_normalize)
        # 括号注释会被读法规整改写成逗号分隔（"占比（Top10）68%）" -> "...，Top10..."），
        # 于是整段可能以逗号/顿号收尾。朗读时那是个"话没说完"的半截停顿，
        # 听起来很怪；补成句号，分句和停顿都干脆。
        t = re.sub(r"[，,、：:；;]+$", "。", t)
        if t and t != "。":
            cooked.append({"kind": b["kind"], "text": t})
    text = "\n\n".join(b["text"] for b in cooked).strip()
    items = split_items("", max_len=max_len, min_len=min_len, blocks=cooked) if cooked else []
    if not items and text:
        items = [{"text": text, "kind": "para", "pause": PAUSE["paragraph"], "rate": 0}]
    return {"text": text, "items": items,
            "segments": [it["text"] for it in items], "blocks": cooked}
