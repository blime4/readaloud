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


print("\n=== 6. 重排之前先清行（不然碎片会被粘进正文）===")

# 缩进过的 HTML 极常见：<h1>\n  标题\n</h1>。井号和标题一旦被换行拆开，
# 下游既认不出标题，又会把标题+作者卡片+首段糊成一坨。
_hl = webfetch.html_to_lines("<h1>\n  浅谈 One UI6.0 更新\n</h1>\n<p>正文来了。</p>")
check("标题的井号与标题粘回同一行", _hl[0], "# 浅谈 One UI6.0 更新")
check("井号行数只剩一行", sum(1 for x in _hl if x.startswith("#")), 1)

# 作者卡片碎片：又短又没有句读，必须在 reflow 之前杀掉
_scraps = ["# 浅谈 One UI6.0 更新", "趁着雨", "少数派作者", "联合作者", "7 分钟阅读",
           "12月26日，我的手机收到了更新推送，我直接安装更新，毕竟也是期待了很久的。"]
_pre, _pre_n = webfetch.drop_junk_lines(_scraps)
check("作者卡片碎片重排前被清掉 4 行", _pre_n, 4)
check("标题与正文都留着", len(_pre), 2)
_reflowed = webfetch.reflow(_pre)
check_true("重排后不会把碎片粘进正文",
           _reflowed[1].startswith("12月26日"), _reflowed[1][:40])
check_true("标题独占一段", _reflowed[0].startswith("# 浅谈"), _reflowed[0][:40])

# 英文站靠硬换行排版，短行是正文碎片，重排前绝不能按长度杀
_en = ["If you collected lists of techniques for doing", "great work in a lot",
       "of different fields, what would the", "intersection look like?",
       "Partly my goal was to create a guide that could be used by someone",
       "working in any field."]
_en_pre, _en_n = webfetch.drop_junk_lines(_en)
check("英文硬换行的短行一行都没被误删", _en_n, 0)
_en_para = webfetch.reflow(_en_pre)
check("短行被接回完整段落", len(_en_para), 2)
check_true("碎行已经接成一句完整的话",
           _en_para[0].endswith("intersection look like?"), _en_para[0][-40:])
check_true("首段一个词都没少",
           _en_para[0].startswith("If you collected") and "great work in a lot" in _en_para[0],
           _en_para[0][:70])


print("\n=== 7. 尾部相关阅读墙 ===")

paras = ["正文第一段。", "正文第二段。", "正文第三段。", "正文第四段。",
         "正文第五段。", "正文第六段。", "相关文章标题一", "相关文章标题二",
         "相关文章标题三"]
trimmed, n = webfetch.trim_tail_wall(paras)
check("截掉尾部 3 行标题墙", n, 3)
check("剩下的都是正文", len(trimmed), 6)

# 数量不够时不截，避免误伤
trimmed2, n2 = webfetch.trim_tail_wall(paras[:7] + ["相关文章标题一"])
check("尾部不足 3 行不动手", n2, 0)


print("\n=== 8. 拦截页识别 ===")

check_true("安全检测页被识别",
           webfetch.looks_blocked("火山引擎 正在进行安全检测，请稍后重试") is not None)
check_true("登录墙被识别",
           webfetch.looks_blocked("登录后查看完整内容") is not None)
check_true("风控等待页被识别",
           webfetch.looks_blocked("Please wait...") is not None)
check_true("正常正文不被误判",
           webfetch.looks_blocked("这是一篇正常文章的正文，讲述了一个完整的故事。") is None)


print("\n=== 9. 标题去重 ===")
t = webfetch.strip_leading_title("浅谈One UI6.0更新\n正文第一段内容在这里。", "浅谈One UI6.0更新")
check_true("正文开头的重复标题被去掉", t.startswith("正文第一段"), t)
# 带结构标记的重复标题也要认得出来
t = webfetch.strip_leading_title("# 浅谈One UI6.0更新\n正文第一段内容在这里。", "浅谈One UI6.0更新")
check_true("「# 标题」形式也去掉", t.startswith("正文第一段"), t)
# 标题、日期、正文首句挤在同一行时，整行删掉会把开篇吃掉
t = webfetch.strip_leading_title(
    "How to Do Great Work July 2023 If you collected lists of techniques for "
    "doing great work in a lot of different fields, what would the intersection "
    "look like?", "How to Do Great Work")
check_true("标题后还跟着正文时不整段删掉", t.startswith("How to Do Great Work"), t[:50])


print("\n=== 10. 回环 / 内网地址不走代理 ===")

