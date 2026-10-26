# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
import os, sys

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from src import store

c = store.connect()
r = c.execute("SELECT COUNT(*) t, SUM(CASE WHEN title_zh != '' THEN 1 ELSE 0 END) d "
              "FROM articles").fetchone()
print("articles=%d translated=%d" % (r["t"], r["d"] or 0))
r = c.execute("SELECT COUNT(*) n FROM event_tags").fetchone()
print("event_tags=%d" % r["n"])
r = c.execute("SELECT COUNT(*) n FROM articles WHERE title_zh != '' AND event_id != ''").fetchone()
print("translated_with_event=%d" % r["n"])

lines = ["== title_zh 抽样（原 -> 译）=="]
for x in c.execute("SELECT source_org, title, title_zh FROM articles "
                   "WHERE title_zh != '' ORDER BY COALESCE(published_at, fetched_at) DESC "
                   "LIMIT 20"):
    lines.append("[%s]\n  原: %s\n  译: %s" % (x["source_org"], x["title"][:78], x["title_zh"]))

lines.append("")
lines.append("== 含中文译名的事件，其关键词标签 ==")
for x in c.execute(
        "SELECT t.event_id, t.tags, t.n_docs, "
        "  (SELECT MIN(title) FROM articles a WHERE a.event_id = t.event_id) AS ttl, "
        "  (SELECT COUNT(*) FROM articles a WHERE a.event_id = t.event_id "
        "     AND a.title_zh != '') AS nt "
        "FROM event_tags t WHERE t.event_id IN "
        "  (SELECT DISTINCT event_id FROM articles WHERE title_zh != '' AND event_id != '') "
        "ORDER BY nt DESC LIMIT 12"):
    lines.append("[%s 篇中 %d 篇有译名] %s\n   -> %s"
                 % (x["n_docs"], x["nt"], (x["ttl"] or "")[:60], x["tags"]))

open(_os.path.join(_DESKTOP, "_chk.txt"), "w", encoding="utf-8").write("\n".join(lines))
print("report -> _chk.txt lines=%d" % len(lines))
