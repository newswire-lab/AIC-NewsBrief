"""离线预热：把外文文章的**正文事实要点**译成中文，写入 ``articles.summary_zh``。

为什么单独一个脚本
------------------
``warm_translations.py`` 预热的是**标题**（80 token，1 秒/条）；
本脚本预热的是**正文事实要点**（最多 600 token，通常十几秒/条），两者代价差一个量级，
所以分开跑，互不影响。

不预热会怎样
------------
``brief.build_material`` 在**请求内**为每篇没有 ``summary_zh`` 的外文文章调一次模型。
一个 5 篇的英文事件，在正式生成之前就先跑 4~5 次推理 ——
诊断实测这是「点一次生成要等好几分钟」的两大主因之一。
预热过之后，生成路径退化成**纯读库**。

用法：
    python scripts/warm_summaries.py --limit 40             # 最近 40 篇待预热的
    python scripts/warm_summaries.py --hours 168 --limit 80  # 只补最近 7 天的
    python scripts/warm_summaries.py --dry-run               # 只看清单
    python scripts/warm_summaries.py --quiet                 # 每 10 条打一次进度
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, keywords, llm, normalize, store  # noqa: E402

POOL_LIMIT = 800


def build_query(hours: int):
    """候选：标题非空、且还没有中文事实要点的文章。"""
    sql = [
        "SELECT id, lang, title, summary, content, summary_zh,",
        "       COALESCE(published_at, fetched_at) AS ts",
        "FROM articles WHERE title != ''",
        "  AND (summary_zh IS NULL OR summary_zh = '')",
    ]
    params: list = []
    if hours > 0:
        sql.append("AND datetime(COALESCE(published_at, fetched_at)) >= datetime('now', ?)")
        params.append(f"-{int(hours)} hours")
    sql.append("ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?")
    params.append(POOL_LIMIT)
    return " ".join(sql), params


def select_pending(conn, limit: int, hours: int):
    """挑出真正需要中文事实要点的文章。

    判定仍然统一交给 ``normalize.needs_normalize()`` —— 不在这里另造一套
    「什么是外文」的标准（实测有标了 ``lang='zh'`` 的英文稿，只信字段会漏）。
    """
    sql, params = build_query(hours)
    rows = conn.execute(sql, params).fetchall()
    picked = []
    for r in rows:
        body = (r["content"] or "").strip() or (r["summary"] or "").strip()
        if not body:
            continue
        if normalize.needs_normalize(r["lang"], f"{r['title']}\n{body}"):
            picked.append(r)
        if len(picked) >= max(1, int(limit)):
            break
    return picked


def progress(conn) -> str:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN summary_zh != '' THEN 1 ELSE 0 END) AS done "
        "FROM articles WHERE lang != 'zh' AND title != ''").fetchone()
    total = int(row["total"] or 0)
    done = int(row["done"] or 0)
    pct = (done / total * 100.0) if total else 0.0
    return f"外文正文事实要点进度：{done}/{total}（{pct:.0f}%）"


def main() -> int:
    ap = argparse.ArgumentParser(description="批量预热外文正文的中文事实要点")
    ap.add_argument("--limit", type=int, default=40, help="这一轮最多处理多少篇")
    ap.add_argument("--hours", type=int, default=168,
                    help="只看最近 N 小时（默认 168=7 天；0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不调模型")
    ap.add_argument("--quiet", action="store_true", help="每 10 条打一次进度")
    args = ap.parse_args()

    config.ensure_dirs()
    conn = store.connect()
    store.init_db(conn)

    print("=" * 78)
    print("外文正文批量翻译（离线预热 summary_zh）")
    print("=" * 78)
    print(progress(conn))

    picked = select_pending(conn, args.limit, args.hours)
    scope = f"最近 {args.hours} 小时" if args.hours > 0 else "不限时窗"
    print(f"本轮待处理 {len(picked)} 篇｜{scope}")
    if not picked:
        print("\n[空] 窗口内没有待预热的正文。可加 --hours 0 放宽，或 --limit 调大。")
        return 0

    if args.dry_run:
        print("-" * 78)
        for i, r in enumerate(picked, 1):
            body = (r["content"] or r["summary"] or "").strip()
            print(f"{i:>3}. [lang={r['lang']}, {len(body)} 字符] {r['title'][:60]}")
        print("-" * 78)
        print("[dry-run] 未调用模型，未写库。")
        return 0

    cfg = llm.load_llm_config()
    backend = llm.get_backend(cfg)          # 走缓存：整轮只加载一次权重
    desc = backend.describe()
    print(f"后端 {cfg['backend']}｜模型 {desc.get('model_id') or desc.get('model')}")
    print("-" * 78)

    t0 = time.time()
    ok = fail = 0
    for i, r in enumerate(picked, 1):
        t1 = time.time()
        try:
            zh = normalize.ensure_article_chinese(conn, r, backend=backend, cfg=cfg)
            good = bool((zh or "").strip()) and not str(zh).startswith("[归一化失败")
        except Exception as exc:                    # noqa: BLE001
            zh, good = f"{type(exc).__name__}: {exc}", False
        if good:
            ok += 1
        else:
            fail += 1
        if not args.quiet or i % 10 == 0 or i == len(picked):
            flag = "" if good else "  [失败]"
            print(f"{i:>3}. [{time.time() - t1:>5.1f}s]{flag} {str(zh)[:52]}")
            print(f"     原文 {r['title'][:56]}")

    wall = time.time() - t0
    print("-" * 78)
    print(f"完成：成功 {ok} / 失败 {fail}｜耗时 {wall:.1f}s"
          f"（平均 {wall / max(1, ok):.1f}s 一篇）")

    # 正文事实要点是事件关键词的**首选**挖掘文本，所以预热完顺手重算标签
    if ok:
        print("已重算事件关键词标签：", keywords.tag_all(conn, verbose=False))
    print(progress(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