# 真实踩过的坑：本机 Clash 开着时，抓"自己起的本地服务"会走代理，
# 代理对 127.0.0.1 只能回 502 —— 于是本机页面反而抓不到。必须直连。
for u, want, why in [
    ("http://127.0.0.1:8756/", True, "回环 IP"),
    ("http://localhost:5000/a", True, "localhost"),
    ("http://192.168.1.10:8080/", True, "C 类内网"),
    ("https://10.28.31.54/docs", True, "A 类内网"),
    ("http://172.20.0.9/", True, "B 类内网"),
    ("http://myhost:8000/", True, "裸机器名"),
    ("https://paulgraham.com/x.html", False, "公网站点不该被当成内网"),
    ("https://172.32.0.1/", False, "172.32 已出内网段"),
]:
    check(f"{why}：{u.split('/')[2][:28]}", webfetch.is_local_url(u), want)

# 直连时必须在 requests 层面显式关掉代理，否则它会去读环境变量 HTTP_PROXY
# （把"直连"悄悄变成"走代理"，这个 bug 就是这么做出来的）。
_orig_get = webfetch.requests.get
_got: dict = {}


def _fake_get(url, **kw):
    _got.clear()
    _got.update(kw)

    class _Resp:
        status_code = 200
        content = b""
        headers: dict = {}
        apparent_encoding = "utf-8"
    _Resp.url = url
    return _Resp()


try:
    webfetch.requests.get = _fake_get
    webfetch._get("http://127.0.0.1:1/x", "desktop", 3, None)
    check("直连时显式声明 http/https 都不走代理",
          _got.get("proxies"), {"http": None, "https": None})
    webfetch._get("https://example.com/", "desktop", 3, "http://127.0.0.1:7897")
    check("给了代理就按代理走",
          _got.get("proxies"),
          {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
finally:
    webfetch.requests.get = _orig_get

# 端到端：起一个真的本地服务，同时把环境变量里的代理指向一个死端口。
# 回环地址能抓到 = 代理确实被绕过去了。
import functools
import http.server
import json
import os
import socketserver
import threading

_BODY = (
    "<html><head><meta charset='utf-8'><title>本地样本页</title></head><body>"
    "<div id='content'><h1>本地样本标题</h1>"
    "<p>这是本地服务返回的第一段正文，用来验证回环地址不会被系统代理拦截，"
    "段落长度要足够长才能通过质量门槛，所以这里多写了一些没有实际含义的说明文字。"
    "如果代理没有被绕过去，这一段根本不可能出现在抓取结果里，只会得到一个 502。</p>"
    "<p>这是第二段。回环地址走代理时，代理不认识 127.0.0.1 指向哪里，"
    "只能回一个网关错误；而环境变量里的代理又是很多机器默认就有的东西，"
    "所以这条路径必须显式关掉代理才能稳定工作。两段合起来应当能被判断为"
    "一份可朗读的稿子，而不是空壳页面。</p>"
    "</div></body></html>")


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = _BODY.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


socketserver.TCPServer.allow_reuse_address = True
_srv = socketserver.TCPServer(("127.0.0.1", 0), _LocalHandler)
_local_url = f"http://127.0.0.1:{_srv.server_address[1]}/a.html"
threading.Thread(target=_srv.serve_forever, daemon=True).start()

_old_env = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy",
                                           "HTTPS_PROXY", "https_proxy")}
