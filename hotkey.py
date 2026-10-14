# -*- coding: utf-8 -*-
"""
ReadAloud 全局热键守护（纯 ctypes，无第三方依赖，无需管理员权限）

    Ctrl + Alt + R   朗读剪贴板
    Ctrl + Alt + S   总结剪贴板并朗读
    Ctrl + Alt + W   润色剪贴板并朗读
    Ctrl + Alt + X   打开朗读工作台
    Ctrl + Alt + Q   停止朗读

说明：文本会先推给已打开的朗读页面；没有页面时自动打开一个再朗读。
"""

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

from ra_common import health, log, open_player, post, read_clipboard, speak

PID_FILE = Path(__file__).resolve().parent / ".hotkey.pid"

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

HOTKEYS = {
    1: (ord("R"), "朗读剪贴板"),
    2: (ord("S"), "总结剪贴板并朗读"),
    3: (ord("W"), "润色剪贴板并朗读"),
    4: (ord("X"), "打开朗读工作台"),
    5: (ord("Q"), "停止朗读"),
}

_ai = {1: "none", 2: "summarize", 3: "polish"}


def handle(hotkey_id: int) -> None:
    if hotkey_id in _ai:
        text = read_clipboard().strip()
        if not text:
            log("剪贴板为空，忽略")
            return
        log(f"[{HOTKEYS[hotkey_id][1]}] {len(text)} 字 -> {_ai[hotkey_id]}")
        try:
            speak(text, _ai[hotkey_id])
        except Exception as e:
            log(f"朗读失败: {e}")
    elif hotkey_id == 4:
        log("打开朗读工作台")
        open_player()
    elif hotkey_id == 5:
        try:
            post("/api/broadcast", {"stop": True, "text": ""})
            log("已发送停止指令")
        except Exception as e:
            log(f"停止失败: {e}")


def main() -> int:
    # 单实例：重复启动直接退出（并保留原有守护）
    pid_file = PID_FILE
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
            h = user32.OpenProcess(0x1000, False, old)   # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                ctypes.windll.kernel32.CloseHandle(h)
                if code.value == 259:                    # STILL_ACTIVE
                    log(f"已有守护进程在运行 (pid {old})，本次退出")
                    return 0
        except Exception:
            pass
    pid_file.write_text(str(ctypes.windll.kernel32.GetCurrentProcessId()), encoding="utf-8")

    registered = 0
    for hid, (vk, name) in HOTKEYS.items():
        if user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
            registered += 1
            log(f"热键已注册: Ctrl+Alt+{chr(vk)}  ->  {name}")
        else:
            log(f"热键注册失败（可能被占用）: Ctrl+Alt+{chr(vk)}")
    if not registered:
        log("没有任何热键注册成功，退出")
        return 1
    if not health():
        log("提示：朗读服务当前未运行，按热键时会提示启动")
    log("热键守护已启动")
    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                try:
                    handle(int(msg.wParam))
                except Exception as e:
                    log(f"处理热键出错: {e}")
    finally:
        for hid in HOTKEYS:
            user32.UnregisterHotKey(None, hid)
        pid_file.unlink(missing_ok=True)
        log("热键守护已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
