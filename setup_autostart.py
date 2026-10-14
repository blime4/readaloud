# -*- coding: utf-8 -*-
"""
开机自启管理 —— 让引擎在下次登录后自动在后台跑起来
--------------------------------------------------------------------------
只写 HKCU\\...\\CurrentVersion\\Run（当前用户级，不需要管理员权限，随时可撤销）。

写入的命令是 "pythonw.exe launcher.py readaloud://start"：
用 pythonw 不带控制台，所以开机时不会闪黑框；启动逻辑仍复用 launcher，端口也跟着配置走。

用法：
    python setup_autostart.py --enable
    python setup_autostart.py --disable
    python setup_autostart.py --status
"""

import sys
import winreg

import launcher as L

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "ReadAloud"          # 单个自启项，引擎与热键都由 launcher 统一管理


def command() -> str:
    return f'"{L.python_for_gui()}" "{L.BASE / "launcher.py"}" "readaloud://start"'


def enable() -> int:
    cmd = command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, cmd)
    print("[√] 已设置开机自启")
    print("    " + cmd)
    print("    下次登录后引擎会自动在后台运行（无窗口）。")
    print("    想取消，双击 disable-autostart.bat 即可。")
    return 0


def disable() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE)
            # 兼容早期版本写过的两个独立自启项
            for legacy in ("ReadAloudServer", "ReadAloudHotkey"):
                try:
                    winreg.DeleteValue(k, legacy)
                except FileNotFoundError:
                    pass
        print("[√] 已取消开机自启")
    except FileNotFoundError:
        print("[i] 本来就没有设置开机自启")
    return 0


def status() -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            print("开机自启：", winreg.QueryValueEx(k, VALUE)[0])
        return 0
    except FileNotFoundError:
        print("开机自启：未设置")
        return 1


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "--status").lower()
    if arg in ("--enable", "-e", "enable"):
        sys.exit(enable())
    if arg in ("--disable", "-d", "disable"):
        sys.exit(disable())
    if arg in ("--status", "-s", "status"):
        sys.exit(status())
    print(__doc__)
    sys.exit(2)
