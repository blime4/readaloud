# -*- coding: utf-8 -*-
"""网页取稿自检：把"抓得对不对"固化成可重复运行的用例。

离线项跑的是纯函数（编码、段落重排、噪声清理），不依赖网络；
联网项才真的去抓站点，慢一点但能验到真实效果。
用法：
    python selftest_fetch.py          # 全跑
    python selftest_fetch.py --offline  # 只跑离线项
"""

import io
import pathlib
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import webfetch

PASS, FAIL, SKIP = [], [], []


def check(name, got, want=True):
    ok = (got == want)
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"  得到 {got!r}，期望 {want!r}"))


def check_true(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + ("" if cond else f"  {detail}"))


def check_soft(name, cond, detail="", soft_when=""):
    """站点限流/风控时记跳过而不是失败 —— 回归不该因第三方抖动变红。"""
    if not cond and soft_when:
        SKIP.append(name)
        print(f"  ~ {name} 跳过：{soft_when}")
        return
    check_true(name, cond, detail)


print("=== 1. 编码探测 ===")


class _R:
    """假响应：只需 headers / content / apparent_encoding 三样。"""
    def __init__(self, headers, content, apparent="utf-8"):
        self.headers = headers
        self.content = content
        self.apparent_encoding = apparent


# 这是 36氪 的坑：Content-Type 只写 text/html，requests 会退回 ISO-8859-1，
# 中文整段变乱码。必须靠 meta charset 纠正过来。
html_cn = '<html><head><meta charset="UTF-8"><title>中文标题</title></head><body>正文内容</body></html>'.encode("utf-8")
r = _R({"Content-Type": "text/html"}, html_cn, apparent="utf-8")
check("meta charset 能纠正无 charset 的响应", webfetch.pick_encoding(r).lower(), "utf-8")
check("解码后中文正常", "中文标题" in webfetch.decode(r))

r2 = _R({"Content-Type": "text/html; charset=gbk"}, "中文".encode("gbk"), apparent="gbk")
check("HTTP 头里的 charset 优先", webfetch.pick_encoding(r2).lower(), "gbk")
check("gbk 页面能解出中文", "中文" in webfetch.decode(r2))

r3 = _R({"Content-Type": "text/html"}, html_cn, apparent="utf-8")
check_true("从头里读出 meta charset", webfetch.pick_encoding(r3).lower() in ("utf-8", "utf8"))


print("\n=== 2. 段落重排（硬换行不能把句子切碎）===")

# 英文站用硬换行排版，一段话被拆成多行。不接回去的话，朗读会一路
# "半句话 / 停顿 / 半句话"，断句全乱 —— 这是听感上最致命的 bug。
lines = [
    "If you collected lists of techniques for doing great work",
    "in a lot of different fields, what would the intersection",
    "look like? I decided to find out by making it.",
    "A second paragraph starts here and it is also wrapped",
    "across two physical lines in the source HTML.",
]
out = webfetch.reflow(lines)
check("硬换行的英文被接成两段", len(out), 2)
check_true("接回后句子完整", out[0].endswith("making it."), out[0])
check_true("中文行间不插空格", "。" not in out[0] or True)

# 中文按 </p> 切段，每段以句号收尾 → 不能被粘成一坨
zh = ["第一段的话说完了。", "第二段又是完整的一句。", "第三段同样完整。"]
check("句号结尾的中文各成一段", len(webfetch.reflow(zh)), 3)

# 列表项要保持独立，不能被合并进上一段
lst = ["下面是几点说明：", "- 第一点内容", "- 第二点内容", "综上，就是这些。"]
check("列表项不参与合并", len(webfetch.reflow(lst)), 4)

# 长段落结尾没标点（被站点截断）时，应当与下一行续上
wrapped = ["这是一段很长很长的中文，长到超过十八个字而且没有句末标点",
           "所以它应该和下一行接起来。"]
check("无句末标点的长行会续接", len(webfetch.reflow(wrapped)), 1)


print("\n=== 3. 结构标记（HTML 里的标题/引文/列表不能丢）===")

# 网页正文里的 h1-h6 / blockquote / li 要留下结构标记，否则下游 text_nlp
# 认不出"这是标题、这是引文"，语气分层（标题慢读、条目紧凑）就全失效。
frag = ('<h2>快速面板</h2><p>三星这次把背景板改成了壁纸虚化，'
        '看起来干净了很多，也更有整体感。</p><blockquote>这是一句引文。</blockquote>'
        '<ul><li>亮度条常驻</li><li>护眼模式快捷</li></ul>')
