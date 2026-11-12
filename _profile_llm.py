"""解码循环逐步打点（Qwen2LM 路径）：forward_one_step / decoder / sampling_ids。"""

import sys
import time

sys.path.insert(0, ".")
import tts_cosy_server as srv  # noqa: E402

engine, err = srv.load_engine()
if engine is None:
    print("加载失败：", err)
    sys.exit(1)
srv.prepare_voice("默认女声")

import torch  # noqa: E402

fe = engine.frontend
llm_obj = engine.model.llm          # Qwen2LM
M = engine.model

TEXT = "但到了十九世纪初，它开始逐渐带有贬义色彩。"
model_input = fe.frontend_zero_shot(TEXT, "", None, 24000, "默认女声")
text = model_input["text"]
prompt_text = model_input["prompt_text"]
prompt_text_len = model_input["prompt_text_len"]
prompt_speech_token = model_input["llm_prompt_speech_token"]
embedding = model_input["llm_embedding"]
print(f"text_len={text.shape[1]} prompt_len={prompt_text.shape[1]} "
      f"prompt_speech={prompt_speech_token.shape[1]}")

# 复刻 Qwen2LM.inference 预填
device = "cuda"
text_all = torch.concat([prompt_text, text], dim=1).to(device)
text_len = model_input["text_len"].to(device) + prompt_text_len.to(device)
text_emb = llm_obj.llm.model.model.embed_tokens(text_all)
sos_emb = llm_obj.llm_embedding.weight[llm_obj.sos].reshape(1, 1, -1)
task_id_emb = llm_obj.llm_embedding.weight[llm_obj.task_id].reshape(1, 1, -1)
prompt_speech_token_emb = llm_obj.speech_embedding(prompt_speech_token.to(device))
lm_input = torch.concat([sos_emb, text_emb, task_id_emb, prompt_speech_token_emb], dim=1)
min_len = int((text_len - prompt_text_len.to(device)) * 2)
max_len = int((text_len - prompt_text_len.to(device)) * 20)
print(f"min_len={min_len} max_len={max_len} lm_input={lm_input.shape}")

sampling = 25
t_chunk = t_dec = t_samp = t_other = 0.0
n_steps = 0
out_tokens = []
cache = None
t_loop0 = time.time()
for i in range(max_len):
    ts = time.time()
    masks = torch.tril(torch.ones(
        (1, lm_input.shape[1], lm_input.shape[1]), device=device)).to(torch.bool)
    y_pred, cache = llm_obj.llm.forward_one_step(lm_input, masks=masks, cache=cache)
    t_chunk += time.time() - ts
    ts = time.time()
    logp = llm_obj.llm_decoder(y_pred[:, -1]).log_softmax(dim=-1)
    t_dec += time.time() - ts
    ts = time.time()
    top_ids = llm_obj.sampling_ids(logp.squeeze(dim=0), out_tokens, sampling,
                                   ignore_eos=i < min_len)
    t_samp += time.time() - ts
    ts = time.time()
    if top_ids in llm_obj.stop_token_ids:
        break
    out_tokens.append(top_ids)
    lm_input = llm_obj.speech_embedding.weight[top_ids].reshape(1, 1, -1)
    t_other += time.time() - ts
    n_steps += 1
loop = time.time() - t_loop0
print(f"共 {n_steps} 步 / {loop:.2f}s = {loop / max(1, n_steps) * 1000:.1f} ms/步")
print(f"  forward_one_step {t_chunk:.2f}s ({t_chunk / max(1,n_steps) * 1000:.1f} ms/步)  "
      f"decoder {t_dec:.2f}s ({t_dec / max(1,n_steps) * 1000:.1f} ms/步)  "
      f"sampling {t_samp:.2f}s ({t_samp / max(1,n_steps) * 1000:.1f} ms/步)  "
      f"其它 {t_other:.2f}s ({t_other / max(1,n_steps) * 1000:.1f} ms/步)")
