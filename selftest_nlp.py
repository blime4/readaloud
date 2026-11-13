# -*- coding: utf-8 -*-
"""文本智能层自检：结构解析 / 读音规整 / 逐句切分 / 音色推荐。

跑法：  .venv/Scripts/python.exe selftest_nlp.py
"""

import sys

import text_nlp as N

OK, BAD = [], []


def check(name, got, want=None, cond=None):
    good = cond(got) if cond else (got == want)
    (OK if good else BAD).append(name)
    mark = "√" if good else "×"
    print(f"  {mark} {name}")
    if not good:
        print(f"      得到: {got!r}")
        if want is not None:
            print(f"      期望: {want!r}")


print("=== 1) 结构解析 ===")
raw = """---
title: 测试
---

# 第三季度复盘

## 一、营收

- 营收 1,280 万元
- 同比 +30%

| 指标 | 数值 |
| --- | --- |
| 客户数 | 412 |

> 结论：把更多资源投向 AI 方向。

正文段落，**加粗**、`code`、[链接](http://x.com)、==高亮==、😀、<br>换行。

```py
print("这段不该被念出来")
```
"""
blocks = N.parse_blocks(raw)
kinds = [b["kind"] for b in blocks]
check("认出 标题/列表/引用/正文", kinds, ["heading", "heading", "list", "list", "quote", "para"])
check("表格行被剔除", not any("客户数" in b["text"] for b in blocks), True)
check("代码块被剔除", not any("不该被念出来" in b["text"] for b in blocks), True)
check("front matter 被剔除", not any("title:" in b["text"] for b in blocks), True)

print("=== 2) 行内清洗 ===")
c = N.clean_inline("**加粗**、==高亮==、`code`、~~删除~~、<br>换行</br>、&nbsp;空 格、😀、^[脚注]、[[双链|别名]]。")
check("富文本残留清干净", c, "加粗、高亮、code、删除、 换行 、 空 格、别名。")

gitbook_entry = """Adam's apple
*/ˌædəmz 'æpəl/ · n*

**释义**　喉结

{% hint style="info" %} **词源**

典出《圣经·旧约·创世纪》。

Adam 一词源自希伯来语，原义为“人”。 {% endhint %}*&#x20;
"""
spoken_entry = ("Adam's apple\n\n释义 喉结\n\n词源\n\n典出《圣经·旧约·创世纪》。"
                "\n\nAdam 一词源自希伯来语，原义为“人”。")
check("GitBook 词条去掉音标行和提示框标记，保留正文", N.clean(gitbook_entry), spoken_entry)
check("GitBook 清理适用于关闭读音规整的朗读稿",
      N.prepare(gitbook_entry, do_normalize=False)["text"], spoken_entry)
check("保留文档路径和正文中的语言学例子",
      N.clean("/usr/local/\n\n/home/\n\n请比较 /æ/ 和 /ɑː/。"),
      "/usr/local/\n\n/home/\n\n请比较 /æ/ 和 /ɑː/。")
check("多读音元数据不进入朗读稿",
      N.clean("## discount\n\n_/ˈdɪskaʊnt/（名词）；/dɪˈskaʊnt/（动词） · n_\n\n折扣"),
      "discount\n\n折扣")

print("=== 3) 读音规整 ===")
cases = [
    ("日期", "2026-10-14", "2026年10月14日"),
    ("时间", "14:30", "14点30分"),
    ("百分比", "涨幅 62%", "涨幅 百分之62"),
    ("货币", "价格 $1,280", "价格 1280美元"),
    ("货币人民币", "收入 ¥1280", "收入 1280元"),
    ("大额", "营收 12800 元", "营收 1.28万元"),
    ("区间", "区间 3.5–4.2", "区间 3.5到4.2"),
    ("版本号", "ReadAloud v1.1.0", "ReadAloud 版本1.1.0"),
    ("协议版本", "支持 HTTP/2", "支持 HTTP 2"),
    ("斜杠并列", "AI/ML 是重点", "AI 和 ML 是重点"),
    ("括号转停顿", "毛利率 62%（上季度 55%），很好。", "毛利率 百分之62，上季度 百分之55，很好。"),
    ("箭头", "数量 3→5", "数量 3到5"),
    ("乘号", "面积 3×4", "面积 3乘4"),
    ("话题编号", "#1 问题", "第1 问题"),
]
for name, src, want in cases:
    got = N.normalize(src)
    good = got == want
    (OK if good else BAD).append(name)
    print(f"  {'√' if good else '×'} {name:10} {src!r} -> {got!r}")
    if not good:
        print(f"      期望: {want!r}")

