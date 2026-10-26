"""诊断本地开源模型输出乱码的原因。

输出从第一个 token 就是多语言随机噪声，说明不是提示词或 JSON 格式问题，
而是模型加载/数值层面的问题。按可能性从高到低逐项检验：

1. dtype 是否一致（config 声明 vs 实际加载 vs 权重实际）
2. 贪心解码下最小样例是否正常（排除采样参数）
3. 不套 chat template 直接续写是否正常（排除模板）
4. 换 attn_implementation（新版 transformers 可能默认启用新 attention 内核，
   在 sm_120 上可能有问题）—— 这是最可疑的一项
"""

from __future__ import annotations

import io
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch                                                   # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer   # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_model_dir() -> str:
    """解析要诊断的权重目录。

    优先取 ``config/llm.yaml`` 中配置的 ``model_dir`` —— 否则脚本会与配置股节：
    早期版本这里写死了 1.5B 路径，换用 3B 后诊断脚本会去检查一个已经不用的模型，
    给出误导性的结论。也可用环境变量 ``NEWSBRIEF_MODEL_DIR`` 临时覆盖。
    """
    override = os.environ.get("NEWSBRIEF_MODEL_DIR")
    if override:
        return override
    try:
        from src import llm as _llm                     # noqa: PLC0415
        return str(_llm.model_dir_of())
    except Exception:                                   # noqa: BLE001
        return os.path.join(PROJECT_ROOT, "models", "Qwen2.5-3B-Instruct")


MODEL_DIR = _resolve_model_dir()

out = []


def log(msg: str = "") -> None:
    out.append(str(msg))
    print(str(msg))


def load(attn: str = "default", dtype=None, device_map="cuda:0"):
    kwargs = {"device_map": device_map}
    if attn != "default":
        kwargs["attn_implementation"] = attn
    if dtype is not None:
        kwargs["dtype"] = dtype
    return AutoModelForCausalLM.from_pretrained(MODEL_DIR, **kwargs)


def greedy(model, tokenizer, prompt: str, n: int = 20, chat: bool = False) -> str:
    if chat and getattr(tokenizer, "chat_template", None):
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=False, add_generation_prompt=True,
        )
    else:
        text = prompt
    enc = tokenizer(text, return_tensors="pt")
    device = next(model.parameters()).device
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.no_grad():
        ids = model.generate(**enc, max_new_tokens=n, do_sample=False,
                             pad_token_id=tokenizer.pad_token_id)
    new = ids[0][enc["input_ids"].shape[-1]:]
    return tokenizer.decode(new, skip_special_tokens=True)


def main() -> int:
    log("=" * 74)
    log("环境")
    log("=" * 74)
    log("torch        : %s" % torch.__version__)
    log("cuda 可用    : %s" % torch.cuda.is_available())
    if torch.cuda.is_available():
        log("GPU          : %s" % torch.cuda.get_device_name(0))
        log("算力         : sm_%d%d" % torch.cuda.get_device_capability(0))
        log("bf16 支持    : %s" % torch.cuda.is_bf16_supported())
    import transformers                                    # noqa: PLC0415
    log("transformers : %s" % transformers.__version__)
    log("")

    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, use_fast=True)

    variants = [
        ("A: dtype=auto + 默认attn + 直接续写", dict(attn="default", dtype=None), False),
        ("B: dtype=bf16 + 默认attn + 直接续写", dict(attn="default", dtype=torch.bfloat16), False),
        ("C: dtype=bf16 + eager attn + 直接续写", dict(attn="eager", dtype=torch.bfloat16), False),
        ("D: dtype=bf16 + eager attn + chat模板", dict(attn="eager", dtype=torch.bfloat16), True),
        ("E: dtype=fp16 + 默认attn + 直接续写", dict(attn="default", dtype=torch.float16), False),
    ]

    for name, kwargs, use_chat in variants:
        log("-" * 74)
        log("变体 %s" % name)
        try:
            model = load(**kwargs)
        except Exception:
            log("  加载失败:\n%s" % traceback.format_exc()[-800:])
            continue

        try:
            emb = model.get_input_embeddings().weight
            log("  model.dtype=%s  config.torch_dtype=%s  嵌入权重dtype=%s  设备=%s"
                % (model.dtype, getattr(model.config, "torch_dtype", None),
                   emb.dtype, emb.device))
        except Exception as exc:
            log("  读取 dtype 失败: %s" % exc)

        try:
            if use_chat:
                got = greedy(model, tokenizer, "用一句话介绍北京。", n=40, chat=True)
            else:
                got = greedy(model, tokenizer, "中国的首都是", n=20)
            log("  输出: %r" % got[:200])
        except Exception:
            log("  生成失败:\n%s" % traceback.format_exc()[-800:])

        del model
        torch.cuda.empty_cache()

    log("=" * 74)
    with io.open(os.path.join(PROJECT_ROOT, "logs", "model_diagnose.txt"), "w",
                 encoding="utf-8") as f:
        f.write("\n".join(out))
    print("已写入 logs/model_diagnose.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