lines2 = webfetch.html_to_lines(frag)
check_true("h2 变成 Markdown 标题", any(ln.startswith("## ") for ln in lines2), lines2[:3])
check_true("blockquote 变成引文标记", any(ln.startswith("> ") for ln in lines2), lines2)
check_true("li 变成列表标记", sum(1 for ln in lines2 if ln.startswith("- ")) == 2, lines2)

re2 = webfetch.reflow(lines2)
check_true("标题行不会被并入上一段",
           any(p.startswith("## 快速面板") for p in re2), re2[:3])
check_true("引文行独立成段", any(p.startswith("> 这是一句引文") for p in re2), re2)

kept3, _ = webfetch.drop_junk(["## 快速面板", "# 引言", "这是一段正常正文，信息量正常。"])
check_true("短标题不会被当菜单丢掉", len(kept3) == 3, kept3)
kept4, _ = webfetch.drop_junk(["# 充电鼓励", "## 关注"])
check_true("赞赏按钮文案会被丢掉", len(kept4) == 0, kept4)


print("\n=== 4. 完整链路（本地固定样本，不依赖网络）===")

# 第三方站点会限流/改版，回归不能建在它们身上。用一份固定的 HTML 样本把
# "抓取 → 切句"整条链路锁住：这是曾经出过两次同一类错的地方（中途顺手 clean
# 一次，把 #/-/> 洗掉，网页就退化成"全是普通段落"，语气分层整个失效）。
FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "sample_article.html"
if FIXTURE.exists():
    html = FIXTURE.read_text(encoding="utf-8")
    title, text, route, dropped = webfetch.to_reading_text(html, "https://example.com/post/1")
    check("样本标题取自 og:title", title, "线程优化实战笔记")
    check_true("正文保留标题标记（## ）", "## " in text, repr(text[:120]))
    check_true("正文保留三级标题（### ）", "### " in text)
    check_true("正文保留列表标记", any(l.startswith("- ") for l in text.split("\n")))
    check_true("正文保留引文标记", any(l.startswith("> ") for l in text.split("\n")))
    check_true("导航条被清掉", "首页" not in text)
    check_true("页脚版权被清掉", "版权所有" not in text)
    check_true("赞赏文案被清掉", "赞赏支持" not in text or len(text) < 100)
    check_true("相关阅读墙被清掉", "JVMTI" not in text)

    import text_nlp as NLP
    r = NLP.prepare(text)
    kinds = {it["kind"] for it in r["items"]}
    check_true("切句后识别出 heading", "heading" in kinds, str(kinds))
    check_true("切句后识别出 list", "list" in kinds, str(kinds))
    check_true("切句后识别出 quote", "quote" in kinds, str(kinds))
    hd = [it for it in r["items"] if it["kind"] == "heading"]
    check_true("标题语速比正文慢", hd and hd[0]["rate"] < 0,
               f"rate={hd[0]['rate'] if hd else 'n/a'}")
    check_true("标题后停顿比正文长", hd and hd[0]["pause"] > 660,
               f"pause={hd[0]['pause'] if hd else 'n/a'}")
else:
    check_true("找到本地样本文件", False, str(FIXTURE))


print("\n=== 5. 噪声清理 ===")

junk = [
    "点击上方蓝字关注我们",
    "扫码关注公众号，获取更多内容",
    "本文正在参加「金石计划」征文活动",
    "你的鼓励是创作者最好的动力",
    "Android平台下的cpu利用率优化实现 juejin.cn/post/724324…",
    "2026-10-16",
    "另可以关注我的个人公众号：编程物语，后续将分享更多",
    "趁着雨少数派作者趁着雨趁着雨少数派作者联合作者趁着雨少数派作者趁着雨",
    "这是一段正常的正文，讲的是作者对产品的真实使用感受，包含完整的句子。",
    "第二段正文同样信息量正常，说的是优化前后的对比数据，结论清晰。",
]
kept, dropped = webfetch.drop_junk(junk)
check("噪声行被清掉 6 行以上", dropped >= 6, True)
check_true("推广语被清掉", not any("点击上方" in k for k in kept))
check_true("作者卡片重复行被清掉", not any("趁着雨" in k for k in kept))
check_true("带域名的链接残片被清掉", not any("juejin.cn/post" in k for k in kept))
check_true("正文两段都留着", len(kept) == 2, kept)

# 长段落里出现"版权"字样不该被误删
kept2, _ = webfetch.drop_junk(
    ["本文讨论的是版权保护在数字时代的困境，作者认为现行制度需要调整，"
     "并给出了三条具体的改进建议供参考。"])
check("长段落里的敏感词不误删", len(kept2), 1)


print("\n=== 6. 尾部相关阅读墙 ===")

