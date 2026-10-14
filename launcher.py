# -*- coding: utf-8 -*-
"""
ReadAloud 本地启动器 —— readaloud:// 协议的执行端
--------------------------------------------------------------------------
网页上的「一键启动本机引擎」按钮会打开 readaloud://start 链接，
Windows 按注册表把我们叫起来，这里负责把引擎拉起来 / 停掉 / 打开页面。

为什么需要它：浏览器出于安全不能直接启动本机程序，只能走"自定义协议"
这条由用户明确点击触发的通道。有了它，安装之后所有操作都能在浏览器里完成。

支持的动作（readaloud:// 后面的部分）：
    start    启动引擎（不动浏览器，适合从网页上点过来）
    open     启动引擎并打开本地页面
    stop     停止引擎
    status   打印运行状态
    restart  重启
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "launcher.log"

# Windows 进程创建标志：脱离父进程 + 独立进程组 + 不弹控制台窗口
DETACHED = 0x00000008
NEW_GROUP = 0x00000200
NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def config() -> dict:
    try:
        return json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def port() -> int:
    return int(os.environ.get("READALOUD_PORT") or config().get("port") or 8756)


def running(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", p)) == 0


def _usable(exe: str) -> bool:
    """这个解释器真能跑我们的服务吗（依赖齐全）？

    必须实测。一台机器上常有多个 Python，随手挑一个很可能缺 flask / edge-tts，
    那样启动出来的进程会立刻死掉，用户只看到"点了没反应"。
    """
    try:
        r = subprocess.run([exe, "-c", "import flask, edge_tts, requests"],
                           capture_output=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def _candidates() -> list:
    out = []

    def add(p):
        if p and Path(p).exists() and str(p) not in out:
            out.append(str(p))

    env = os.environ.get("READALOUD_PYTHON")           # 1) 显式指定
    if env:
        add(env)
    memo = BASE / "python.path"                        # 2) 上次验证可用的
    if memo.exists():
        try:
            add(memo.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    for name in ("pythonw.exe", "python.exe"):         # 3) install.bat 建的工程内环境
        add(BASE / ".venv" / "Scripts" / name)
    exe = Path(sys.executable)                         # 4) 当前解释器
    add(exe.with_name("pythonw.exe"))                  #    无窗口版本优先，避免留个黑框
    add(exe)
    add(exe.with_name("python.exe"))
    for name in ("pythonw.exe", "python.exe"):         # 5) PATH 里能找到的
        add(shutil.which(name))
    return out


def python_for_gui() -> str:
    """挑一个依赖齐全的解释器，并记下来供下次直接用。"""
    cands = _candidates()
    for c in cands:
        if _usable(c):
            try:
                (BASE / "python.path").write_text(c, encoding="utf-8")
            except Exception:
                pass
            return c
    log("✗ 没找到装好依赖的 Python —— 请先双击 install.bat 完成一次安装")
    return cands[0] if cands else "python"


def port_pids(p: int) -> list:
    """查出正在监听该端口的进程号（只用系统自带的 netstat）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, encoding="utf-8", errors="ignore", timeout=8).stdout
    except Exception as e:
        log(f"netstat 调用失败: {e}")
        return []
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{p}"):
                pids.add(parts[4])
    return sorted(pids)


def start(p: int, open_page: bool) -> int:
    if running(p):
        log(f"引擎已在运行（端口 {p}）")
    else:
        py = python_for_gui()
        log(f"正在启动引擎：{py} server.py（端口 {p}）")
        with (BASE / "server.log").open("a", encoding="utf-8") as lf:
            subprocess.Popen(
                [py, str(BASE / "server.py")], cwd=str(BASE),
                creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
                close_fds=True, stdout=lf, stderr=lf,
            )
        for _ in range(34):                       # 最多等约 10 秒
            time.sleep(0.3)
            if running(p):
                break
        if not running(p):
            log("✗ 启动超时。请查看 server.log；常见原因是依赖没装好（运行 install.bat）")
            return 1
        log(f"✓ 引擎已就绪 -> http://127.0.0.1:{p}/")

    if open_page:
        webbrowser.open(f"http://127.0.0.1:{p}/")
        log("已打开本地页面")
    return 0


def stop(p: int) -> int:
    if not running(p):
        log(f"引擎没在运行（端口 {p}）")
        return 0
    pids = port_pids(p)
    if not pids:
        log("没找到监听进程，可能刚退出")
        return 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True,
                           text=True, encoding="utf-8", errors="ignore", timeout=8)
            log(f"已停止进程 {pid}")
        except Exception as e:
            log(f"停止 {pid} 失败: {e}")
    return 0 if not running(p) else 1


def main() -> int:
    raw = (sys.argv[1] if len(sys.argv) > 1 else "readaloud://start").strip()
    act = re.sub(r"^readaloud://", "", raw, flags=re.I).strip("/ ").lower()
    act = act.split("/")[0]
    p = port()
    if act in ("", "start"):
        return start(p, open_page=False)
    if act == "open":
        return start(p, open_page=True)
    if act == "stop":
        return stop(p)
    if act == "restart":
        stop(p)
        time.sleep(1.0)
        return start(p, open_page=True)
    if act == "status":
        log(json.dumps({"running": running(p), "port": p, "python": python_for_gui()},
                       ensure_ascii=False))
        return 0
    log(f"未知动作：{raw}（支持 start / open / stop / restart / status）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