check("URL 变可读词", N.normalize("见 https://example.com/a/b?c=1 这里"), "见 网页链接 这里")
check("中英之间插空格", N.normalize("AI产品线"), "AI 产品线")
check("关闭时保持原文", N.normalize("价格 $1,280", enabled=False), "价格 $1,280")
check("用户词典生效", N.normalize("用 K8s 部署"), "用 Kubernetes 部署")

# 纯英文段落走轻量模式：中文专属的符号/日期/货币/百分号读法一律不套，
# 否则英文书会把 "R&D" 念成 "R和D"、"and/or" 念成 "and和or"。
en_raw = "R&D and/or AI/ML cost $1,280 (62%) on 2026-10-14, see v1.2 & C++."
check("英文符号不被念成中文", N.normalize(en_raw), en_raw)
check("英文 C++ 不套中文发音", "C 加加" in N.normalize("The C++ class uses std::vector<int>."), False)
check("英文 K8s 通用转写仍生效", "Kubernetes" in N.normalize("We run K8s in production clusters."), True)
check("中文读音规整不受影响", "1280美元" in N.normalize("价格 $1,280"), True)
check("英文块结尾不冒出中文句号",
      "。" in N.prepare("R&D and/or AI/ML cost $1,280,")["segments"][0], False)

print("=== 4) 逐句切分（这是旧版最大的问题）===")
t = "他停顿了一下，说：“这样做是对的。”然后转身就走了。"
items = N.split_items("", blocks=[{"kind": "para", "text": t}])
check("引号内句号不被截断", [i["text"] for i in items], [t])

date_intro = "该词源于一本书的书名,作者是公元6世纪拜占庭史学家普罗科匹厄斯"
date_next = "他曾在君士坦丁堡为官多年而且身居要职。"
date_sentence = date_intro + "，Procopius, 约490到约562。"
check("生卒年中的两个问号不会把一句拆开",
      N.prepare(date_intro + "（Procopius, 490? —562?）。" + date_next)["segments"],
      [date_sentence, date_next])
check("已经展开的括号内生卒年仍然连读",
      N.prepare(date_intro + "，Procopius, 490? —562?。" + date_next)["segments"],
      [date_sentence, date_next])
date_unmodified = date_intro + "，Procopius, 490? —562?。"
check("关闭读音规整仍然连读已展开的生卒年",
      N.prepare(date_unmodified + date_next, do_normalize=False)["segments"],
      [date_unmodified, date_next])
check("保留公元前年代及两位数年份",
      N.prepare("昆图斯（82？ —30B. C.）曾经担任此职。")["segments"],
      ["昆图斯，公元前约82到30，曾经担任此职。"])
check("关闭读音规整仍然连读公元前年代",
      N.prepare("昆图斯，82？ —30B. C.，曾经担任此职。", do_normalize=False)["segments"],
      ["昆图斯，82？ —30B. C.，曾经担任此职。"])
check("普通问句和破折号后回答仍然分句",
      N.prepare("是编号490? —没错。预算是100—200? 对。 ")["segments"],
      ["是编号490?", "—没错。", "预算是100到200?", "对。"])
check("问句里的产品型号不会转为约数范围",
      N.prepare("产品是AB12? - 34CD是另一个型号。")["segments"],
      ["产品是 AB12?", "- 34CD 是另一个型号。"])
check("回答以年份开头时仍保留问题边界",
      N.prepare("编号是42? — 2024年才开始编号。")["segments"],
      ["编号是42?", "— 2024年才开始编号。"])
check("公元前缩写末尾句号可同时结束整句",
      N.prepare("他生活于 500?—300 B.C. 他写了这本书。")["segments"],
      ["他生活于 公元前约500到300.", "他写了这本书。"])
check("区间末尾真正的问号仍可结束英文问句",
      N.prepare("Who was active around 490?—562? I don't know.", do_normalize=False)["segments"],
      ["Who was active around 490?—562?", "I don't know."])

t2 = "第一句话在这里。第二句话稍微长一点点。第三句话结束了。"
items2 = N.split_items("", blocks=[{"kind": "para", "text": t2}])
check("真逐句：3 句 -> 3 段", len(items2), 3)
check("每段都短", max(len(i["text"]) for i in items2) < 30, True)

t3 = "Hello world. This is a test. Second sentence. 这是中文补充说明。"
items3 = N.split_items("", blocks=[{"kind": "para", "text": t3}])
check("英文句号能切句", len(items3) >= 3, True)

long_sent = ("人工智能的发展正在深刻改变我们的工作方式，过去需要人工完成的重复性任务如今可以交给模型自动处理，"
             "这并不意味着人类不重要，而是说人的价值更多体现在判断、审美和决策上，未来的竞争是谁更会用工具。")
