# -*- coding: utf-8 -*-
"""
注册 / 注销 readaloud:// 自定义协议
--------------------------------------------------------------------------
注册之后，网页上的 readaloud://start 链接才能唤起本机启动器。
只写 HKCU（当前用户），不需要管理员权限，也随时可以撤销。

用法：
    python setup_protocol.py --install     注册
    python setup_protocol.py --uninstall   注销
    python setup_protocol.py --status      查看当前注册成了什么
"""

import sys
import winreg
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = r"Software\Classes\readaloud"
CMD_KEY = ROOT + r"\shell\open\command"


def target_command() -> str:
    """协议被触发时执行的命令行，%1 会被替换成完整链接。"""
    return f'"{BASE / "launcher.bat"}" "%1"'


def install() -> int:
    launcher = BASE / "launcher.bat"
    if not launcher.exists():
        print(f"[x] 找不到 {launcher}")
        return 1
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ROOT, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:ReadAloud Protocol")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, CMD_KEY, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, target_command())
    print("[√] 已注册 readaloud:// 协议")
    print(f"    {target_command()}")
    return 0


def uninstall() -> int:
    def drop(path: str) -> None:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"    删除 {path} 时出错: {e}")

    drop(CMD_KEY)
    drop(ROOT + r"\shell\open")
    drop(ROOT + r"\shell")
    drop(ROOT)
    print("[√] 已注销 readaloud:// 协议")
    return 0


def status() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CMD_KEY) as k:
            val = winreg.QueryValueEx(k, "")[0]
        print("已注册：", val)
        expected = target_command()
        print("是否指向当前目录：", "是" if val == expected else f"否（期望 {expected}）")
        return 0
    except FileNotFoundError:
        print("未注册 readaloud:// 协议")
        return 1


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "--install").lower()
    if arg in ("--install", "-i", "install"):
        sys.exit(install())
    if arg in ("--uninstall", "-u", "uninstall"):
        sys.exit(uninstall())
    if arg in ("--status", "-s", "status"):
        sys.exit(status())
    print(__doc__)
    sys.exit(2)
