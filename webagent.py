# -*- coding: utf-8 -*-
"""网页取稿 Agent：让会调工具的小模型主导"把一个链接变成朗读稿"这件事。

为什么不让模型全程接管？
    让 qwen3:4b 从头到尾自己抓、自己洗，一次要 40 秒以上，而且它会改写正文。
    朗读场景要的是**原文**，改写等于毁掉内容 —— 这是不可接受的。

所以这里分两级：

    第一级（零模型开销，1~3 秒）
        直接用 webfetch 抓。抓得干净（质量分够高）就立刻返回。
        绝大多数链接走这条路，用户点一下就能听。

    第二级（才唤醒模型）
        抓不到、抓得差、或者用户明确要求"整理干净"时，交给模型决策：
        · 看诊断信息判断为什么失败，换策略重抓；
        · 头尾混着导航/推广/相关阅读时，让模型只挑出"要删的行号"。
        第二条尤其重要 —— 模型只输出行号，正文一个字都不经它的手，
        既干净又绝不会被改写。

返回里带 trace，前端可以把"模型做了什么"摊开给用户看。
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Optional

import requests

import webfetch

# 真正的句子收尾（不含省略号：链接被截断时也会以 … 结尾）
REAL_END_RE = webfetch.REAL_END_RE


# --------------------------------------------------------------------------
# 工具定义
# --------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "抓取一个网页的正文。返回结果里 ok 表示是否拿到可用正文，"
                "quality 是 0-100 的质量分，blocked 说明被什么拦住了，"
                "tried 记录试过哪些策略。抓得不好时可以换 mode 再抓一次。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http/https 链接"},
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "desktop", "mobile"],
                        "description": "auto=自动；desktop=桌面浏览器身份；mobile=手机浏览器身份",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clean_page",
            "description": (
                "清理第 idx 份稿子头尾残留的网页垃圾：导航条、面包屑、推广语、"
                "关注引导、相关文章标题、作者简介、版权声明、广告等。"
                "只返回要删除的行号，正文内容不会被改动。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "description": "稿子编号，来自 fetch_page 的返回"}
                },
                "required": ["idx"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "选定第 idx 份稿子作为最终朗读稿并结束任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "description": "稿子编号"},
                    "note": {"type": "string", "description": "一句话说明结果，给用户看"},
                },
                "required": ["idx"],
            },
        },
    },
]

SYS_AGENT = (
    "你是朗读助手的取稿员，唯一任务是把用户给的链接变成一份可以直接朗读的干净原文。\n"
    "\n"
    "可用工具：\n"
    "- fetch_page(url, mode)：抓取网页正文，返回质量分和诊断。\n"
    "- clean_page(idx)：删掉第 idx 份稿子头尾残留的网页垃圾（导航、推广语、"
    "关注引导、相关文章、作者简介、版权声明等）。\n"
    "- finish(idx, note)：选定最终稿子并结束。\n"
    "\n"
    "工作步骤：\n"
    "1) 用 fetch_page 抓取。\n"
    "2) 如果 ok 为 false 或 quality 低于 60，看 blocked 和 tried 判断原因，"
    "换一个 mode 再抓一次（总共最多抓两次）。\n"
    "3) 如果抓到的 head 或 tail 里明显混着导航、推广语、相关文章这类网页垃圾，"
    "调用 clean_page。\n"
    "4) 满意后调用 finish 结束。\n"
    "\n"
    "硬性约束：\n"
    "- 你绝不能改写、概括、翻译、删减正文内容，也不要输出正文本身。\n"
    "- 不要反复重抓。只要有一份稿子 ok 为 true 且 quality 不低于 60，就直接 finish。\n"
    "- 工具总共最多调用 4 次。\n"
    "- 每一轮都调用一个工具，工具调用就是你这一轮的全部回答。"
    # 这里原本写的是「只输出工具调用，不要输出解释性文字」—— 结果 qwen2.5:7b
    # 直接吐一个空回复（连 tool_calls 都没有），4 次里 0 次成功。实测换成上面
    # 这句肯定式说法后 4/4 成功。小模型对否定式指令的处理非常不可靠，别再写回去。
)


# --------------------------------------------------------------------------
# Ollama 通信
# --------------------------------------------------------------------------

def _chat(messages: list[dict], model: str, ollama_url: str, num_ctx: int,
          keep_alive: str, tools: Optional[list] = None,
          temperature: float = 0.2, json_mode: bool = False,
          timeout: float = 300.0) -> dict:
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if tools:
        payload["tools"] = tools
    if json_mode:
        payload["format"] = "json"
    # 不传 think 参数：实测给 qwen3 系列传 think=false 会把推理写进正文
    r = requests.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    msg = dict(r.json().get("message") or {})
    msg["content"] = re.sub(r"<think(ing)?>.*?</think(ing)?>", "",
                            msg.get("content") or "", flags=re.S | re.I).strip()
    return msg


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def extract_url(text: str) -> str:
    m = re.search(r"https?://[^\s<>\"'）)】\]]+", text or "")
    return m.group(0).rstrip("。，,.;；") if m else ""


# --------------------------------------------------------------------------
# Agent 主流程
# --------------------------------------------------------------------------

def run(url: str, model: str, ollama_url: str, num_ctx: int = 32768,
        keep_alive: str = "30m", auto: bool = True, do_clean: bool = False,
        fetch_timeout: float = 25.0, proxy: str = "auto",
        retry_on_fail: bool = False, ollama_ok: bool = True,
        use_login: bool = True, allow_browser: bool = True,
        on_event: Optional[Callable[[dict], None]] = None,
        max_tool_calls: int = 4) -> dict:
    """把链接抓成朗读稿。auto=True 时先走零模型开销的快路。

    `use_login` / `allow_browser` 交给 webfetch 决定要不要带登录态、
    要不要在规则抓取失败时动用本机浏览器。
    """
    t0 = time.time()
    trace: list[dict] = []
    state: dict = {"pages": [], "chosen": None, "note": ""}

    def emit(kind: str, **kw):
        ev = {"kind": kind, **kw}
        trace.append(ev)
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    def store(d: dict) -> int:
        state["pages"].append(d)
        return len(state["pages"]) - 1

    def brief(idx: int, d: dict) -> dict:
        """给模型看的摘要 —— 只给头尾，别把整篇正文塞进上下文。"""
        if not d.get("ok"):
            return {"idx": idx, "ok": False, "error": d.get("error"),
                    "blocked": d.get("blocked"),
                    "tried": (d.get("diag") or {}).get("tried")}
        txt = d.get("text") or ""
        return {"idx": idx, "ok": True, "title": d.get("title", ""),
                "chars": d.get("chars"), "paras": d.get("paras"),
                "quality": d.get("quality"), "grade": d.get("grade"),
                "route": d.get("route"), "junk_lines_removed": d.get("dropped"),
                "head": txt[:280], "tail": txt[-220:]}

    # ---------- 第一级：确定性抓取 ----------
    first = webfetch.fetch(url, mode="auto", timeout=fetch_timeout, proxy=proxy,
                           login=use_login, allow_browser=allow_browser)
    idx0 = store(first)
    emit("fetch", tool="deterministic", mode="auto",
         ok=bool(first.get("ok")), quality=first.get("quality"),
         chars=first.get("chars"), route=first.get("route"),
         error=first.get("error"))
    # 需要登录态的站点（知乎最典型）应该在抓取阶段就把话说明白，
    # 别让用户以为"这站抓不了"。
    if first.get("need_login"):
        emit("need_login",
             reason="这个站要登录态才给正文",
             hint="点页面上的「登录态」→「从浏览器导入」，一次配好长期有效")
    good = bool(first.get("ok")) and (first.get("quality") or 0) >= 65
    if auto and good and not do_clean:
        return _result(first, trace, t0, "direct", note="", model=None)

    # 第一级**彻底**失败（一个字节正文都没拿到）时，别叫模型 —— 它帮不上忙。
    # auto 模式已经把 desktop / mobile 两种身份都试过一遍了，模型能做的
    # 也只是"再抓一次"，撞的是同一堵墙。实测：白烧 39 秒、产出 0 字。
    # 想让它硬试，调用方传 retry_on_fail=True。
    if not first.get("ok") and not retry_on_fail:
        emit("skip_model",
             reason=("缺登录态，重抓也是同一堵墙" if first.get("need_login")
                     else "网络层没拿到内容，重抓是同一堵墙"))
        return _result(first, trace, t0, "direct", note="", model=None)

    # Ollama 没在跑：别报错，把确定性抓到的结果照样交出去。
    # 抓得干净时本来就用不着模型，用户不该因为这个听不了文章。
    if not ollama_ok:
        emit("skip_model", reason="Ollama 未运行，跳过模型整理")
        return _result(first, trace, t0, "direct", note="", model=None)

    # 用户只是要"深度清理"，而稿子已经在手里 —— 不用绕工具回路：
    # 直接调 clean_page。清理本来就是一个单步动作，让模型先"决定去抓"再"决定
    # 去清"纯属浪费一轮，还多一个翻车点（实测那轮会偶发空回复）。
    if do_clean and auto and first.get("ok"):
        emit("clean_start", reason="稿子已到手，直接进深度清理")
        _clean_page(state, 0, model, ollama_url, num_ctx, keep_alive, emit)
        return _result(state["pages"][0], trace, t0, "agent",
                       note="已清掉头尾的网页垃圾", model=model)

    # ---------- 第二级：模型介入 ----------
    def call_fetch(a: dict) -> dict:
        mode = a.get("mode") or "auto"
        if mode not in ("auto", "desktop", "mobile"):
            mode = "auto"
        d = webfetch.fetch(a.get("url") or url, mode=mode,
                           timeout=fetch_timeout, proxy=proxy,
                           login=use_login, allow_browser=allow_browser)
        i = store(d)
        emit("fetch", tool="fetch_page", mode=mode, idx=i, ok=bool(d.get("ok")),
             quality=d.get("quality"), chars=d.get("chars"), route=d.get("route"),
             error=d.get("error"))
        # 同一个错误连撞两次就别再撞了（模型有时会固执地重试同一个 mode）
        if not d.get("ok"):
            err = str(d.get("error") or "")
            if err and err == state.get("_last_fetch_err"):
                state["_fail_streak"] = state.get("_fail_streak", 1) + 1
            else:
                state["_fail_streak"] = 1
            state["_last_fetch_err"] = err
        else:
            state["_fail_streak"] = 0
        return brief(i, d)

    def call_clean(a: dict) -> dict:
        i = int(a.get("idx") or 0)
        if not (0 <= i < len(state["pages"])):
            return {"ok": False, "error": f"没有编号为 {i} 的稿子"}
        return _clean_page(state, i, model, ollama_url, num_ctx, keep_alive, emit)

    def call_finish(a: dict) -> dict:
        i = int(a.get("idx") or 0)
        if not (0 <= i < len(state["pages"])):
            return {"ok": False, "error": f"没有编号为 {i} 的稿子"}
        state["chosen"] = i
        state["note"] = (a.get("note") or "").strip()
        emit("finish", tool="finish", idx=i, note=state["note"])
        return {"ok": True}

    handlers = {"fetch_page": call_fetch, "clean_page": call_clean, "finish": call_finish}

    user_msg = f"把这个链接抓成可以朗读的稿子：{url}"
    if not first.get("ok"):
        user_msg += (f"\n\n（系统已经先试过一次，失败了。诊断："
                     f"{json.dumps(brief(idx0, first), ensure_ascii=False)[:500]}）")
    elif not good:
        user_msg += (f"\n\n（系统已经先试过一次，质量分只有 {first.get('quality')}，"
                     f"摘要：{json.dumps(brief(idx0, first), ensure_ascii=False)[:600]}）")
    if do_clean:
        user_msg += "\n\n另外：抓完请务必用 clean_page 把头尾的网页垃圾清一遍。"

    messages = [{"role": "system", "content": SYS_AGENT},
                {"role": "user", "content": user_msg}]

    used = 0
    for _round in range(max_tool_calls + 1):
        # 小模型偶尔会吐一个彻底的空回复（既没文字也没工具调用）——采样是随机的，
        # 原地重试一次基本就能出结果，比直接放弃划算得多。
        msg: dict = {}
        for attempt in range(2):
            try:
                msg = _chat(messages, model, ollama_url, num_ctx, keep_alive,
                            tools=TOOLS,
                            temperature=0.2 + 0.2 * attempt)
            except Exception as e:
                emit("error", where="model", message=f"{type(e).__name__}: {str(e)[:150]}")
                msg = {}
                break
            if (msg.get("tool_calls") or []) or (msg.get("content") or "").strip():
                break
            emit("retry", round=_round + 1, attempt=attempt + 1,
                 reason="模型这一轮什么都没输出，重发一次")

        if not msg:
            break

        calls = msg.get("tool_calls") or []
        if not calls:
            emit("model_text", text=(msg.get("content") or "")[:200])
            break

        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": calls})
        for c in calls:
            fn = (c.get("function") or {})
            name = fn.get("name") or ""
            args = _parse_args(fn.get("arguments"))
            used += 1
            h = handlers.get(name)
            if not h:
                out = {"ok": False, "error": f"没有这个工具：{name}"}
            else:
                try:
                    out = h(args)
                except Exception as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(out, ensure_ascii=False)})
        if state["chosen"] is not None or used >= max_tool_calls:
            break
        if state.get("_fail_streak", 0) >= 2:
            emit("stop", reason="连续两次抓取报同一个错，提前收手")
            break

    # ---------- 收尾：模型没 finish 也要给结果 ----------
    idx = state["chosen"]
    if idx is None:
        ok_pages = [(p.get("quality") or 0, i)
                    for i, p in enumerate(state["pages"]) if p.get("ok")]
        if ok_pages:
            idx = max(ok_pages)[1]
            emit("fallback", picked=idx, reason="模型未给出结论，按质量分自动选择")
        elif state["pages"]:
            idx = 0
        else:
            return {"ok": False, "error": "没有可用的稿子", "trace": trace,
                    "model": model, "elapsed": round(time.time() - t0, 1)}

    return _result(state["pages"][idx], trace, t0, "agent",
                   note=state["note"], model=model)


def _clean_page(state: dict, idx: int, model: str, ollama_url: str, num_ctx: int,
                keep_alive: str, emit: Callable) -> dict:
    """让模型只挑"要删的行号"，正文一个字都不经它的手。"""
    page = state["pages"][idx]
    if not page.get("ok"):
        return {"ok": False, "error": "这一份没有可用正文"}
    lines = [ln for ln in (page.get("text") or "").split("\n")]
    if len(lines) < 4:
        return {"ok": False, "error": "内容太少，没什么可清的"}

    HEAD_N, TAIL_N = 16, 30
    if len(lines) <= HEAD_N + TAIL_N:
        sel = list(range(len(lines)))
    else:
        sel = list(range(HEAD_N)) + list(range(len(lines) - TAIL_N, len(lines)))
    # 截断别太狠：砍到 88 字时模型经常把"半句话"当成垃圾（真实正文段落被腰斩后
    # 看起来确实不像正常句子）。给足 120 字，并在被截断的行尾加个记号。
    def clip(s: str, n: int = 120) -> str:
        return s if len(s) <= n else s[:n] + "…"

    listing = "\n".join(f"{i}| {clip(lines[i])}" for i in sel)

    sys = (
        "你是网页正文的清理员。下面是一篇稿子开头和结尾的若干行，每行格式是「行号| 内容」。\n"
        "请找出其中**不属于正文**的行：导航条、面包屑、页面菜单、推广语、"
        "关注/点赞/转发引导、相关文章或推荐阅读的标题、作者简介、版权声明、"
        "广告、二维码提示、编辑校对署名、评论、下载 App 提示。\n"
        "规则：\n"
        "1) 正文的句子一律保留。**特别提醒：读起来是完整论述、信息量正常的句子，"
        "即使带一点推广口吻也可能是原文的一部分，宁可留着；**\n"
        "2) 只输出 JSON，形如 {\"drop\": [12, 13, 45]}；\n"
        "3) 没有要删的就输出 {\"drop\": []}。"
    )
    try:
        out = _chat([{"role": "system", "content": sys},
                     {"role": "user", "content": listing}],
                    model, ollama_url, num_ctx, keep_alive,
                    json_mode=True, temperature=0.0)
        data = json.loads(out.get("content") or "{}")
        drop = {int(x) for x in (data.get("drop") or [])
                if isinstance(x, (int, float, str)) and str(x).lstrip("-").isdigit()}
    except Exception as e:
        emit("clean", idx=idx, ok=False, error=f"{type(e).__name__}: {str(e)[:120]}")
        return {"ok": False, "error": "清理失败"}

    # 只允许删开头那段和结尾那段里的行，中间正文碰都不碰
    allowed = set(sel)
    drop &= allowed

    # 硬保险：又长又以句末标点收尾的行一定是正文段落。
    # 误删正文是朗读工具最不可接受的失败 —— 宁可少删，也不能吃掉内容。
    guarded = {i for i in drop
               if len(lines[i].strip()) >= 40 and REAL_END_RE.search(lines[i].strip())}
    if guarded:
        drop -= guarded
    if len(drop) > max(3, len(sel) * 0.5):
        emit("clean", idx=idx, ok=False, error="模型想删的行太多，判定为误判，已忽略")
        return {"ok": False, "error": "清理结果不可信，已跳过"}

    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    page["text"] = "\n".join(kept)
    page["chars"] = len(re.sub(r"\s", "", page["text"]))
    page["paras"] = len([ln for ln in kept if ln.strip()])
    emit("clean", idx=idx, ok=True, removed=len(drop),
         protected=len(guarded))
    return {"ok": True, "idx": idx, "removed": len(drop), "protected": len(guarded),
            "head": page["text"][:240], "tail": page["text"][-180:]}


def _result(page: dict, trace: list, t0: float, path: str, note: str,
            model: Optional[str]) -> dict:
    if not page.get("ok"):
        return {"ok": False, "error": page.get("error") or "没能抓到正文",
                "blocked": page.get("blocked"), "title": page.get("title", ""),
                "need_login": bool(page.get("need_login")),
                "trace": trace, "path": path, "model": model,
                "elapsed": round(time.time() - t0, 1),
                "diag": page.get("diag")}
    return {"ok": True, "title": page.get("title", ""), "text": page.get("text", ""),
            "url": page.get("url"), "chars": page.get("chars"),
            "paras": page.get("paras"), "quality": page.get("quality"),
            "grade": page.get("grade"), "route": page.get("route"),
            "dropped": page.get("dropped"), "note": note,
            "trace": trace, "path": path, "model": model,
            "elapsed": round(time.time() - t0, 1)}


if __name__ == "__main__":  # 手动调试：python webagent.py <url> [model]
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://sspai.com/post/86000"
    m = sys.argv[2] if len(sys.argv) > 2 else "qwen3:4b"
    d = run(u, model=m, ollama_url="http://127.0.0.1:11434", do_clean=True, auto=False)
    print(json.dumps({k: v for k, v in d.items() if k != "text"},
                     ensure_ascii=False, indent=2))
    print("--- 正文 ---")
    print((d.get("text") or "")[:1200])
