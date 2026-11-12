"""并发吞吐压测：同时打 K 条短句，量总吞吐（音频秒 / 墙钟秒）与显卡频率。

只连 8891 的 TTS 服务本体，不经引擎（引擎有缓存会干扰测量）。
用法：python _throughput_test.py [并发数] [轮数]
"""

import json
import subprocess
import sys
import threading
import time
import urllib.request

URL = "http://127.0.0.1:8891/v1/audio/speech"

SENTENCES = [
    "据传，中世纪法国普罗旺斯地区的赶骡人常把驮鞍当作枕头。",
    "因此在法语中，私生子被婉称为驮鞍之子。",
    "十三世纪英语直接从古法语借用了这个单词。",
    "但到了十九世纪初，它开始逐渐带有贬义色彩。",
    "如今这个词更多被用作骂人的粗俗语，很少出现在正式场合。",
    "若要表示非婚生子，人们常用爱之子来婉转地代替。",
]


def synth(text: str) -> float:
    """合成一条，返回音频秒数。"""
    body = json.dumps({"input": text, "voice": "默认女声", "speed": 1.0,
                       "response_format": "wav"}).encode("utf-8")
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        resp.read()
        audio_sec = float(resp.headers.get("X-ReadAloud-Audio-Seconds", "0"))
        took = time.time() - t0
    rtf = took / max(0.1, audio_sec)
    print(f"    {len(text)} 字 → {audio_sec:.1f} 秒音频 / {took:.1f} 秒 (RTF {rtf:.2f})")
    return audio_sec


def gpu_watcher(stop: threading.Event, out: list) -> None:
    while not stop.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=clocks.sm,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
                creationflags=(0x08000000 if sys.platform == "win32" else 0))
            clk, watt = r.stdout.strip().splitlines()[0].split(",")
            out.append((float(clk), float(watt)))
        except Exception:                                  # noqa: BLE001
            pass
        stop.wait(1.0)


def main() -> int:
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    print(f"=== 并发 {k} × {rounds} 轮（每轮 {k} 条短句并飞）===")
    stop = threading.Event()
    samples = []
    th = threading.Thread(target=gpu_watcher, args=(stop, samples), daemon=True)
    th.start()
    time.sleep(1.2)                                        # 先采一个静置频率
    t_all = time.time()
    audio_total = 0.0
    for rd in range(rounds):
        group = SENTENCES[rd * k % len(SENTENCES):][:k]
        if len(group) < k:                                 # 不够就从头补
            group += SENTENCES[:k - len(group)]
        print(f"  第 {rd + 1} 轮：")
        ts = [threading.Thread(target=synth, args=(s,)) for s in group]
        # 收音频秒数：synth 打印兼返回，简单起见用全局累加
        global_acc = [0.0]
        lock = threading.Lock()

        def run(s):
            sec = synth(s)
            with lock:
                global_acc[0] += sec

        ts = [threading.Thread(target=run, args=(s,)) for s in group]
        t0 = time.time()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wall = time.time() - t0
        audio_total += global_acc[0]
        print(f"  → 本轮 {k} 条共 {global_acc[0]:.1f} 秒音频 / {wall:.1f} 秒 "
              f"= 吞吐 {global_acc[0] / wall:.2f}x")
    stop.set()
    th.join(timeout=2)
    wall_all = time.time() - t_all
    busy = [s for s in samples[1:-1]]                      # 掐头去尾
    clk_avg = sum(s[0] for s in busy) / max(1, len(busy))
    watt_avg = sum(s[1] for s in busy) / max(1, len(busy))
    print(f"=== 总吞吐 {audio_total:.1f} / {wall_all:.1f} = "
          f"{audio_total / wall_all:.2f}x 实时；"
          f"GPU 均值 {clk_avg:.0f} MHz / {watt_avg:.0f} W "
          f"(静置 {samples[0][0]:.0f} MHz) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
