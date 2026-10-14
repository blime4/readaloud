# -*- coding: utf-8 -*-
"""一次性朗读剪贴板（给桌面快捷方式 / 计划任务用）。

用法：
    python clip_read.py                 # 原文朗读剪贴板
    python clip_read.py --ai summarize  # 先用小模型总结，再朗读
    python clip_read.py --ai polish     # 先润色，再朗读
    python clip_read.py --text "你好"    # 直接读指定文字
"""

import argparse
import sys

from ra_common import log, read_clipboard, speak


def main() -> int:
    ap = argparse.ArgumentParser(description="朗读剪贴板内容")
    ap.add_argument("--ai", default="none", choices=["none", "summarize", "polish"])
    ap.add_argument("--text", default=None)
    a = ap.parse_args()
    text = a.text if a.text else read_clipboard()
    result = speak(text, a.ai)
    log(result)
    print(result)
    return 0 if "没在运行" not in result and "没有文字" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
