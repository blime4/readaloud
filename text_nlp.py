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
from collections import Counter
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

# PDF / EPUB 抽出来的正文经常在书名号、引号中间留着换行空格：
# 「《大 雪纷飞的日子》」「他说“ 我不去”」。这种空格会被切句逻辑当成断点，
# 把《大|雪纷飞的日子》劈成两条。因此只在**成对符号内部**收掉它 ——
# 全局删"汉字 空格 汉字"会误伤正文里本来就有的空格（「释义 喉结」「空 格」）。
RE_CJK_SPACE = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f])")

# 成对符号：切超长句时绝不能从中间下刀（《大雪纷飞的日子》→《大 / 雪纷飞的日子》）
RE_PAIRED = re.compile(
    r"《[^》]{0,60}》|〈[^〉]{0,60}〉|「[^」]{0,80}」|『[^』]{0,80}』"
    r"|“[^”]{0,140}”|‘[^’]{0,80}’|（[^）]{0,80}）|\([^)]{0,80}\)|【[^】]{0,80}】")


def tighten_pairs(s: str) -> str:
    """把成对符号内部被换行/排版塞进来的空格收掉（只动中文语境里的空格）。"""
    if not any(b in s for b in "《〈「『“‘（(【"):
        return s
    return RE_PAIRED.sub(lambda m: RE_CJK_SPACE.sub("", m.group(0)), s)

# 真正能"念出来"的字符：汉字、字母、数字、日文假名、韩文。
# 纯标点（单独的 "."、"…"、"——"）和分隔符（"***"）没有音，送去合成只会拿到
# 「模型没有产出音频」，播放器就卡在那一句上再也不往前走。
RE_SPEAKABLE = re.compile(r"[0-9A-Za-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def speakable(text: str) -> bool:
    """这段文本有没有能念出来的内容（纯标点 / 纯符号返回 False）。"""
    return bool(RE_SPEAKABLE.search(text or ""))

# 解析阶段用到的标记
RE_FENCE = re.compile(r"^\s*(```|~~~)")
RE_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
# 章节标题（小说最常见）：「第一章 雪夜」「卷二」「Chapter 3」这类独立成行的行。
# 整行匹配且标题部分限长，避免把正文里恰好出现的"序章"二字误伤成标题。
RE_CHAPTER = re.compile(
    r"^\s*#{0,6}\s*"
    r"(?:第[0-9〇零一二三四五六七八九十百千万两]+\s*[章回卷节部篇话幕]"
    r"|Chapter\s+[0-9]+|CHAPTER\s+[0-9]+|Chapter\s+[IVXLivxl]+"
    r"|Part\s+(?:[0-9]+|[IVXivx]+)|PART\s+(?:[0-9]+|[IVXIVX]+)"
    r"|Appendix\s+[A-Z0-9]+|Section\s+[0-9]+(?:\.[0-9]+)*"
    r"|Prologue|Epilogue|Preface|Foreword"
    # 英文技术书的编号节："1. Introduction"、"2.3 Concurrency"。
    # 限制两位数字 + 大写开头 + 行内无句末标点，正文折行、有序列表外的句子
    # 都不会凑巧长这样。
    r"|[0-9]{1,2}(?:\.[0-9]{1,2}){0,2}\s+[A-Z][^。！？.!?\n]{2,60}\s*$"
    r"|序章|楔子|引子|尾声|终章|后记|番外[篇集]?)"
    r"[\s：:·、．.\-—]*[^。！？\n]{0,48}\s*$")
RE_HR = re.compile(r"^\s{0,3}([-*_])\s*(\1\s*){2,}$")
RE_LIST = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
RE_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
RE_TABLE = re.compile(r"^\s*\|.*\|\s*$")
RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\s*\n", re.S)
RE_GITBOOK_HINT = re.compile(
    r"{%\s*(?:hint(?:\s+style\s*=\s*(?:\"[^\"]*\"|'[^']*'))?|endhint)\s*%}", re.I)
RE_GITBOOK_HINT_TAIL = re.compile(
    r"(?<!\S){%\s*endhint\s*%}[*_]{1,2}(?=[ \t]*&(?:#x0*20|#0*32|nbsp);\s*$)", re.I)
# 只跳过独占一行的「/读音/ · 词性」，正文里的音标、斜杠和文件路径照常处理。
_DICT_PRON = r"/[^/\n]+/(?:\s*[（(][^）)\n]+[）)])?"
_DICT_POS = r"(?:n|v|adj|adv|interj|prep|pron|conj|det|num|phr)\.?"
RE_DICT_PRON = re.compile(
    rf"\s*(?P<emphasis>\*{{1,2}}|_{{1,2}}|){_DICT_PRON}"
    rf"(?:\s*[;；]\s*{_DICT_PRON})*\s*·\s*{_DICT_POS}"
    rf"(?:\s*[&,/]\s*{_DICT_POS})*\s*(?P=emphasis)\s*", re.I)

# 行内标记
RE_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
RE_WIKI_ALIAS = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
RE_WIKI = re.compile(r"\[\[([^\]]*)\]\]")
RE_HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]{0,80}>")
# 技术文本里 <int> / <vector> / <T> 是模板参数，不是 HTML 标签。
# 通用 RE_HTML_TAG 会把它们连内容一起删掉（max<int> 只剩 max），
# 所以 tech 模式只认一份明确的 HTML 标签白名单。
RE_HTML_TAG_STRICT = re.compile(
    r"</?(?:br|p|div|span|code|pre|em|strong|a|img|ul|ol|li|table|thead|tbody|tr|td|th|"
    r"h[1-6]|blockquote|hr|sup|sub|small|b|i|u|del|mark)\b[^<>]{0,80}>", re.I)
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
RE_UNCERTAIN_YEARS = re.compile(
    r"(?<![\w.])(\d{2,4})[?？]\s*[–—-]\s*(\d{2,4})([?？])?(?:\s*(B\.\s*C\.))?"
    r"(?![\w?？]|\.\d)", re.I)
RE_TEXT_AFTER_YEARS = re.compile(r"\s+\w")
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
# 技术文本专用符号表：刻意不把 & / -> 翻成「和 / 到」——T& 是引用、-> 是退化/指向，
# 全局替换会把讲 decay 的一整段念反。这些符号由 tech_inline() 按语境先转写好。
TECH_SYMBOLS = [("→", "变为"), ("->", "变为"), ("×", "乘"), ("≈", "约等于"),
                ("≥", "大于等于"), ("≤", "小于等于"), ("≠", "不等于"),
                ("±", "正负"), ("@", "艾特"), ("°", "度")]
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

