# -*- coding: utf-8 -*-
import ctypes, json, sys, time, urllib.request
sys.path.insert(0, r"C:/Users/bo/WorkBuddy/2026-10-13-23-31-19/readaloud")
import ra_common as R

# --- 1) 用 Win32 设置剪贴板 ---
u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
GMEM_MOVEABLE, CF_UNICODETEXT = 0x0042, 13
s = "测试剪贴板朗读功能，这是一段用于验证的文字。"
buf = ctypes.create_unicode_buffer(s)
size = ctypes.sizeof(buf)
h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
p = k32.GlobalLock(h)
ctypes.memmove(p, buf, size)
k32.GlobalUnlock(h)
u32.OpenClipboard(None); u32.EmptyClipboard(); u32.SetClipboardData(CF_UNICODETEXT, h); u32.CloseClipboard()
print("1) 剪贴板读取 ->", repr(R.read_clipboard()))

# --- 2) 服务健康 ---
print("2) 服务健康 ->", R.health())

# --- 3) 广播（当前无页面，应为 0） ---
print("3) broadcast delivered ->", R.broadcast("广播测试"))

# --- 4) 热键注册能力 ---
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 1, 2, 0x4000
ok = u32.RegisterHotKey(None, 99, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("R"))
print("4) 注册 Ctrl+Alt+R ->", bool(ok), "(1=成功)")
if ok:
    u32.UnregisterHotKey(None, 99)

# --- 5) 端到端：prepare(none) + tts 首段 ---
body = json.dumps({"text": "# 标题\n\n这是一段**中文**测试，用来验证 30% 的流程。", "ai": "none"}).encode()
req = urllib.request.Request("http://127.0.0.1:8756/api/prepare", data=body,
                             headers={"Content-Type": "application/json"})
d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
print("5) prepare ->", d["count"], "句 |", d["segments"])
import urllib.parse
u = "http://127.0.0.1:8756/api/tts?" + urllib.parse.urlencode({"text": d["segments"][0], "voice": "zh-CN-XiaoxiaoNeural"})
r = urllib.request.urlopen(u, timeout=40)
print("6) tts 首段 ->", r.status, r.headers.get("Content-Type"), len(r.read()), "bytes")

# --- 7) 回归：韵律参数缺正号的写法也必须能合成 ---
# 背景：edge-tts 只认 "+0%" / "+0Hz"，写成 "0%" 会直接报 Invalid volume。
# 网页端滑块取到 0 时正好会漏掉正号，而默认音调/音量就是 0 —— 一踩就是
# 「页面显示已连接，但每一句都播不出来」。这里把四种写法都锁住。
for tag, q in [
    ("缺正号", {"rate": "0%", "pitch": "0Hz", "volume": "0%"}),
    ("标准",   {"rate": "+8%", "pitch": "+0Hz", "volume": "+0%"}),
    ("负数",   {"rate": "-5%", "pitch": "-2Hz", "volume": "50%"}),
    ("空值",   {"rate": "", "pitch": "", "volume": ""}),
]:
    u = "http://127.0.0.1:8756/api/tts?" + urllib.parse.urlencode(
        {"text": "回归测试", "voice": "zh-CN-XiaoxiaoNeural", **q})
    r = urllib.request.urlopen(u, timeout=40)
    ctype, n = r.headers.get("Content-Type"), len(r.read())
    good = r.status == 200 and ctype.startswith("audio/")
    print(f"7) tts[{tag}] ->", r.status, ctype, n, "bytes", "OK" if good else "!! 异常")
    if not good:
        sys.exit(1)