items4 = N.split_items("", blocks=[{"kind": "para", "text": long_sent}])
check("超长句被硬切", len(items4) >= 2 and all(len(i["text"]) <= 62 for i in items4), True)

items5 = N.split_items("", blocks=[{"kind": "heading", "text": "第三季度复盘"},
                                   {"kind": "list", "text": "营收 1280 万元"},
                                   {"kind": "para", "text": "小结。"}])
check("标题停顿更长", items5[0]["pause"] > items5[-1]["pause"], True)
check("标题语速更慢", items5[0]["rate"] < 0, True)
check("段末停顿是段落级", items5[-1]["pause"] >= 600, True)

# 回归：英文句号被单独切成一条 "."，送去合成会 502「模型没有产出音频」，
# 播放器就卡死在这一句上再也读不下去。纯标点必须既不朗读也不阻塞。
check("纯标点不成条", N.speakable("."), False)
check("省略号/破折号不算可读", [N.speakable(x) for x in ("……", "——", "。。。")],
      [False, False, False])
check("有字才算可读", [N.speakable(x) for x in ("abc", "中文", "1")], [True, True, True])
punct_mix = "他说完了。. 然后他走了。"
check("句间孤立句号被丢掉",
      [i["text"] for i in N.split_items("", blocks=[{"kind": "para", "text": punct_mix}])],
      ["他说完了。", "然后他走了。"])
punct_para = N.prepare("第一段内容。\n\n.\n\n第二段内容。")
check("整段只有标点也不进朗读稿", punct_para["segments"], ["第一段内容。", "第二段内容。"])

# 英文长句：旧版沿用中文的 62 字符上限，78 字符的英文句被切成
# "…blue blood to" / "marry his son." —— 断在介词后面，念起来就是半截话。
# 旧断言「第二段以 marry 开头」其实把这个毛病固化成了预期，一并纠正。
long_en = ("The earl refuses to allow anyone who is not of blue blood "
           "to marry his son.")
en_items = N.split_items(long_en)
check("英文长句无损拼接", " ".join(i["text"] for i in en_items), long_en)
check("中等长度英文整句成条", len(en_items), 1)
check("不再把 to marry 切开", "to marry his son." in en_items[0]["text"], True)

# 用户真实案例：C++ 教材的破折号插入语。旧版剁成 4 段，分别断在
# "through" / "a" / "a" 后面，逐句高亮跟着一堆半截话走。
cpp_en = ("When a class is a resource handle – that is, when the class is "
          "responsible for an object accessed through a pointer – the default "
          "memberwise copy is typically a disaster.")
cpp_items = N.split_items(cpp_en)
check("破折号插入语整句成条", len(cpp_items), 1)
check("英文案例无损拼接", " ".join(i["text"] for i in cpp_items), cpp_en)
check("英文书端到端仍是完整句", N.prepare(cpp_en)["segments"], [cpp_en])

# 真正超长的英文才分段，且必须落在标点 / 从句边界，绝不结束在冠词、
# 介词、be 动词、助动词这些"悬空词"上。
very_long = (
    "When a class is a resource handle – that is, when the class is responsible for an "
    "object accessed through a pointer – the default memberwise copy is typically a "
    "disaster, because it would violate the invariant that the pointer points to exactly "
    "one object, and a copy would silently give two independent handles to the same "
    "resource, which means that both destructors would try to delete the same object "
    "when they go out of scope.")
vl = N.split_items(very_long)
check("超长英文仍会分段", len(vl) >= 2, True)
check("超长英文无损拼接", " ".join(i["text"] for i in vl), very_long)
for j, it in enumerate(vl[:-1]):
    tail_word = it["text"].rstrip(",.!?;:–—-()\"'").split()[-1].lower()
    check(f"第 {j + 1} 段不结束在悬空词上（{tail_word}）",
          tail_word in N.EN_DANGLING, False)

check("丢掉标点后不丢停顿",
      N.split_items("", blocks=[{"kind": "para", "text": punct_mix}])[0]["pause"] >= 330, True)

print("=== 5) 端到端 prepare ===")
# 显式关掉 Markdown：这一节测的是普通文本路径（表格/代码块应当被剔除），
# 不能让自动检测悄悄把样本切到 MD 分支去，否则断言的语义就变了。
p = N.prepare(raw, markdown=False)
check("产出 items 与 segments 一致", p["segments"] == [i["text"] for i in p["items"]], True)
check("text 非空", bool(p["text"]), True)
print("   逐句稿：")
for i, it in enumerate(p["items"], 1):
    print(f"     {i:2}. [{it['kind']:7} 停{it['pause']:4}ms 速{it['rate']:+3}%] {it['text']}")

