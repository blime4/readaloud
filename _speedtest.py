# -*- coding: utf-8 -*-
"""干净环境下的模型速度对比：每次只让一个模型驻留显存。"""
import json
import subprocess
import time
import urllib.request

OLLAMA = r"C:\Users\bo\AppData\Local\Programs\Ollama\ollama.exe"
P_TEXT = "本季度营收 1,280 万元，同比增长 30%，毛利率提升至 62%。结论：把更多资源投向 AI 方向。"
S_TEXT = ("人工智能正在改变工作方式。过去需要人工完成的重复性任务，如今可以交给模型自动处理。"
          "这并不意味着人类不重要，而是人的价值更多体现在判断、审美和决策上。未来的竞争是谁更会用工具。")


def call(payload):
    req = urllib.request.Request("http://127.0.0.1:8756/api/prepare",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=600).read().decode())
    return time.time() - t, d


for model in ("qwen3:4b", "qwen3.5:9b"):
    subprocess.run([OLLAMA, "stop", "qwen3:4b"], capture_output=True)
    subprocess.run([OLLAMA, "stop", "qwen3.5:9b"], capture_output=True)
    time.sleep(2)
    print(f"===== {model}（显存已清空）=====")
    for label, payload in (("总结", {"ai": "summarize", "model": model}),
                           ("润色", {"ai": "polish", "model": model}),
                           ("润色(第二次)", {"ai": "polish", "model": model})):
        p = dict(payload)
        p["text"] = S_TEXT if payload["ai"] == "summarize" else P_TEXT
        dt, d = call(p)
        extra = d["style"].get("note", "") if d["ai"] == "polish" else d["text"][:40]
        print(f"  {label:14s} {dt:6.1f}s  模型={d['model']}  {extra}")