# 技术文档里的「念不出来」或一定念错的缩写，只在 Markdown 模式叠加。
# 英文术语（lambda / template / concepts…）刻意保留：技术读者习惯听原词，
# TTS 的中英混排也能吃下；真正需要改的只有符号和缩写。
TECH_PRON = {
    "e.g.": "例如", "i.e.": "也就是", "etc.": "等等", "vs.": "对比",
    "cf.": "参见", "aka": "也就是", "std::": "标准库 ", "nullptr": "空指针",
    "constexpr": "const expr", "SFINAE": "S F I N A E", "RAII": "R A I I",
    "CRTP": "C R T P", "CLI": "C L I", "GUI": "G U I", "AST": "A S T",
    "sizeof": "size of", "decltype": "decl type", "typedef": "type def",
    "w/": "with ", "w/o": "without ", "C++20": "C 加加 20",
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


_pron_pat_cache: dict = {}          # {(词典对象 id, tech, latin): 已编译的替换 pattern}


def apply_pron(text: str, tech: bool = False, latin: bool = False) -> str:
    base = load_pron()
    if tech:
        d = dict(base)
        d.update(TECH_PRON)
    elif latin:
        # 英文书里别套中文发音（C++ -> "C 加加"、.NET -> "道耐特"），
        # K8s -> Kubernetes 这类通用转写和用户自定义词典照常生效。
        d = {k: v for k, v in base.items()
             if not (k in DEFAULT_PRON and re.search(r"[\u3400-\u9fff]", v))}
    else:
        d = base
    if not d:
        return text
    # pattern 按（词典身份, 模式）缓存：load_pron 按 mtime 返回同一对象，
    # 于是整本书逐块 prepare 时不用每块都重新编译上百个键的大正则
    # （几千块就是几秒的纯浪费）；词典文件一改，身份变了自然重编。
    key = (id(base), tech, latin)
    pat = _pron_pat_cache.get(key)
    if pat is None:
        pat = re.compile("|".join(re.escape(k) for k in sorted(d, key=len, reverse=True)))
        if len(_pron_pat_cache) > 4:
            _pron_pat_cache.clear()
        _pron_pat_cache[key] = pat
    return pat.sub(lambda m: d[m.group(0)], text)


# ---------------------------------------------------------------- 1) 结构解析

RE_LINE_PARA_END = re.compile(r"[。！？…”’』」）).!?\"']\s*$")


def _looks_like_line_paragraphs(s: str) -> bool:
    """判断是不是"一行一段"的纯文本。

    中文小说 TXT 常见排版：一行一段，段落之间没有空行。反过来，固定宽度硬折行的
    文本每 40 字就换行，行尾基本不会落在句末标点上 —— 用它来区分，不会误伤。
    """
    lines = [l.strip() for l in s.split("\n") if l.strip()]
    if len(lines) < 8:
        return False
    if s.count("\n\n") >= max(3, int(len(lines) * 0.1)):
        return False                      # 本来就有空行分段，别再插一脚
    hit = sum(1 for l in lines if RE_LINE_PARA_END.search(l))
    return hit / len(lines) >= 0.75


# ------------------------------------------------------------ 篇章名（目录）
# 中文小说、短篇集的篇名常常只是**单独一行的短词**，没有任何「第X章」标记：
#     目录：为了大家的安全/ 1        正文：为了大家的安全
# 只认「第X章」的话，整本书一个章节都认不出来（《烧纸》就是全书 0 章），
# 于是前端的☰目录按钮压根不出现 —— 用户看到的是"目录功能没做出来"。
# 这里补两层：① 先找目录块，把它列出的篇名当白名单；② 再用短行启发式兜底。

RE_TOC_PAIR = re.compile(
    r"^\s*(?P<t>.{1,32}?)\s*[/／]\s*(?P<p>[0-9ivxlⅰ-ⅻ]{1,6})\s*$", re.I)
RE_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_TITLE_BAD_TAIL = "。！？…”’」，、；：,;:."
_TITLE_BAD_HEAD = "“”‘’「」『』\"'《》（）()【】[]，。、；：！？·—-"
RE_TITLE_NOISE = re.compile(r"[0-9@/\\|<>{}\[\]=+~^*_（）()]")
RE_SENT_TAIL = re.compile(r"[。！？!?.;；…]$")


def _title_like(t: str, min_len: int = 2) -> bool:
    """一行短文本能不能当篇名/章名（保守判断，宁可漏也别把正文当标题）。"""
    t = (t or "").strip()
    if not (min_len <= len(t) <= 24):
        return False
    if t[0] in _TITLE_BAD_HEAD or t[-1] in _TITLE_BAD_TAIL:
        return False
    if RE_TITLE_NOISE.search(t):
        return False
    if any(ch in t for ch in "。！？…"):
        return False
    cjk = len(RE_CJK_CHAR.findall(t)) / len(t)
    if cjk >= 0.6:
        return True
    # 英文短行也能当篇名（Introduction / Preface）：
    # 首字母大写、行内没有句末标点、字母占比高。中文规则放宽到英文书，
    # 否则英文技术书的目录启发式一行都认不出来。
    letters = [c for c in t if c.isalpha()]
    return (len(t) >= 6 and letters
            and len(RE_LATIN_CHAR.findall(t)) / len(letters) >= 0.7
            and t[0].isupper() and not RE_SENT_TAIL.search(t))


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", "", (t or "").strip()).strip("·—-:： ")


def _find_titles(lines: list) -> tuple:
    """返回 (要当标题的行号集合, 要丢掉的目录行号集合)。

    目录行是「篇名/页码」或一串连续短行——留着会被朗读成"书名串烧"，别无用处。
    只有确认正文里也出现同名篇名时才丢，避免误删真正的内容。

    注意：这里只按"非空行"顺序看邻居。一行一段的文本会被补上大量空行，
    直接按原始行号找连续段落会永远断在空行上（第一版就栽在这儿）。
    """
    seq = [(i, l.strip()) for i, l in enumerate(lines) if l.strip()]
    m = len(seq)
    toc_titles, toc_rows = {}, set()
    toc_runs = []                             # 每一段"连续短标题"的 (篇名序列)

    i = 0
    while i < m:
        run, j, seen = [], i, set()
        while j < m:
            t = seq[j][1]
            mm = RE_TOC_PAIR.match(t)
            cand = mm.group("t").strip() if mm else t
            if cand and _title_like(cand, min_len=1):   # 「舞 / 151」这种单字篇名
                key = _norm_title(cand)
                if key in seen:
                    break                     # 一段目录里又出现同名 → 这里已经是正文开头了
                seen.add(key)
                run.append((seq[j][0], cand, bool(mm)))
                j += 1
                continue
            break
        if len(run) >= 4:                    # 连着 4 行以上短标题 → 认定是目录
            toc_runs.append([(t, pg) for _, t, pg in run])
            for row, title, _pg in run:
                toc_titles.setdefault(_norm_title(title), []).append(row)
                toc_rows.add(row)
            i = j
        else:
            i += 1

    title_idx, drop_idx = set(), set()
    for k, (row, t) in enumerate(seq):
        if row in toc_rows:
            continue
        key = _norm_title(t)
        if key in toc_titles and len(key) >= 2:
            title_idx.add(row)
            drop_idx.update(toc_titles[key])        # 正文出现了，目录那几行就可以不读

    # 兜底：整本书没有目录可依赖时，才用"独立短行 + 后面紧跟长正文"判定。
    # 目录已经给了篇名就不要再猜 —— 猜出来的多半是译者名、献词这类短行。
    for k, (row, t) in enumerate(seq):
        if title_idx:
            break
        if row in toc_rows or row in title_idx:
            continue
        if not _title_like(t):
            continue
        nxt = seq[k + 1][1] if k + 1 < m else ""
        prv = seq[k - 1][1] if k > 0 else ""
        if len(nxt) < 30:                    # 标题后面应当紧跟一段像样的正文
            continue
        if len(prv) > 20 and not RE_LINE_PARA_END.search(prv):
            continue                          # 上一行没说完，不是标题边界
        title_idx.add(row)

    # 有些篇名在正文里根本没被抽出来（PDF 转文字时整行丢了），
    # 但「（原载《…》）」这种篇末分隔是可靠的 —— 用它按目录顺序把缺的篇名补回去。
    synth = _fill_missing_titles(seq, title_idx, toc_runs)

    return title_idx, drop_idx, synth


RE_COPYRIGHT_END = re.compile(r"^[（(]?\s*原载[^）)]{0,60}[）)]?\s*$")


def _fill_missing_titles(seq: list, title_idx: set, toc_runs: list) -> dict:
    """用「（原载…）」分隔 + 目录顺序，补回正文里丢失的篇名。

    返回 {行号: 篇名}：在这些行之前插一个标题块（这一行本身还是正文，不能吃掉）。
    只在"目录里大部分篇名都能在正文对上号"时才动手，避免给别的书瞎编章节。
    """
    if not toc_runs:
        return {}
    # 挑条目最多、且带页码的那份目录（同一本书常有两份目录）
    order = [t for t, _ in max(toc_runs, key=lambda r: (sum(1 for _, pg in r if pg), len(r)))]
    if len(order) < 3:
        return {}

    found = {}
    for row in sorted(title_idx):
        txt = next((t for r, t in seq if r == row), "")
        found.setdefault(_norm_title(txt), row)
    hit = sum(1 for t in order if _norm_title(t) in found)
    if hit < 3 or hit / len(order) < 0.4:      # 对不上号，多半不是同一套标题
        return {}

    # 篇末标记「（原载《文艺中央》，1987年）」后面那一行，就是下一篇的开头
    starts = []
    for k, (row, t) in enumerate(seq):
        if RE_COPYRIGHT_END.match(t) and k + 1 < len(seq):
            starts.append(seq[k + 1][0])

    synth, bi = {}, 0
    last = -1
    for title in order:
        key = _norm_title(title)
        if key in found:
            last = max(last, found[key])
            continue
        nxt_row = min((r for r in found.values() if r > last), default=float("inf"))
        while bi < len(starts) and starts[bi] <= last:
            bi += 1
        if bi < len(starts) and starts[bi] < nxt_row and (not synth or starts[bi] > max(synth)):
            synth[starts[bi]] = title
            last = starts[bi]
            bi += 1
    return synth


# ---------------------------------------------------------------- PDF 文本修复
# PDF / EPUB 抽出来的英文正文有三类"非内容"痕迹，直接朗读全是噪音：
#   ① 单词被硬折行断成两半："typi-\ncally" 念出来是两个假词；
#   ② 段落被页宽切成一行行（行尾没有标点），句子里塞满换行；
#   ③ 每页一行的页码 / 页眉："42"、"A Tour of C++"，整本书重复几百遍。
RE_PAGE_NUM = re.compile(r"\d{1,4}")
RE_FLOW_END = re.compile(r"[A-Za-z,;:!?\"'\)\]]\s*$")
RE_FLOW_HEAD = re.compile(r"^\s*[A-Za-z(\"']")


def _is_struct_line(ln: str) -> bool:
    """这一行是不是结构行（标题 / 列表 / 引用 / 表格 / 分隔线 / 代码围栏）。"""
    return bool(RE_CHAPTER.match(ln) or RE_HEADING.match(ln) or RE_LIST.match(ln)
                or RE_QUOTE.match(ln) or RE_HR.match(ln) or RE_FENCE.match(ln)
                or RE_TABLE.match(ln))


def _strip_running_headers(s: str) -> str:
    """把每页都出现的那一两行页眉（书名 / 部名）替换成空行。

    判定条件刻意保守：书够长（≥300 非空行）且同一行（6~48 字符、不带
    句末标点）完全相同地出现 ≥6 次——正文里没有这样的行，页眉却全书都是。
    """
    nonempty = [l.strip() for l in s.split("\n") if l.strip()]
    if len(nonempty) < 300:
        return s
    bad = {t for t, c in Counter(nonempty).items()
           if c >= 6 and 6 <= len(t) <= 48 and not RE_SENT_TAIL.search(t)}
    if not bad:
        return s
    return "\n".join(l if l.strip() not in bad else "" for l in s.split("\n"))


def _latin_join_lines(s: str) -> str:
    """英文专用：拼回断词、把段内硬折行折叠成空格。

    空行（段落边界）和结构行（标题 / 列表 / 引用 / 表格）一律不动，
    于是既有段落停顿不丢，章节也还能被认出来。
    """
    if "\n" not in s:
        return s
    # ① 断词：行尾连字符 + 换行 + 小写续行 → 拼回一个词
    s = re.sub(r"([A-Za-z])-\n(?=[a-z])", r"\1", s)
    # ② 折行：上一行以流内字符收尾、下一行以字母/引号开头且不是结构行 → 合并
    out = []
    for ln in s.split("\n"):
        prev = out[-1] if out else ""
        if (out and ln.strip() and prev.strip()
                and not _is_struct_line(ln)
                and RE_FLOW_END.search(prev) and RE_FLOW_HEAD.match(ln)):
            out[-1] = prev.rstrip() + " " + ln.strip()
        else:
            out.append(ln)
    return "\n".join(out)


def parse_blocks(raw: str) -> list:
    """把原始文本切成有类型的块：[{kind, text}]，kind ∈ heading/list/quote/para。"""
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    s = RE_FRONTMATTER.sub("", s)            # 去掉 YAML front matter
    s = RE_INVISIBLE.sub("", s)
    s = _strip_running_headers(s)            # PDF 页眉（全书重复的短行）不读
    if _mostly_latin(s):
        s = _latin_join_lines(s)             # 英文：拼回断词、折叠段内折行
    if _looks_like_line_paragraphs(s):
        # 一行一段（大量中文小说 TXT 就是这样，全文一个空行都没有）：
        # 不补空行的话整本书会被拼成"一个段落"，段落停顿全丢，
        # 目录/版权页的短行也会被缝成一句长话。
        s = "\n\n".join(s.split("\n"))

    blocks, buf, kind, in_code = [], [], None, False
    all_lines = s.split("\n")
    title_idx, drop_idx, synth_titles = _find_titles(all_lines)

    def flush():
        nonlocal buf, kind
        if buf:
            blocks.append({"kind": kind or "para", "text": "\n".join(buf).strip()})
        buf, kind = [], None

    for ln_no, line in enumerate(all_lines):
        if RE_FENCE.match(line):
            in_code = not in_code
            flush()
            continue
        if in_code:
            continue
        line = RE_GITBOOK_HINT.sub("", RE_GITBOOK_HINT_TAIL.sub("", line))
        if RE_DICT_PRON.fullmatch(line):
            flush()
            continue
        if RE_TABLE.match(line) or RE_HR.match(line):
            flush()
            continue
        if not line.strip():
            flush()
            continue
        if RE_PAGE_NUM.fullmatch(line.strip()):
            flush()                           # 独立的页码行：念出来只有噪音
            continue
        if ln_no in drop_idx:                 # 目录行本身不读（正文里还有同名的篇章标题）
            flush()
            continue
        if ln_no in synth_titles:             # 正文丢了篇名，但篇末分隔把位置钉死了
            flush()
            blocks.append({"kind": "heading", "text": synth_titles[ln_no]})
        if ln_no in title_idx:
            flush()
            blocks.append({"kind": "heading", "text": line.strip()})
            continue

        m = RE_HEADING.match(line)
        if m:
            flush()
            blocks.append({"kind": "heading", "text": m.group(2).strip()})
            continue
        # 章节行（第X章 / Chapter N / 楔子…）：当标题块处理。
        # 前端据此生成目录，跳章就是跳到这个条目的下标。
        m = RE_CHAPTER.match(line)
        if m:
            flush()
            blocks.append({"kind": "heading", "text": line.strip()})
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

def clean_inline(text: str, tech: bool = False) -> str:
    """只处理行内标记，不动结构（结构在上一步已经认出来了）。

    tech=True 时只删明确的 HTML 标签，保留 <int> / <vector> 这类模板尖括号。
    """
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
    s = (RE_HTML_TAG_STRICT if tech else RE_HTML_TAG).sub(" ", s)
    s = RE_WIKI_ALIAS.sub(r"\1", s)
    s = RE_WIKI.sub(r"\1", s)
    s = RE_LINK.sub(r"\1", s)
    s = RE_EMOJI.sub(" ", s)
    s = s.replace("**", "").replace("~~", "")
    # 书名号 / 引号里被换行塞进来的空格：先收掉，后面才不会在这里断句
    s = tighten_pairs(s)
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


def normalize(text: str, enabled: bool = True, tech: bool = False,
              latin: bool | None = None) -> str:
    """把符号/数字/日期读法规整成"念出来自然"的样子。

    刻意保守：只处理"朗读必定出问题"的写法，且每条规则都可解释。
    真正的疑难杂症交给 pronounce.json 用户词典。

    tech=True 是 Markdown/技术笔记模式：跳过会毁掉 C++ 记号的几条全局替换
    （& -> 和、-> -> 到、* / ^ 删掉、括号改逗号、AI/ML -> AI和ML），
    这些已由 tech_inline() 按语境转写过；数字/日期/百分号/中英空格等
    通用规则照常复用。小说路径 tech=False，行为完全不变。

    latin=None 自动识别：纯英文段落走轻量英文模式，跳过所有中文专属的
    日期/货币/百分号/符号读法 —— 否则英文书会把 "R&D" 念成 "R和D"、
    "and/or" 念成 "and和or"、"62%" 念成 "百分之62"、(see Ch.7) 变成中文逗号。
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

    # 1.5) 纯英文：只做中英空格 / 空白规整 / 用户词典，不套中文读法。
    #      英文 TTS 自己会处理 62%、$1,280、2026-10-14、and/or、R&D，
    #      硬套中文规则反而把文本改成中英混排、念得四不像。
    if latin is None:
        latin = _mostly_latin(s) and not tech
    if latin:
        s = RE_CJK_LATIN.sub(" ", s)
        s = RE_LATIN_CJK.sub(" ", s)
        s = RE_MULTI_SPACE.sub(" ", s)
        s = re.sub(r"\s+([,.!?;:])", r"\1", s)
        s = apply_pron(s, latin=True)
        for i, _u in enumerate(urls):
            s = s.replace("\x00U%d\x00" % i, "web link")
        return s.strip()

    # 2) 日期 / 时间
    s = RE_DATE.sub(lambda m: f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日", s)
    s = RE_TIME.sub(
        lambda m: f"{int(m.group(1))}点{m.group(2)}分" + (f"{m.group(3)}秒" if m.group(3) else ""), s)

    # 3) 数字区间：3.5–4.2 / 1280-1500 -> 「到」
    # 生卒年内的问号表示约数，括号展开后也不能当作句末。
    def spoken_years(m):
        # 直接接下一句时，区间末尾的问号或 B.C. 的句点也可能是句末标点。
        terminal = m[0][-1] if RE_TEXT_AFTER_YEARS.match(s, m.end()) else ""
        terminal = terminal if terminal in ".?？" else ""
        approx_end = "约" if m[3] and terminal not in ("?", "？") else ""
        return f"{'公元前' if m[4] else ''}约{m[1]}到{approx_end}{m[2]}{terminal}"

    s = RE_UNCERTAIN_YEARS.sub(spoken_years, s)
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
    for a, b in (TECH_SYMBOLS if tech else RE_SYMBOLS):
        s = s.replace(a, b)
    s = RE_HASH_NUM.sub(r"第\1", s)
    s = re.sub(r"(?<=\d)\s*\+\s*(?=\d)", "加", s)     # 3 + 2 -> 3加2
    s = RE_PLUS_SIGN.sub("", s)                      # 「同比 +30%」的加号念不出意思，去掉
    if not tech:
        s = s.replace("*", " ").replace("^", " ")     # 技术文本的 * 是指针/解引用，^ 是异或，不能删

    # 8) 斜杠：AI/ML -> AI和ML ；HTTP/2 -> HTTP 2
    if not tech:
        s = RE_SLASH_ALPHA.sub("和", s)               # 技术文本的 and/or、std/boost 不能变「和」
    s = RE_SLASH_MIX.sub(" ", s)

    # 9) 括号内容前后改成逗号，念起来有停顿（这是"像人念"的关键细节）
    #    技术文本里 (T&&) / (std::function) 被拆成逗号会把类型念散，跳过。
    if not tech:
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

    # 12) 用户发音词典（技术模式再叠加缩写表）
    s = apply_pron(s, tech=tech)

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
    year_positions = {i for match in RE_UNCERTAIN_YEARS.finditer(text)
                      for i in range(match.start(), match.end() - bool(RE_TEXT_AFTER_YEARS.match(text, match.end())))}
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        buf += ch
        if ch in OPEN_PAIRS:
            stack.append(OPEN_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif not stack and ch in SENT_END and i not in year_positions:
            j = i + 1
            while j < n and (text[j] in SENT_END or text[j] in CLOSERS or text[j] in "”’\"'"):
                buf += text[j]
                j += 1
            out.append(buf.strip())
            buf, i = "", j
            continue
        elif not stack and ch == "." and i not in year_positions:
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


def _paired_spans(s: str) -> list:
    """成对符号的区间 [start, end)，用来避开"从中劈开"的切点。"""
    return [(m.start(), m.end()) for m in RE_PAIRED.finditer(s)]


def _cut_is_safe(pos: int, spans: list) -> bool:
    """pos 作为切点（前段 = s[:pos]）会不会把成对符号劈开。"""
    return not any(st < pos < en for st, en in spans)


# ---- 英文长句专用 ----------------------------------------------------------
# 62 个汉字约念 13 秒，62 个英文字母却只有 11 个词、约 4 秒。沿用同一个字符
# 上限，英文长句必然被剁成一串半截话（"…accessed through" / "a pointer…"）；
# 更糟的是旧版把每个空格都当等价断点、取 limit 前最后一个，于是断在冠词、
# 介词、助动词后面成了常态。下面两件事一起改：按词折算上限 + 给断点打分。

RE_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*")
RE_LATIN_CHAR = re.compile(r"[A-Za-z]")

# 不能结束一个呼吸组的词：断在它们后面，前半句听起来就是"话没说完"。
EN_DANGLING = frozenset("""
a an the this that these those his her its their our your my some any every each
it he she we you they there one all both either neither
no not very more most less least quite rather too also just even still only
and or but nor yet so as than if while when because although though unless until
of to in on at by for with from into through about between under over across
within without via upon among toward towards against along around behind beyond
besides during except inside outside near off onto out past per plus regarding
is are was were be been being have has had do does did
will would can could should may might must shall
""".split())

# 适合当新一段开头的词（从句 / 转折 / 介词短语的起点）
EN_OPENER = frozenset("""
that which who whom whose when where why how while because although though
if unless since until before after as but and or so yet however therefore thus
hence moreover nevertheless otherwise instead namely despite provided
in on at by for with from to of through into about between under over across
within without via the a an this these those it they we he she there
""".split())


def _mostly_latin(s: str) -> bool:
    """是不是英文（而不是中文里夹了几个英文词）。"""
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 12 and len(RE_LATIN_CHAR.findall(s)) / len(letters) >= 0.72


def _effective_limit(s: str, limit: int) -> int:
    """英文按"词"折算字符上限；中文一律原样返回。"""
    if not _mostly_latin(s):
        return limit
    cap = min(int(limit * 3.2), 240)        # 62 汉字 ≈ 200 英文字符
    words = list(RE_LATIN_WORD.finditer(s))
    wmax = max(12, int(limit * 0.55))       # 62 → 34 词，约 15 秒封顶
    if len(words) > wmax:
        cap = min(cap, words[wmax - 1].end())
    return max(limit, min(len(s), cap))


def _best_cut_en(s: str, limit: int, spans: list) -> int | None:
    """在英文里挑一个"念得下去"的断点：优先标点后 / 从句前，绝不切在悬空词后。"""
    n, scored = len(s), []
    hard_cap = min(n, int(limit * 1.3))      # 为躲开烂断点，允许略微超长
    for m in re.finditer(r"\s+", s):
        pos = m.end()
        if pos >= n or pos > hard_cap or not _cut_is_safe(pos, spans):
            continue
        left, right = s[:pos].rstrip(), s[pos:].lstrip()
        if not left or not right:
            continue
        pm = re.search(r"([A-Za-z][A-Za-z'’\-]*)[^A-Za-z]*$", left)
        pw = pm.group(1).lower() if pm else ""
        nm = re.match(r"([A-Za-z][A-Za-z'’\-]*)", right)
        nw = nm.group(1).lower() if nm else ""
        tail = left[-1]
        dangling = pw in EN_DANGLING
        score = 10.0 * pos / max(limit, 1)  # 越靠后越好
        if tail in ".!?":
            score += 11
        elif tail in ",;:":
            score += 9
        elif tail in "–—-":
            score += 8
        elif tail in ")]}\"'’":
            score += 5
        if nw in EN_OPENER:
            score += 3
        if dangling:
            score -= 25                     # 悬空词后：几乎否决
        if pos < limit * 0.55:
            score -= 7
        if pos > limit:
            score -= 12.0 * (pos - limit) / max(limit, 1)
        scored.append((score, pos, dangling))
    if not scored:
        return None
    clean = [x for x in scored if not x[2]]
    return max(clean or scored)[1]


def _best_cut(s: str, limit: int, spans: list) -> int:
    """在 (0, limit] 内挑一个最靠后的"干净"断点。

    优先顺序天然由位置决定：越靠后越好，但只认像样的断点（软标点 / 书名引号收尾 /
    英文空格），而且不能落在成对符号内部。英文改走语义打分版，别再砍在冠词后面。
    """
    if _mostly_latin(s):
        return _best_cut_en(s, limit, spans) or limit
    cands = []
    for i in range(min(limit, len(s) - 1)):
        if s[i] in SOFT_BREAK or s[i] in CLOSERS or s[i] == " ":
            pos = i + 1
            if _cut_is_safe(pos, spans):
                cands.append(pos)
    if cands:
        best = max(cands)
        if best >= int(limit * 0.4):      # 太靠前的断点会切出碎句，宁可换个办法
            return best
    # 一个像样的断点都没有：只能硬切。若正好卡在书名号/引号中间，就把整个
    # 成对符号留给下一段（略超几个字，好过把《大雪纷飞的日子》劈成两半）。
    for st, en in spans:
        if st < limit < en:
            if en <= limit + max(8, limit // 4):
                return en                    # 稍微超一点，保整个书名
            if st >= int(limit * 0.4):
                return st                    # 退回符号前面，别切出「《」这种碎片
            break
    return limit


def _hard_split_legacy(s: str, limit: int) -> tuple:
    """中文 / 混排：按软标点预切，再对超长 token 找干净断点（规则成熟，不动）。"""
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
            # 断在哪儿：软标点 > 书名号/引号收尾 > 英文空格，且绝不劈开成对符号
            # （以前只认软标点，英文空格是回退项，书名号里的空格又没设防，
            # 于是「《大雪纷飞的日子》」被切成「《大」+「雪纷飞的日子》」）。
            k = _best_cut(token, limit, _paired_spans(token))
            parts.append(token[:k])
            token = token[k:]
            cut = True
        buf = token
    if buf:
        parts.append(buf)
    return parts, cut


def _hard_split(s: str, limit: int) -> tuple:
    """超长句切开，返回 (片段列表, 是否发生过硬切)。

    英文：折算上限 + 断点语义打分；中文完全沿用旧逻辑，保证既有断句零退化。
    """
    if not _mostly_latin(s):
        return _hard_split_legacy(s, limit)
    eff = _effective_limit(s, limit)
    if len(s) <= eff:
        return [s], False
    parts, rest, cut, guard = [], s, False, 0
    while len(rest) > eff and guard < 40:
        guard += 1
        k = _best_cut_en(rest, eff, _paired_spans(rest))
        if not k or not 0 < k < len(rest):
            k = rest.rfind(" ", 0, eff + 1) + 1 or eff
        parts.append(rest[:k])
        rest = rest[k:]
        cut = True
    if rest:
        parts.append(rest)
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
            if not speakable(txt):
                # 纯标点条目（英文句号被单独切开、目录里的点线……）：本来就不该读。
                # 丢掉这一条，把它的停顿记到前一条上，既不出声也不丢呼吸感。
                if items:
                    items[-1]["pause"] = max(items[-1]["pause"], int(pause))
                continue
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


# ---------------------------------------------------------------- 5) Markdown
# 技术笔记跟小说是两种材料：小说只要「把字念顺」，技术文档要先决定
# 「哪些该读、哪些该改写、哪些该跳过」。这里保留完整的 Markdown 结构块，
# 同一份 blocks 同时投影成「渲染视图」和「三档朗读稿」——不是两件事。
#
# 三档听法：
#   commute  通勤：代码块全跳，只听讲解与任务单（闭眼听）
#   balanced 平衡：代码块改读中文注释（作者自己写好的讲解稿），默认档
#   study    精读：代码块逐行符号转写（对着屏幕学）
# 后 20% 的代码讲解可以用 LLM 离线改写一次存进 prep.json，不必实时调用。

MD_MODES = {
    "commute": {"name": "通勤", "desc": "代码块全跳，只听讲解与任务单"},
    "balanced": {"name": "平衡", "desc": "代码块改读中文注释（默认）"},
    "study": {"name": "精读", "desc": "代码块逐行符号转写，对着屏幕学"},
}
CIRCLED = str.maketrans("①②③④⑤⑥⑦⑧⑨⑩", "一二三四五六七八九十")
CN_DIGIT = "零一二三四五六七八九十"
RE_MD_TABLE_SEP = re.compile(r"^:?-{2,}:?$")
RE_MD_CODE_COMMENT = re.compile(r"//\s*(.+)$")


def cn_num(n: int) -> str:
    return CN_DIGIT[n] if 0 <= n <= 10 else str(n)


def looks_like_markdown(raw: str) -> bool:
    """粘进来的纯文本是不是 Markdown 源码（导入 .md 时会由调用方直接指定）。

    只认「结构标记」的组合信号：# 标题 / ``` 围栏 / | 表格。小说正文里
    几乎不会出现这些，所以误判概率很低；单个信号不算数，避免网页残留。
    """
    s = (raw or "")[:200_000]
    lines = s.split("\n")[:600]
    heads = sum(1 for l in lines if RE_HEADING.match(l))
    fences = sum(1 for l in lines if RE_FENCE.match(l))
    tables = sum(1 for l in lines if RE_TABLE.match(l))
    lists = sum(1 for l in lines if RE_LIST.match(l))
    score = (2 if heads >= 2 else 1 if heads == 1 else 0) \
        + (2 if fences >= 2 else 0) \
        + (1 if tables >= 2 else 0) \
        + (1 if lists >= 2 and heads else 0)
    return score >= 2


def parse_markdown_blocks(raw: str) -> list:
    """MD 源码 -> [{bid, kind, raw, level?/rows?/lang?/ordered?}]。

    跟 parse_blocks 最大的区别：**结构块不再被丢弃**。代码块、表格、分隔线
    过去直接 continue，信息只往朗读一条路流，前端拿不到任何可渲染的东西。
    """
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    s = RE_FRONTMATTER.sub("", s)
    s = RE_INVISIBLE.sub("", s)
    lines = s.split("\n")
    out, para = [], []
    i = 0

    def add(kind, **kw):
        rec = {"bid": len(out), "kind": kind}
        rec.update(kw)
        out.append(rec)

    def flush():
        if para:
            add("para", raw=" ".join(x.strip() for x in para).strip())
            para.clear()

    while i < len(lines):
        line = lines[i]
        fm = RE_FENCE.match(line)
        if fm:
            flush()
            fence = fm.group(1)[0] * 3
            lang = line.strip().lstrip("`~").strip()
            buf = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(fence):
                buf.append(lines[i])
                i += 1
            add("code", raw="\n".join(buf), lang=lang)
            i += 1
            continue
        m = RE_HEADING.match(line)
        if m:
            flush()
            add("heading", raw=m.group(2).strip(), level=len(m.group(1)))
            i += 1
            continue
        if RE_TABLE.match(line):
            flush()
            rows = []
            while i < len(lines) and RE_TABLE.match(lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(RE_MD_TABLE_SEP.fullmatch(c or "") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                add("table", rows=rows, raw="\n".join(" | ".join(r) for r in rows))
            continue
        if RE_HR.match(line):
            flush()
            add("hr", raw=line.strip())
            i += 1
            continue
        m = RE_QUOTE.match(line)
        if m:
            flush()
            body = []
            while i < len(lines):
                q = RE_QUOTE.match(lines[i])
                if not q:
                    break
                body.append(q.group(1).strip())
                i += 1
            add("quote", raw=" ".join(body).strip())
            continue
        m = RE_LIST.match(line)
        if m:
            flush()
            add("list", raw=m.group(3).strip(),
                ordered=bool(re.match(r"\d", m.group(2))))
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        para.append(line)
        i += 1
    flush()
    return [b for b in out if str(b.get("raw") or "").strip()]


def tidy_tech(s: str) -> str:
    """收掉规整过程留下的怪空格：只保留「中文 ↔ 拉丁/数字」之间必要的空隙。"""
    s = re.sub(r"\s+([，。！？；：、）】」》])", r"\1", s)
    s = re.sub(r"([（【「《，。！？；：、])\s+", r"\1", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    s = re.sub(r"([，。！？；：、])\s*([，。！？；：、])", r"\1", s)
    # 「第 3 天」「大约 60 分钟」「第 7 到 8 章」里数字两侧的空格会被中文 TTS
    # 念成不必要的碎停顿；只收「中文量词 ↔ 数字」，中英之间的空隙保留。
    s = re.sub(r"第\s*(\d+)\s*(到|至)\s*(\d+)\s*(章|节|页|天|讲)", r"第\1\2\3\4", s)
    s = re.sub(r"第\s*(\d+)\s*(章|节|页|天|讲|行|句|版)", r"第\1\2", s)
    s = re.sub(r"(大约|总共|约|共|到|至)\s+(\d)", r"\1\2", s)
    s = re.sub(r"(\d)\s+(天|章|节|分钟|秒|个|行|句|页|版|倍|次)", r"\1\2", s)
    s = re.sub(r"第\s+(\d)", r"第\1", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def tech_inline(s: str) -> str:
    """行内技术符号 → 口语。英文术语一律保留，只换「念不出来/会念错」的符号。

    这一层必须在 normalize(tech=True) 之前跑：先把 T& / -> / :: / ... 按语境
    转写掉，通用规整才不会再把它们当成小说的「和 / 到」。
    """
    s = clean_inline(s, tech=True).translate(CIRCLED)
    s = s.replace("#include", "include ")          # 行首 # 没声音，念成 include 就行
    s = re.sub(r"§\s*(\d+(?:\.\d+)*)", r"第 \1 节", s)
    s = re.sub(r"\bDay\s*(\d+)\b", r"第 \1 天", s, flags=re.I)
    s = re.sub(r"(?<![A-Za-z])ch\s*(\d+)\s*[-–~]\s*(\d+)", r"第 \1 到 \2 章", s, flags=re.I)
    s = re.sub(r"(?<![A-Za-z])ch\s*(\d+)\b", r"第 \1 章", s, flags=re.I)
    s = re.sub(r"[~～]\s*(\d+)\s*(min|分钟)", r"大约 \1 分钟", s)
    s = re.sub(r"\b(\d+)\s*min\b", r"\1 分钟", s)
    s = s.replace("——", "，").replace("·", "、")
    s = s.replace("::", " ")
    s = s.replace("->", " 变为 ").replace("→", " 变为 ")
    s = s.replace("&&", " 双引用 ")
    s = re.sub(r"(?<=[A-Za-z0-9_\]])&", " 的引用", s)      # T& / auto&& 的后半
    s = re.sub(r"\.{3}", " 参数包展开 ", s)
    # 模板尖括号只留里面的标识符，不硬塞「版/类型」这种会念错的语义：
    # max<int> → max int，#include <vector> → include vector。
    s = re.sub(r"<\s*([A-Za-z_][^<>\n]{0,30})\s*>", r" \1 ", s)
    s = re.sub(r"char\s*\[\s*(\d+)\s*\]", r"\1 元素 char 数组", s)
    s = re.sub(r"char\s*\*", "char 指针", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", s)
    s = re.sub(r"(?<=[A-Za-z0-9])(?=[\u4e00-\u9fff])", " ", s)
    return tidy_tech(s)


def speak_heading_tech(s: str) -> str:
    t = tech_inline(s)
    t = re.sub(r"（(第 [\d.]+ 节)）", r"，见 \1", t)
    t = t.replace("（", "，").replace("）", "")
    t = t.replace("、", "，")
    t = re.sub(r"[，]{2,}", "，", t)
    t = re.sub(r"总\s*大约", "总共大约", t)
    return tidy_tech(t).strip("，。 ") + "。"


def clean_para_tech(s: str) -> str:
    t = tech_inline(s).replace("（", "，").replace("）", "，")
    t = re.sub(r"，{2,}", "，", t)
    t = re.sub(r"，\s*([。！？])", r"\1", t)
    return tidy_tech(t).strip("， ")


def table_speech(rows: list) -> str:
    """表格不能线性念「30 min ch 7 ...」，要按列名重组成自然句。

    通用做法：先念表头，再逐行「列名是值」。不假设列语义，
    所以任何 Markdown 表格都能读，不会把 tech 笔记里的三列表格念串。
    """
    rows = [r for r in (rows or []) if any(str(c or "").strip() for c in r)]
    if not rows:
        return ""
    head = [tech_inline(str(c or "").strip()) for c in rows[0]]
    lines = ["表格：" + "、".join(h for h in head if h) + "。"]
    for n, row in enumerate(rows[1:], 1):
        pairs = []
        for j, cell in enumerate(row):
            v = tech_inline(str(cell or "").strip()).replace("/", "、")
            if not v:
                continue
            label = head[j] if j < len(head) and head[j] else f"第{cn_num(j + 1)}列"
            pairs.append(f"{label}是{v}")
        if pairs:
            lines.append(f"第{cn_num(n)}行，" + "，".join(pairs) + "。")
    return tidy_tech("".join(lines))


def code_comments_speech(raw: str) -> str:
    """平衡档：技术文档的代码注释，天然就是作者写好的朗读讲解稿。

    不逐字念 C++，也不整段跳过 —— 只把 // 后面的中文串起来。
    纯规则就能拿到 80 分，不必实时调 LLM。
    """
    parts = []
    for line in (raw or "").splitlines():
        m = RE_MD_CODE_COMMENT.search(line)
        if not m:
            continue
        c = tech_inline(m.group(1).strip())
        c = re.sub(r"^[\s—-]+", "", c).replace("#include", "include")
        if c:
            parts.append(c.rstrip("。！？，"))
    if not parts:
        return ""
    return tidy_tech("看一段示例：" + "。".join(parts) + "。")


def code_lines_speech(raw: str) -> str:
    """精读档：逐行符号转写。规则生成的是粗稿，生产环境可用 LLM 离线改写一次。"""
    parts = []
    for line in (raw or "").splitlines():
        l = line.strip()
        if not l:
            continue
        m = RE_MD_CODE_COMMENT.match(l)
        t = tech_inline(m.group(1) if m else l)
        t = t.replace("{", "，").replace("}", "").replace(";", "，")
        t = t.replace("(", "，").replace(")", "，")
        t = re.sub(r"\s{2,}", " ", t).strip("， ")
        if t:
            parts.append(t)
    return tidy_tech("逐行精读：" + "。".join(parts) + "。") if parts else ""


def md_block_speech(b: dict, mode: str) -> tuple:
    """返回 (处置策略, 朗读文本)。策略会透给前端，让「哪些该读」看得见。"""
    k = b["kind"]
    if k == "heading":
        return "转写", speak_heading_tech(b.get("raw", ""))
    if k == "para":
        return "直读", clean_para_tech(b.get("raw", ""))
    if k in ("list", "quote"):
        return "直读", tech_inline(b.get("raw", ""))
    if k == "table":
        t = table_speech(b.get("rows"))
        return ("改写", t) if t else ("跳过", "")
    if k == "code":
        if mode == "commute":
            return "跳过", ""
        if mode == "balanced":
            t = code_comments_speech(b.get("raw", ""))
            return ("改写", t) if t else ("跳过", "")
        t = code_lines_speech(b.get("raw", ""))
        return ("精读", t) if t else ("跳过", "")
    return "跳过", ""                       # hr / 纯格式


def build_md_items(blocks: list, speech: dict, mode: str,
                   max_len: int, min_len: int, do_normalize: bool) -> list:
    """把某一档的朗读文本切成逐句条目，并打上 bid / si 双层定位。"""
    items = []
    for b in blocks:
        bid = b["bid"]
        _policy, txt = speech[bid][mode]
        if not txt:
            continue
        txt = normalize(txt, do_normalize, tech=True)
        txt = re.sub(r"[，,、：:；;]+$", "。", txt)
        if not speakable(txt):
            continue
        kind = b["kind"] if b["kind"] in ("heading", "list", "quote") else "para"
        sub = split_items("", max_len=max_len, min_len=min_len,
                          blocks=[{"kind": kind, "text": txt}])
        for si, it in enumerate(sub):
            it["bid"] = bid
            it["si"] = si
            items.append(it)
        if sub:
            # 块与块之间要有一次完整换气，否则表格紧接正文会糊成一团
            sub[-1]["pause"] = max(sub[-1]["pause"],
                                   PAUSE["heading"] if kind == "heading" else PAUSE["paragraph"])
    return items


def md_payload(blocks: list, max_len: int, min_len: int,
               do_normalize: bool, mode: str) -> dict:
    """一次性生成三档朗读稿 + 渲染视图，全部塞进 prep.json 缓存。"""
    speech = {b["bid"]: {m: md_block_speech(b, m) for m in MD_MODES} for b in blocks}
    modes = {}
    for m in MD_MODES:
        items = build_md_items(blocks, speech, m, max_len, min_len, do_normalize)
        modes[m] = {
            "items": items,
            "segments": [it["text"] for it in items],
            "count": len(items),
            "chars": sum(len(it["text"]) for it in items),
        }
    view = []
    for b in blocks:
        rec = {k: v for k, v in b.items() if k in ("bid", "kind", "raw", "level",
                                                    "rows", "lang", "ordered")}
        rec["policy"] = {m: speech[b["bid"]][m][0] for m in MD_MODES}
        view.append(rec)
    mode = mode if mode in MD_MODES else "balanced"
    return {"is_md": True, "mode": mode, "modes": modes, "view": view,
            "block_count": len(blocks), "names": {k: v["name"] for k, v in MD_MODES.items()},
            "descs": {k: v["desc"] for k, v in MD_MODES.items()}}


# ---------------------------------------------------------------- 一站式

def prepare(raw: str, max_len: int = 62, min_len: int = 9, do_normalize: bool = True,
            markdown: bool | None = None, md_mode: str = "balanced") -> dict:
    """原文 -> {text, items, segments, blocks[, md]}。

    markdown=None 时自动检测；False 强制走原来的小说路径（行为一字不变）。
    Markdown 分支会额外返回 md：三档 items + 渲染视图 + 每块的处置策略。
    """
    is_md = looks_like_markdown(raw) if markdown is None else bool(markdown)
    if is_md:
        blocks = parse_markdown_blocks(raw)
        if not blocks:
            is_md = False
        else:
            md = md_payload(blocks, max_len, min_len, do_normalize, md_mode)
            cur = md["modes"][md["mode"]]
            text = "\n\n".join(cur["segments"]).strip()
            return {"text": text, "items": cur["items"], "segments": cur["segments"],
                    "blocks": [{"kind": b["kind"], "text": b.get("raw", "")} for b in blocks],
                    "md": md}

    blocks = parse_blocks(raw)
    cooked = []
    for b in blocks:
        t = clean_inline(b["text"])
        if not t:
            continue
        is_latin = _mostly_latin(t)
        t = normalize(t, do_normalize, latin=is_latin)
        # 括号注释会被读法规整改写成逗号分隔（"占比（Top10）68%）" -> "...，Top10..."），
        # 于是整段可能以逗号/顿号收尾。朗读时那是个"话没说完"的半截停顿，
        # 听起来很怪；补成句号，分句和停顿都干脆。英文用半角句号，别冒出中文「。」。
        t = (re.sub(r"[,:;]+$", ".", t) if is_latin else
             re.sub(r"[，,、：:；;]+$", "。", t))
        # 纯标点/分隔线的 block（"."、*** 之后剩下的 "。"）没有可念的内容，
        # 不进朗读稿：合成器会拒绝它，播放器还会卡死在这一句上。
        if t and speakable(t):
            cooked.append({"kind": b["kind"], "text": t})
    text = "\n\n".join(b["text"] for b in cooked).strip()
    items = split_items("", max_len=max_len, min_len=min_len, blocks=cooked) if cooked else []
    if not items and text and speakable(text):
        items = [{"text": text, "kind": "para", "pause": PAUSE["paragraph"], "rate": 0}]
    return {"text": text, "items": items,
            "segments": [it["text"] for it in items], "blocks": cooked}
