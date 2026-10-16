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

print("=== 4) 逐句切分（这是旧版最大的问题）===")
t = "他停顿了一下，说：“这样做是对的。”然后转身就走了。"
items = N.split_items("", blocks=[{"kind": "para", "text": t}])
check("引号内句号不被截断", [i["text"] for i in items], [t])

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

print("=== 5) 端到端 prepare ===")
p = N.prepare(raw)
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
sp = N.prepare(struct_raw)
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

print()
print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
if BAD:
    print("失败项：" + "、".join(BAD))
sys.exit(1 if BAD else 0)
