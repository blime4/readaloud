"""TTS 分阶段耗时剖析：前端 / LLM 解码 / flow+hift / empty_cache 各花多久。

用 cosyvoice 环境跑：C:/Users/bo/miniconda3/envs/cosyvoice/python.exe _profile_tts.py
"""

import io
import sys
import time

sys.path.insert(0, ".")
import tts_cosy_server as srv  # noqa: E402

engine, err = srv.load_engine()
if engine is None:
    print("加载失败：", err)
    sys.exit(1)
srv.prepare_voice("默认女声")

# —— 打点：llm_job / token2wav / empty_cache ——
import torch  # noqa: E402
from cosyvoice.cli.model import CosyVoice2Model  # noqa: E402

M = engine.model
stats = {"llm": 0.0, "flow": 0.0, "empty": 0.0, "n_llm": 0, "n_flow": 0}

_llm_job = M.llm_job
def llm_job(*a, **k):
    t0 = time.time()
    r = _llm_job(*a, **k)
    stats["llm"] += time.time() - t0
    stats["n_llm"] += 1
    return r
M.llm_job = llm_job

_token2wav = M.token2wav
def token2wav(*a, **k):
    t0 = time.time()
    r = _token2wav(*a, **k)
    stats["flow"] += time.time() - t0
    stats["n_flow"] += 1
    return r
M.token2wav = token2wav

_empty = torch.cuda.empty_cache
def empty_cache():
    t0 = time.time()
    r = _empty()
    dt = time.time() - t0
    stats["empty"] += dt
    if dt > 0.3:
        print(f"  [empty_cache 慢] {dt:.2f}s")
    return r
torch.cuda.empty_cache = empty_cache

TEXTS = [
    "据传，中世纪法国普罗旺斯地区的赶骡人常把驮鞍当作枕头。",
    "因此在法语中，私生子被婉称为驮鞍之子。",
    "十三世纪英语直接从古法语借用了这个单词。",
    "但到了十九世纪初，它开始逐渐带有贬义色彩。",
]

# 预热一条（CUDA kernel 编译缓存等）
for i, text in enumerate(TEXTS):
    t0 = time.time()
    chunks = [item["tts_speech"] for item in engine.inference_zero_shot(
        text, "", "", zero_shot_spk_id="默认女声", stream=False, speed=1.0)]
    took = time.time() - t0
    full = chunks[0] if len(chunks) == 1 else torch.cat(chunks, dim=1)
    audio = full.shape[-1] / 24000
    print(f"第{i+1}条 {len(text)} 字 → {audio:.1f}s 音频 / {took:.2f}s "
          f"(RTF {took / audio:.2f})  累计 llm={stats['llm']:.1f}s "
          f"flow={stats['flow']:.1f}s empty={stats['empty']:.1f}s")

llm_total = stats["llm"]
flow_total = stats["flow"]
print(f"\n4 条合计：llm 解码 {llm_total:.1f}s（{stats['n_llm']} 次）、"
      f"flow+hift {flow_total:.1f}s（{stats['n_flow']} 次）、"
      f"empty_cache {stats['empty']:.1f}s")
print("其余时间 = 文本前端(wetext/分词) + HTTP 无关的进程内开销")
