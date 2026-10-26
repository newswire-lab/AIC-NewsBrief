"""对照实验：比较不同开源模型在同一批事件上的生成质量。

**为什么需要这个脚本**

技术报告第三节声称"选型由实测驱动"——即换用 3B 不是因为"参数大更好"，
而是因为 1.5B 在 300 字长文本上表现不稳定（实测一次只写 100~180 字）。
这类结论**必须给出可复现的对照数据**，否则就只是主观判断。

本脚本在**完全相同的事件与材料**上分别跑不同模型，输出可直接写入报告的对比表：

- 一次成型率（未触发抽取式降级）
- 简报长度（越接近 300 越好）
- 逐句溯源达标率
- 生成耗时
- 实际加载模式（4bit / full），用于确认量化是否生效

用法：
    python scripts/compare_models.py                      # 默认比较 1.5B 与 3B
    python scripts/compare_models.py --events 5
    python scripts/compare_models.py --dirs models/Qwen2.5-1.5B-Instruct
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import brief, cluster, llm, store                     # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_DIRS = [
    "models/Qwen2.5-1.5B-Instruct",
    "models/Qwen2.5-3B-Instruct",
]


def model_id_of(rel_dir: str) -> str:
    """由本地目录名推断魔搭模型 id。"""
    name = os.path.basename(rel_dir.rstrip("/\\"))
    return f"Qwen/{name}" if name.startswith("Qwen") else name


def backend_for(rel_dir: str, base_cfg: Dict[str, Any], four_bit: bool):
    """按指定权重目录构造一个本地后端（覆盖配置，不影响全局缓存）。"""
    import json
    cfg = json.loads(json.dumps(base_cfg))          # 深拷贝，避免污染全局配置
    cfg["backend"] = "local"
    cfg["local"]["model_dir"] = rel_dir
    cfg["local"]["model_id"] = model_id_of(rel_dir)
    cfg["local"]["load_in_4bit"] = four_bit
    return llm.build_backend(cfg, "local"), cfg


def release(backend) -> None:
    """释放上一个模型占用的显存，避免两个模型叠加导致 OOM。"""
    try:
        backend._model = None                        # noqa: SLF001
        backend._tokenizer = None                    # noqa: SLF001
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="对照实验：不同开源模型的生成质量")
    parser.add_argument("--dirs", nargs="*", default=DEFAULT_DIRS,
                        help="要比较的权重目录（相对仓库根）")
    parser.add_argument("--events", type=int, default=5, help="参与比较的事件数")
    parser.add_argument("--min-orgs", type=int, default=1)
    parser.add_argument("--no-4bit", action="store_true", help="不启用 4-bit 量化")
    args = parser.parse_args()

    config = llm.load_llm_config()
    conn = store.connect()
    store.init_db(conn)

    rows = cluster.top_events(conn, limit=args.events, min_orgs=args.min_orgs)
    if not rows:
        print("[空] 没有可用事件，先跑 python -m src.cli pipeline")
        return 1

    out: List[str] = []
    out.append("=" * 78)
    out.append("开源模型对照实验")
    out.append("=" * 78)
    out.append(f"参与比较的事件数: {len(rows)}   量化: {'关闭' if args.no_4bit else '启用(4bit)'}")
    out.append(f"事件 id: {', '.join(r['event_id'][:16] for r in rows)}")
    out.append("")

    summary: Dict[str, Dict[str, float]] = {}

    for rel_dir in args.dirs:
        abs_dir = os.path.join(PROJECT_ROOT, rel_dir)
        out.append("=" * 78)
        out.append(f"模型: {rel_dir}")
        if not os.path.isdir(abs_dir):
            out.append("  [跳过] 权重目录不存在")
            out.append("")
            continue

        t_load = time.time()
        try:
            backend, cfg = backend_for(rel_dir, config, not args.no_4bit)
            backend._ensure_loaded()                 # noqa: SLF001  预热，把加载耗时与生成耗时分开
        except Exception as exc:
            out.append(f"  [失败] 无法加载: {type(exc).__name__}: {str(exc)[:300]}")
            out.append("")
            continue
        load_s = time.time() - t_load
        desc = backend.describe()
        out.append(f"  加载模式: {desc.get('load_mode')}   加载耗时: {load_s:.1f}s")
        out.append("")
        out.append("  %-4s %-14s %-7s %-8s %-8s %s"
                   % ("#", "结果", "字数", "溯源率", "耗时s", "标题"))
        out.append("  " + "-" * 78)

        stats = {"n": 0, "model_ok": 0, "chars": 0, "supported_ratio": 0.0,
                 "elapsed": 0.0, "fallback": 0}
        for i, row in enumerate(rows, 1):
            t0 = time.time()
            try:
                result = brief.generate_brief(conn, row["event_id"], backend=backend, cfg=cfg)
            except Exception as exc:
                out.append(f"  {i:<4} [异常] {type(exc).__name__}: {str(exc)[:80]}")
                continue
            wall = time.time() - t0

            ver = result.get("verification") or {}
            # 降级时 verification 描述的是「抽取式产物」——它直接摘自原文，
            # 必然 100% 溯源且必然通过校验；模型自身的真实表现被存在
            # verification_before_fallback 里。评价模型能力必须用后者，
            # 否则降级行会伪装成完美结果，两个模型都看上去 100% 达标。
            pre = result.get("verification_before_fallback") or ver
            is_fallback = bool(result.get("_fallback"))
            model_ok = bool(ver.get("accepted")) and not is_fallback

            chars = int(pre.get("brief_chars") or 0)
            ratio = float(pre.get("supported_ratio") or 0.0)

            stats["n"] += 1
            stats["model_ok"] += 1 if model_ok else 0
            stats["fallback"] += 1 if is_fallback else 0
            stats["chars"] += chars
            stats["supported_ratio"] += ratio
            stats["elapsed"] += wall

            out.append("  %-4d %-14s %-7d %-8s %-8.1f %s"
                       % (i,
                          "一次成型" if model_ok else "降级为抽取式",
                          chars,
                          f"{ratio:.0%}",
                          wall,
                          (result.get("headline") or "")[:30]))
            if is_fallback:
                out.append("       └ 模型直出未过校验，已自动降级："
                           f"{(result.get('_fallback_reason') or '')[:48]}")

        n = max(1, stats["n"])
        summary[rel_dir] = {
            "n": stats["n"],
            "model_ok": stats["model_ok"],
            "model_ok_rate": stats["model_ok"] / n,
            "avg_chars": stats["chars"] / n,
            "avg_supported": stats["supported_ratio"] / n,
            "avg_elapsed": stats["elapsed"] / n,
            "fallback": stats["fallback"],
            "load_mode": str(desc.get("load_mode")),
            "load_s": load_s,
        }
        out.append("")
        release(backend)
        time.sleep(1)

    # ---- 汇总表（可直接写入技术报告）----
    out.append("=" * 78)
    out.append("汇总对比")
    out.append("=" * 78)
    out.append("%-28s %-11s %-9s %-9s %-10s %-8s %-8s"
               % ("模型", "一次成型率", "平均字数", "平均溯源", "平均耗时s", "加载模式", "加载s"))
    out.append("-" * 92)
    for rel_dir, s in summary.items():
        out.append("%-28s %-11s %-9.0f %-9s %-10.1f %-8s %-8.1f"
                   % (os.path.basename(rel_dir),
                      f"{s['model_ok']}/{s['n']}",
                      s["avg_chars"],
                      f"{s['avg_supported']:.0%}",
                      s["avg_elapsed"],
                      s["load_mode"],
                      s["load_s"]))
    out.append("-" * 92)
    out.append("说明：")
    out.append("  一次成型率 = 模型直出且通过校验的比例（降级行不计入）。")
    out.append("  平均字数 / 平均溯源 统计的是【模型直出】的结果：降级行取")
    out.append("  verification_before_fallback，即模型失败前的真实输出。")
    out.append("  若改用降级后的抽取式产物统计，会得到虚高的通过率与 100% 溯源率。")

    text = "\n".join(out)
    log_path = os.path.join(PROJECT_ROOT, "logs", "model_compare.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\n已写入: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
