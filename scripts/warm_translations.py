"""批量预热：把外文新闻标题译成中文，写入 ``articles.title_zh``。

**为什么必须离线跑。** 单条标题要调一次本地模型（实测 2~4 秒）。如果放进 Web 界面
同步等结果，页面会卡死；做成后台线程又要处理并发与写库竞争。所以翻译在**离线**
批量完成，界面只读结果 —— 这也是本项目一贯的分工：慢的、要调模型的都在离线，
界面里只做「读 + 自研算法」。

**和 ``normalize.ensure_article_chinese`` 的分工。**
后者把正文转写成「中文事实要点」（3~6 句，用于简报素材，600 tokens）；
本脚本默认只翻**标题**（80 tokens，快得多）。要用后者，加 ``--with-summary``。

用法：
    python scripts/warm_translations.py                      # 最近 40 条未译外文标题
    python scripts/warm_translations.py --limit 200
    python scripts/warm_translations.py --hours 168          # 只处理最近 7 天的
    python scripts/warm_translations.py --dry-run            # 先看清单，不调模型
    python scripts/warm_translations.py --with-summary       # 连正文事实要点一起转写（慢）
    python scripts/warm_translations.py --refresh            # 已译过的也重译
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

# 候选池大小。因为「要不要翻」是在 Python 里判定的（见 select_pending），
# 所以要先把窗口内的候选多取一些，再筛出真正待译的 limit 条。
POOL_LIMIT = 1000


def build_candidate_query(hours: int, refresh: bool):
    """构造候选查询（只取轻量字段，不含 content）。"""
    sql = [
        "SELECT id, lang, title, title_zh,",
        "       COALESCE(published_at, fetched_at) AS ts",
        "FROM articles WHERE title != ''",
    ]
    params: list = []
    if not refresh:
        sql.append("AND title_zh = ''")
    if hours > 0:
        sql.append("AND datetime(COALESCE(published_at, fetched_at)) >= datetime('now', ?)")
        params.append(f"-{int(hours)} hours")
    sql.append("ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?")
    params.append(POOL_LIMIT)
    return " ".join(sql), params


def select_pending(conn, limit: int, hours: int, refresh: bool):
    """挑出真正需要翻译的文章。

    **不按 ``lang`` 字段过滤。** 实测库里有 8 篇标为 ``zh`` 但标题全是英文的文章
    （中国军网的英文稿，见 ``--dry-run`` 输出）—— 只信 ``lang`` 会漏掉它们。
    所以「要不要翻」一律交给 ``normalize.needs_normalize()`` 判断，
    与归一化模块**同一口径**：同一个库里不能有两套「什么是外文」的标准。
    """
    sql, params = build_candidate_query(hours, refresh)
    rows = conn.execute(sql, params).fetchall()
    picked = [r for r in rows
              if normalize.needs_normalize(r["lang"], r["title"] or "")]
    return picked[:max(1, int(limit))]


def load_full(conn, ids):
    """按 id 取回完整字段（含 content，供 --with-summary 用）。"""
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    return conn.execute(
        "SELECT id, lang, title, title_zh, summary, summary_zh, content, "
        "       COALESCE(published_at, fetched_at) AS ts "
        f"FROM articles WHERE id IN ({marks}) "
        "ORDER BY COALESCE(published_at, fetched_at) DESC",
        list(ids),
    ).fetchall()


def progress(conn) -> str:
    """译名进度。分母用「标题非中文」判定，而不是 ``lang`` 字段（后者有 8 条不准）。"""
    rows = conn.execute(
        "SELECT lang, title, title_zh FROM articles WHERE title != ''"
    ).fetchall()
    total = done = 0
    for r in rows:
        if not normalize.needs_normalize(r["lang"], r["title"] or ""):
            continue
        total += 1
        if (r["title_zh"] or "").strip():
            done += 1
    pct = (done / total * 100.0) if total else 0.0
    return f"外文标题译名进度：{done}/{total}（{pct:.0f}%）"


def main() -> int:
    ap = argparse.ArgumentParser(description="批量预热外文标题的中文译名")
    ap.add_argument("--limit", type=int, default=40, help="这一轮最多处理多少条")
    ap.add_argument("--hours", type=int, default=0, help="只看最近 N 小时，0=不限")
    ap.add_argument("--refresh", action="store_true", help="已有译名的也重译一遍")
    ap.add_argument("--with-summary", action="store_true",
                    help="顺带把正文转写成中文事实要点（每条慢很多）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待翻译清单，不调模型")
    ap.add_argument("--quiet", action="store_true",
                    help="批量跑时只每 25 条打一次进度（日志不会刷屏）")
    ap.add_argument("--sleep", type=float, default=0.0, help="每条之间歇 N 秒")
    args = ap.parse_args()

    config.ensure_dirs()
    conn = store.connect()
    store.init_db(conn)

    print("=" * 78)
    print("外文标题批量翻译（离线预热）")
    print("=" * 78)
    print(progress(conn))

    picked = select_pending(conn, args.limit, args.hours, args.refresh)
    scope = f"最近 {args.hours} 小时" if args.hours > 0 else "不限时窗"
    print(f"本轮待处理 {len(picked)} 条｜{scope}"
          f"｜{'重译已有译名' if args.refresh else '只处理未译的'}")
    if not picked:
        print("\n[空] 没有待翻译的外文标题。")
        print("      · 想重译：加 --refresh")
        print("      · 想放宽时窗：加 --hours 0")
        print("      · 想多翻一些：--limit 200")
        return 0

    if args.dry_run:
        print("-" * 78)
        for i, r in enumerate(picked, 1):
            print(f"{i:>3}. [lang={r['lang']}] {r['title'][:64]}")
            print(f"      {r['ts']}")
        print("-" * 78)
        print("[dry-run] 未调用模型，未写库。")
        return 0

    rows = load_full(conn, [r["id"] for r in picked])
    del picked

    cfg = llm.load_llm_config()
    backend = llm.build_backend(cfg)
    desc = backend.describe()
    print(f"后端 {cfg['backend']}｜模型 {desc.get('model_id') or desc.get('model')}")
    print("-" * 78)

    t0 = time.time()
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        title = (r["title"] or "").strip()
        t1 = time.time()
        zh = normalize.translate_title(title, lang=r["lang"], backend=backend, cfg=cfg)
        if not zh:
            fail += 1
            print(f"{i:>3}. [失败] {title[:56]}")
            continue
        conn.execute("UPDATE articles SET title_zh = ? WHERE id = ?", (zh, r["id"]))
        conn.commit()
        ok += 1
        if not args.quiet or i % 25 == 0 or i == len(rows):
            print(f"{i:>3}. [{time.time() - t1:>4.1f}s] {zh[:46]}")
            print(f"     原文 {title[:56]}")
        if args.with_summary:
            normalize.ensure_article_chinese(conn, r, backend=backend, cfg=cfg)
        if args.sleep:
            time.sleep(args.sleep)

    wall = time.time() - t0
    print("-" * 78)
    print(f"完成：成功 {ok} / 失败 {fail}｜耗时 {wall:.1f}s"
          f"（平均 {wall / max(1, ok):.1f}s 一条）")

    # 标题译名会改变事件关键词的候选文本（keywords 优先取中文内容），
    # 所以译完顺手重算一遍 —— 纯自研算法，全库 0.3 秒，没有理由不做。
    if ok:
        res = keywords.tag_all(conn, verbose=False)
        print(f"已重算事件关键词标签：{res}")
    print(progress(conn))

    if fail:
        print(f"\n[注意] 有 {fail} 条翻译失败（模型偶尔会吐空串，重跑一次通常就好）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
