"""给**已有**文章补抓正文（存量回填）。

**为什么不用重跑采集**：``collect`` 靠 ``url_hash`` 去重，已入库的行会被直接跳过，
所以光改采集逻辑只能让**新文章**有正文，历史那批永远补不上。

用法：
    python scripts/backfill_bodies.py --dry-run            # 先看清单
    python scripts/backfill_bodies.py --limit 20           # 先补 20 条试水
    python scripts/backfill_bodies.py --source mod_gov_qwfb   # 只补某个源
    python scripts/backfill_bodies.py                      # 全量补（慢，建议分批）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import collect, dedup, fetch, parsers, parsers_html, store  # noqa: E402

MIN = collect.MIN_BODY_CHARS


def pick(conn, source, hours, limit, refresh):
    where = ["url != ''"]
    params = []
    if not refresh:
        where.append("length(COALESCE(content, '')) < ?")
        params.append(MIN)
    if source:
        where.append("(source_id = ? OR source_name = ?)")
        params += [source, source]
    if hours > 0:
        where.append("datetime(COALESCE(published_at, fetched_at)) >= datetime('now', ?)")
        params.append(f"-{int(hours)} hours")
    sql = ("SELECT id, source_name, url, length(COALESCE(content,'')) lc, title "
           "FROM articles WHERE " + " AND ".join(where) +
           " ORDER BY (lc IS NOT NULL), lc ASC, COALESCE(published_at, fetched_at) DESC")
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="给已有文章补抓正文")
    ap.add_argument("--source", default="", help="只处理某个 source_id 或 source_name")
    ap.add_argument("--hours", type=int, default=0, help="只看最近 N 小时，0=不限")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条，0=不限")
    ap.add_argument("--sleep", type=float, default=0.4, help="每条之间的间隔")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不抓取")
    ap.add_argument("--refresh", action="store_true", help="已有正文的也重抓一遍")
    args = ap.parse_args()

    conn = store.connect()
    store.init_db(conn)
    rows = pick(conn, args.source, args.hours, args.limit, args.refresh)
    print("=" * 78)
    print("正文回填｜待处理 %d 条（正文 < %d 字视为缺失）" % (len(rows), MIN))
    print("=" * 78)
    if not rows:
        print("没有需要处理的文章。")
        return 0
    if args.dry_run:
        for r in rows[:60]:
            print("  %-22s 正文 %5d 字  %s" % (r["source_name"][:22], r["lc"], r["title"][:44]))
        print("\n[dry-run] 未抓取。")
        return 0

    sess = fetch.build_session()
    ok = fail = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        try:
            # 注意：这里 fetch 是**模块**（上面 from src import fetch），
            # 真正的抓取函数是 fetch.fetch（collect.py 里是 from .fetch import fetch）
            res = fetch.fetch(r["url"], session=sess)
            raw = getattr(res, "content", None)
            body = parsers_html.extract_body(
                parsers.decode_bytes(raw or b"", getattr(res, "encoding", None))
            ) if raw else ""
        except Exception as exc:
            fail += 1
            print("%4d. [抓取失败] %s %s" % (i, type(exc).__name__, r["url"][:56]))
            continue
        if len(body) <= r["lc"]:
            fail += 1
            print("%4d. [仍为空]   %s" % (i, r["url"][:64]))
            continue
        conn.execute("UPDATE articles SET content = ?, content_fp = ? WHERE id = ?",
                     (body[:20000], dedup.text_fingerprint(body), r["id"]))
        conn.commit()
        ok += 1
        print("%4d. [%5d -> %5d 字] %s" % (i, r["lc"], len(body), r["title"][:44]))
        if args.sleep:
            time.sleep(args.sleep)

    print("-" * 78)
    print("完成：补上 %d 条 / 失败或仍为空 %d 条｜耗时 %.1fs" % (ok, fail, time.time() - t0))
    rest = conn.execute("SELECT COUNT(*) c FROM articles WHERE url != '' "
                        "AND length(COALESCE(content,'')) < ?", (MIN,)).fetchone()["c"]
    print("仍缺正文：%d 条" % rest)
    print("\n提示：正文变了，建议重跑一次聚类与评分：")
    print("   python -m src.cli cluster && python -m src.cli score")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
