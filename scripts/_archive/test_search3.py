# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""验证关联词扩展检索：搜「俄乌」/「俄乌冲突」应命中近期相关新闻。"""
import os, subprocess, sys, time

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src")], capture_output=True, text=True)
print("compileall rc =", rc.returncode, rc.stderr[-500:])

from src import search, store

rep = []
def w(s=""):
    rep.append(str(s))

conn = store.connect()
store.init_db(conn)

w("=" * 78)
w("一、关键词扩展结果")
w("=" * 78)
for q in ("俄乌", "俄乌冲突", "俄乌局势", "无人机", "巴以", "个别词"):
    terms = search.parse_query(q)
    groups = search.expand_query(terms)
    for t, g in zip(terms, groups):
        w("  「%s」→ %d 个词：%s" % (t, len(g), "、".join(g)))

w()
w("=" * 78)
w("二、命中数对比（改前只能字面匹配）")
w("=" * 78)
for q in ("俄乌", "俄乌冲突", "乌克兰", "俄罗斯", "无人机", "巴以"):
    t0 = time.time()
    rows = search.search_articles(conn, q, hours=0, limit=8)
    ms = (time.time() - t0) * 1000
    w("  「%s」→ **%d 条**（%.0f ms）" % (q, len(rows), ms))
    for r in rows[:3]:
        w("     %.2f ｜ %s ｜ %s" % (r["score"], r["source_org"][:14],
                                     r["title"][:52]))

w()
w("=" * 78)
w("三、字面匹配 vs 关联词匹配（证明扩展真的起作用）")
w("=" * 78)
literal = 0
for r in conn.execute("SELECT COUNT(*) n FROM articles "
                      "WHERE title LIKE '%俄乌%' OR summary LIKE '%俄乌%' "
                      "OR content LIKE '%俄乌%' OR title_zh LIKE '%俄乌%'"):
    literal = r["n"]
w("  字面含「俄乌」的文章：**%d 篇**" % literal)
w("  关联词扩展后命中：**%d 篇**"
  % len(search.search_articles(conn, "俄乌", hours=0, limit=100)))

w()
w("=" * 78)
w("四、最近 72 小时的「俄乌」相关新闻（用户实际要的效果）")
w("=" * 78)
rows = search.search_articles(conn, "俄乌", hours=72, limit=10)
w("  命中 %d 条" % len(rows))
for i, r in enumerate(rows[:8], 1):
    w("  %d. %.2f ｜ %s ｜ %s" % (i, r["score"], r["source_org"][:12],
                                 r["title"][:56]))

open(_os.path.join(_DESKTOP, "_test_search3.txt"), "w", encoding="utf-8").write("\n".join(rep))
print("report -> _test_search3.txt lines=%d" % len(rep))