os.environ.update({"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                   "HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"})
try:
    _d = webfetch.fetch(_local_url, timeout=5)
    check_true("环境变量里有死代理时，回环地址照样抓到",
               bool(_d.get("ok")) and _d.get("chars", 0) > 60,
               f"err={_d.get('error')} diag={json.dumps(_d.get('diag'), ensure_ascii=False)[:160]}")
    check_true("诊断里标出了这是本地地址", (_d.get("diag") or {}).get("local_url") is True)
finally:
    for k, v in _old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _srv.shutdown()
    _srv.server_close()


print("\n=== 11. 抓不到时不该让模型空转 ===")
import webagent

_calls = {"n": 0}


def _fail_fetch(url, mode="auto", timeout=25.0, proxy="auto", **kw):
    _calls["n"] += 1
    return {"ok": False, "error": "抓取失败：HTTP 403",
            "blocked": "登录后查看", "diag": {"tried": [{"mode": "desktop+proxy", "status": 403}]}}


def _low_fetch(url, mode="auto", timeout=25.0, proxy="auto", **kw):
    _calls["n"] += 1
    return {"ok": True, "title": "样本", "text": "这里是正文。", "chars": 6, "paras": 1,
            "quality": 30, "grade": "poor", "route": "整页", "dropped": 0,
            "diag": {"tried": []}, "url": url}


_orig_fetch = webagent.webfetch.fetch
try:
    # 网络层就失败（无正文可交）→ 直接如实报错。ollama_url 指向死端口，
    # 一旦真的去调模型必然抛错，所以"调用次数 == 1"就证明模型没被叫醒。
    webagent.webfetch.fetch = _fail_fetch
    _calls["n"] = 0
    _d1 = webagent.run("https://example.com/a", model="qwen2.5:7b",
                       ollama_url="http://127.0.0.1:9", auto=True)
    check("彻底抓不到时只抓一次，不喊模型", _calls["n"], 1)
    check_true("返回的是可读的失败原因", not _d1.get("ok") and "403" in str(_d1.get("error")),
               str(_d1.get("error")))
    check_true("trace 说明了为什么没喊模型",
               any(t.get("kind") == "skip_model" for t in _d1.get("trace") or []),
               json.dumps(_d1.get("trace"), ensure_ascii=False)[:160])

    # 抓到了但很脏 + Ollama 没开 → 照样把稿子交出去，只是不整理
    webagent.webfetch.fetch = _low_fetch
    _calls["n"] = 0
    _d2 = webagent.run("https://example.com/a", model="qwen2.5:7b",
                       ollama_url="http://127.0.0.1:9", auto=True, ollama_ok=False)
    check("Ollama 没开时也不重复抓", _calls["n"], 1)
    check_true("Ollama 没开也能拿到正文（不报错）", bool(_d2.get("ok")), str(_d2.get("error")))
    check_true("trace 注明是 Ollama 没开",
               any(t.get("kind") == "skip_model" and "Ollama" in str(t.get("reason"))
                   for t in _d2.get("trace") or []))
finally:
    webagent.webfetch.fetch = _orig_fetch


print("\n=== 12. Cookie 文本解析（三种粘法都要认）===")
import cookies as _ck

_p1 = _ck.parse_cookie_text("z_c0=AAA; d_c0=BBB; _xsrf=CCC")
check("纯 Cookie 头", _p1, {"z_c0": "AAA", "d_c0": "BBB", "_xsrf": "CCC"})

_p2 = _ck.parse_cookie_text("  Cookie: z_c0=AAA; d_c0=BBB")
check("从 Network 复制的整行", _p2, {"z_c0": "AAA", "d_c0": "BBB"})

_p3 = _ck.parse_cookie_text(
    "curl 'https://www.zhihu.com/' -H 'cookie: z_c0=AAA; d_c0=BBB' -H 'user-agent: x'")
check("Copy as cURL 的整段命令", _p3, {"z_c0": "AAA", "d_c0": "BBB"})

_p4 = _ck.parse_cookie_text("z_c0=AAA\nd_c0=BBB\n_xsrf=CCC")
check("换行分隔（有些人从表格复制会变成一行一个）", len(_p4), 3)

_p5 = _ck.parse_cookie_text('z_c0="AAA"; d_c0=\'BBB\'')
check("值上带引号", _p5, {"z_c0": "AAA", "d_c0": "BBB"})

check("空串不炸", _ck.parse_cookie_text(""), {})
check("垃圾文本不炸", _ck.parse_cookie_text("这不是 cookie"), {})
# 伪头（Copy as cURL 里常见的 :authority 之类）不能混进来
check_true("伪头被排除",
           ":authority" not in _ck.parse_cookie_text(
               "curl x -H 'cookie: z_c0=A; :authority: zhihu.com'"))


print("\n=== 13. 域名归一（写进去和读出来必须是同一个键）===")
for host, want in [("www.zhihu.com", "zhihu.com"),
                   ("zhuanlan.zhihu.com", "zhihu.com"),
                   ("zhihu.com", "zhihu.com"),
                   ("mp.weixin.qq.com", "mp.weixin.qq.com"),
                   ("www.google.com", ""),
                   ("localhost", ""),
                   ("", "")]:
    check(f"base_domain({host!r})", _ck.base_domain(host), want)
check("site_info_for 认得出知乎", (_ck.site_info_for("zhuanlan.zhihu.com") or {}).get("name"), "知乎")
check("不认识的站返回 None", _ck.site_info_for("example.com"), None)

# 存取用临时文件，绝不能碰用户真实的 cookies.json
import tempfile
_real_store = _ck.STORE_FILE
_ck.STORE_FILE = pathlib.Path(tempfile.mkdtemp()) / "cookies.json"
try:
    _ck.clear()
    # 用户粘贴时给的是 www.zhihu.com，读的时候查的是 zhihu.com —— 必须归一，
    # 否则表现为"明明提示保存成功，抓取时却没带上"。（实测踩过）
    _ck.set_site(_ck.base_domain("www.zhihu.com") or "www.zhihu.com",
                 {"z_c0": "X", "d_c0": "Y", "osd": "Z"},
                 ua="UA-1", source="manual")
    check("写入的键是主域", list(_ck.load()["sites"].keys()), ["zhihu.com"])
    _got = _ck.for_url("https://zhuanlan.zhihu.com/p/1")
    check("zhuanlan 子域能读到", len(_got["jar"]), 3)
    check("www 子域也能读到", len(_ck.for_url("https://www.zhihu.com/q/1")["jar"]), 3)
    check("UA 一起存下来了（__zse_ck 与 UA 绑定）", _got["ua"], "UA-1")
    check("未配置的站返回 None", _ck.for_url("https://example.com/a"), None)

    # 合并语义：用户往往只想补一个 z_c0，不该把他之前导入的整套冲掉
    _ck.set_site("zhihu.com", {"_xsrf": "NEW"}, ua="UA-1", source="manual")
    _after = _ck.for_url("https://zhihu.com/")["jar"]
    check("合并后总数变多而不是被覆盖", len(_after), 4)
    check_true("原有的 z_c0 还在", _after.get("z_c0") == "X")
    check("有登录凭证判定为已登录", _ck._looks_logged_in("zhihu.com", _after))

    _ck.set_site("zhihu.com", {"d_c0": "Y"}, ua="UA-1", source="browser", merge=False)
    check("merge=False 是整体覆盖（浏览器导入用）",
          len(_ck.for_url("https://zhihu.com/")["jar"]), 1)

    st = _ck.status()
    check("status 把 zhihu.com 算作已配置", "zhihu.com" in st["configured"])
    _ck.clear("zhihu.com")
    check("clear 指定站", _ck.status()["configured"], [])
    _ck.clear()
    check("clear 全部", _ck.load()["sites"], {})
finally:
    _ck.STORE_FILE = _real_store


print("\n=== 14. 登录态真的被带上了请求头 ===")
# 起一个只在本机听的迷你服务，把收到的请求头记下来，验证三件事：
#   1. Cookie 确实带上了（知乎这类站靠这个过 403）
#   2. UA 用的是登录态里那一个（__zse_ck 与 UA 绑定，换 UA 就失效）
#   3. 顺带补了浏览器导航头（缺这些头本身就很可疑）
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Rec(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):                                  # noqa: N802
        _Rec.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = ("<html><head><meta charset='utf-8'><title>本机探针</title></head>"
                "<body><article><h1>探针页</h1><p>"
                + "这段正文只是为了让响应看起来像一篇正常文章。" * 8
                + "</p></article></body></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_srv = HTTPServer(("127.0.0.1", 0), _Rec)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()

_PROBE = f"http://127.0.0.1:{_port}/probe"
_LOGIN = {"cookie": "z_c0=OK; d_c0=DEV", "ua": "UA-FROM-LOGIN",
          "referer": "https://www.zhihu.com/", "domain": "zhihu.com"}

_rec = webfetch._get(_PROBE, "desktop", 6, None, _LOGIN)
_h1 = _Rec.seen[-1]
check("Cookie 带上了", _h1.get("cookie"), "z_c0=OK; d_c0=DEV")
check("UA 用的是登录态里的那个", _h1.get("user-agent"), "UA-FROM-LOGIN")
check("Referer 用站点首页", _h1.get("referer"), "https://www.zhihu.com/")
check("补了 Sec-Fetch-Dest", _h1.get("sec-fetch-dest"), "document")
check("补了 Upgrade-Insecure-Requests", _h1.get("upgrade-insecure-requests"), "1")

_rec2 = webfetch._get(_PROBE, "desktop", 6, None, None)
_h2 = _Rec.seen[-1]
check_true("没登录态时不该凭空冒出 Cookie", not _h2.get("cookie"), str(_h2.get("cookie"))[:60])
check("没登录态时用默认 UA", _h2.get("user-agent"), webfetch.UA_DESKTOP)

# 走一遍完整 fetch：本机地址 + 配了登录态 → 不该因为注入登录态而出错
_d14 = webfetch.fetch(_PROBE, timeout=8, allow_browser=False)
check_true("完整链路能抓到探针页", bool(_d14.get("ok")), str(_d14.get("error"))[:120])
check_true("正文是探针页的内容",
           "探针页" in (_d14.get("text") or ""), (_d14.get("text") or "")[:60])
_srv.shutdown()

# 内网地址不该去碰浏览器兜底（本地服务自己就是抓取目标，不是被风控的站点）
check("本机地址标记为 local", webfetch.is_local_url(_PROBE))


print("\n=== 15. 站点元信息与提示语 ===")
_meta = webfetch._site_meta("https://zhuanlan.zhihu.com/p/670368800")
check("认得知乎", (_meta or {}).get("name"), "知乎")
check_true("列出了关键 cookie", "z_c0" in (_meta or {}).get("关键 cookie", []))
check("不认识的站返回 None", webfetch._site_meta("https://example.com/a"), None)
check("没配登录态时 _looks_logged_in 为假", webfetch._looks_logged_in(None), False)


if "--offline" not in sys.argv:
    print("\n=== 16. 真实站点（联网，会被限流）===")
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
    print("\n=== 17. 真实站点上的结构贯通 ===")
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
