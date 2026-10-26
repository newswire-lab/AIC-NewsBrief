"""调试本地开源模型的生成结果：打印原始输出，定位 JSON 解析失败的原因。

用法：
    python scripts/debug_llm.py                 # 用排名第一的事件
    python scripts/debug_llm.py <event_id>
    python scripts/debug_llm.py --tokens 1400   # 覆盖 max_new_tokens
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import brief, cluster, llm, store                     # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    parser = argparse.ArgumentParser(description="调试本地模型输出")
    parser.add_argument("event_id", nargs="?")
    parser.add_argument("--backend", default="local")
    parser.add_argument("--tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    args = parser.parse_args()

    conn = store.connect()
    store.init_db(conn)

    event_id = args.event_id
    if not event_id:
        rows = cluster.top_events(conn, limit=1, min_orgs=1)
        if not rows:
            print("没有可用事件")
            return 1
        event_id = rows[0]["event_id"]

    cfg = llm.load_llm_config()
    material, sources = brief.build_material(
        conn, event_id, max_total_chars=int(cfg["task"]["max_source_chars"])
    )
    prompt = brief.build_prompt(material, len(sources), len(sources), cfg)

    out = []
    out.append("=" * 74)
    out.append("事件: %s" % event_id)
    out.append("材料篇数: %d   提示词长度: %d 字符" % (len(sources), len(prompt)))
    out.append("后端: %s" % args.backend)
    out.append("=" * 74)
    out.append("")
    out.append("--- 提示词（前 1500 字符）---")
    out.append(prompt[:1500])
    out.append("")
    out.append("=" * 74)

    backend = llm.build_backend(cfg, kind=args.backend)
    t0 = time.time()
    try:
        gen = backend.generate(prompt, system=brief.SYSTEM_PROMPT,
                               max_new_tokens=args.tokens,
                               temperature=args.temperature)
    except Exception as exc:
        import traceback
        out.append("生成失败: %s" % traceback.format_exc())
        with io.open(os.path.join(PROJECT_ROOT, "logs", "llm_debug.txt"), "w",
                     encoding="utf-8") as f:
            f.write("\n".join(out))
        print("失败，详见 logs/llm_debug.txt")
        return 1
    wall = time.time() - t0

    out.append("生成完成: backend=%s model=%s" % (gen.backend, gen.model))
    out.append("耗时: %d ms (wall %.1fs)   输出长度: %d 字符" % (gen.elapsed_ms, wall,
                                                              len(gen.text)))
    out.append("元信息: %s" % gen.extra)
    out.append("")
    out.append("--- 模型原始输出（完整）---")
    out.append(gen.text)
    out.append("")
    out.append("--- JSON 解析尝试 ---")
    parsed = brief.extract_json(gen.text)
    if parsed is None:
        out.append("解析失败：未能从输出中抠出 JSON 对象")
        # 诊断线索
        out.append("  '{' 出现次数: %d" % gen.text.count("{"))
        out.append("  '}' 出现次数: %d" % gen.text.count("}"))
        out.append("  含 ``` 围栏: %s" % ("```" in gen.text))
        out.append("  以 } 结尾: %s" % gen.text.rstrip().endswith("}"))
    else:
        out.append("解析成功，字段: %s" % ", ".join(sorted(parsed.keys())))
        out.append("  headline: %r" % str(parsed.get("headline"))[:80])
        out.append("  brief 长度: %d" % len(str(parsed.get("brief") or "")))
        out.append("  claims 条数: %d" % len(parsed.get("claims") or []))

    text = "\n".join(out)
    log_path = os.path.join(PROJECT_ROOT, "logs", "llm_debug.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("已写入: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