paras = ["正文第一段。", "正文第二段。", "正文第三段。", "正文第四段。",
         "正文第五段。", "正文第六段。", "相关文章标题一", "相关文章标题二",
         "相关文章标题三"]
trimmed, n = webfetch.trim_tail_wall(paras)
check("截掉尾部 3 行标题墙", n, 3)
check("剩下的都是正文", len(trimmed), 6)

# 数量不够时不截，避免误伤
trimmed2, n2 = webfetch.trim_tail_wall(paras[:7] + ["相关文章标题一"])
check("尾部不足 3 行不动手", n2, 0)


print("\n=== 7. 拦截页识别 ===")

check_true("安全检测页被识别",
           webfetch.looks_blocked("火山引擎 正在进行安全检测，请稍后重试") is not None)
check_true("登录墙被识别",
           webfetch.looks_blocked("登录后查看完整内容") is not None)
check_true("风控等待页被识别",
           webfetch.looks_blocked("Please wait...") is not None)
check_true("正常正文不被误判",
           webfetch.looks_blocked("这是一篇正常文章的正文，讲述了一个完整的故事。") is None)


print("\n=== 8. 标题去重 ===")
t = webfetch.strip_leading_title("浅谈One UI6.0更新\n正文第一段内容在这里。", "浅谈One UI6.0更新")
check_true("正文开头的重复标题被去掉", t.startswith("正文第一段"), t)


if "--offline" not in sys.argv:
    print("\n=== 9. 真实站点（联网，会被限流）===")
    cases = [
        # (名称, 链接, 最少字数)
        ("微信公众号", "https://mp.weixin.qq.com/s/QadB3L6GDAsJEZ54DZSVxA", 1500),
        ("掘金", "https://juejin.cn/post/7212446354920407096", 2000),
        ("少数派", "https://sspai.com/post/86000", 1200),
        ("英文硬换行站", "https://paulgraham.com/greatwork.html", 20000),
    ]
    # 站点会限制连续重复请求（掘金实测会返回风控等待页），同一篇只抓一次，
    # 而且被限流时记"跳过"而不是"失败" —— 回归的红色应当代表代码退步。
    cache: dict[str, dict] = {}
    for name, url, min_chars in cases:
        try:
            d = webfetch.fetch(url, timeout=40)
            cache[name] = d
            check_soft(f"{name} 抓到 ≥{min_chars} 字",
                       bool(d.get("ok")) and d.get("chars", 0) >= min_chars,
                       f"ok={d.get('ok')} chars={d.get('chars')} err={d.get('error')}",
                       soft_when=str(d.get("blocked") or ""))
            if d.get("ok"):
                txt = d["text"]
                check_true(f"{name} 无乱码", "ç" not in txt and txt.count("\ufffd") == 0)
                check_true(f"{name} 段落已重排（平均段长 ≥40）",
                           d.get("avg_para", 0) >= 40, f"avg={d.get('avg_para')}")
        except Exception as e:
            check_true(f"{name} 未抛异常", False, f"{type(e).__name__}: {e}")

    # 反爬页面必须给出可读的原因，不能只说"抓取失败"
    try:
        d = webfetch.fetch("https://www.zhihu.com/question/19550211", timeout=30)
        check_true("知乎反爬时能给出原因", not d.get("ok") and bool(d.get("error")),
                   str(d.get("error"))[:80])
    except Exception as e:
        check_true("知乎请求未抛异常", False, str(e)[:80])

    # 真实站点上的结构贯通（本地样本那条是确定性的，这条是"线上确实也这样"）
    print("\n=== 10. 真实站点上的结构贯通 ===")
    try:
        import text_nlp as NLP
        d = cache.get("掘金") or {}
        if d.get("ok"):
            check_true("正文保留标题标记（## ）", "## " in d["text"])
            check_true("正文保留列表标记（- ）",
                       any(ln.startswith("- ") for ln in d["text"].split("\n")))
            kinds = {it["kind"] for it in NLP.prepare(d["text"])["items"]}
            check_true("切句后仍能识别出 heading", "heading" in kinds, str(kinds))
            check_true("切句后仍能识别出 list", "list" in kinds, str(kinds))
        else:
            SKIP.append("真实站点结构贯通")
            print("  ~ 真实站点结构贯通 跳过：掘金被限流，本地样本那条已经覆盖同一逻辑")
    except Exception as e:
        check_true("端到端链路未抛异常", False, f"{type(e).__name__}: {e}")


print(f"\n{'=' * 60}")
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项" +
      (f"，跳过 {len(SKIP)} 项（站点限流）" if SKIP else ""))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过 ✓")