print("=== 6) 音色推荐 ===")
r1 = N.recommend_voice("本季度 API 接口的部署配置与服务器日志需要统一。")
r2 = N.recommend_voice("那年夏天，他背着包出发了，故事就这样开始了。")
r3 = N.recommend_voice("The quick brown fox jumps over the lazy dog and runs away.")
r4 = N.recommend_voice("我们今天用了 GPU 和 API，也用了 machine learning 模型。")
for tag, r in (("技术", r1), ("故事", r2), ("英文", r3), ("中英混排", r4)):
    print(f"    {tag:6} -> {r['voice']:36} {r['reason']}")
check("技术稿推荐播报音", "yunyang" in r1["voice"].lower() or "yunxi" in r1["voice"].lower(), True)
check("故事稿推荐叙事音", "yunjian" in r2["voice"].lower(), True)
check("英文稿推荐英文音色", r3["voice"].startswith("en-"), True)
check("中英混排推荐多语种音色", "Multilingual" in r4["voice"], True)

print("=== 7) 结构层次（回归：kind 不能被洗成 para）===")
# 曾经的 bug：/api/prepare 先 clean_text(raw) 再 plan_text()，而 clean_text 会把
# #、-、> 一起洗掉，于是标题/列表/引文永远识别不出来、全变成 para，
# 「标题慢读、条目紧凑、引用留白」这套语气层次静默失效 —— 页面看着正常，就是读着平。
struct_raw = "# 季度复盘\n\n正文一段。\n\n- 条目一\n- 条目二\n\n> 引用一句。\n"
sp = N.prepare(struct_raw, markdown=False)
kinds = [it["kind"] for it in sp["items"]]
print(f"    kinds = {kinds}")
check("标题仍是 heading", "heading" in kinds, True)
check("列表仍是 list", "list" in kinds, True)
check("引用仍是 quote", "quote" in kinds, True)
check("标题停顿 > 正文停顿",
      max(it["pause"] for it in sp["items"] if it["kind"] == "heading")
      > max(it["pause"] for it in sp["items"] if it["kind"] == "para"), True)
# 括号注释被改写成逗号分隔后，整段可能以逗号收尾 —— 朗读时是"话没说完"的半截停顿
tail = N.prepare("- 客户占比（Top10）68%\n")["items"][-1]["text"]
print(f"    列表项收尾: {tail!r}")
check("列表项不以逗号收尾", not tail.endswith(("，", ",", "、", "：")), True)

print("=== 8) Markdown 三档听法（1.13.0）===")
novel_sample = ("第一章 雪夜\n\n他说：“我不去。”《大雪纷飞的日子》里下着雪。\n"
                "第2章 归途\n\n她笑了，风把雪吹进屋里。\n")
check("MD 源码能自动识别", N.looks_like_markdown(raw), True)
check("小说不会被误判成 MD", N.looks_like_markdown(novel_sample), False)
mdp = N.prepare(raw, markdown=True)
md = mdp.get("md") or {}
modes = md.get("modes") or {}
check("返回 md 三档结构", sorted(modes), ["balanced", "commute", "study"])
check("通勤稿比精读稿短", modes["commute"]["chars"] < modes["study"]["chars"], True)
check("逐句都带 bid/si 双层定位",
      all("bid" in it and "si" in it for it in mdp["items"]), True)
view_kinds = {b["kind"] for b in md.get("view") or []}
check("渲染视图保留 code / table", {"code", "table"} <= view_kinds, True)
code_bids = [b["bid"] for b in md.get("view") or [] if b["kind"] == "code"]
check("通勤档跳过所有代码块",
      all(not any(it["bid"] == bid for it in modes["commute"]["items"])
          for bid in code_bids), True)
check("精读档会读代码块",
      all(any(it["bid"] == bid for it in modes["study"]["items"])
          for bid in code_bids), True)
check("T& 不再被念成「和」", "的引用" in N.tech_inline("T& x;"), True)
check("-> 不再被念成「到」", "到" not in N.tech_inline("auto&& 与 ->"), True)
check("圈号 / 小节号转写",
      "一" in N.tech_inline("①") and "7.3" in N.tech_inline("§7.3")
      and "节" in N.tech_inline("§7.3"), True)
# 回归：<int> 曾被引擎当成 HTML 标签整段删掉，max<int> 只剩 max
check("模板尖括号不被当 HTML 标签吃掉",
      "int" in N.tech_inline("max<int>(a, b)") and "vector" in N.tech_inline("#include <vector>"), True)
check("小说路径不受影响", bool(N.prepare(novel_sample, markdown=False)["items"]), True)

print()
print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
if BAD:
    print("失败项：" + "、".join(BAD))
sys.exit(1 if BAD else 0)
