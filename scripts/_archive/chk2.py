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
from src import store, keywords

c = store.connect()
print("tag_all:", keywords.tag_all(c, verbose=False))

rows = c.execute(
    "SELECT a.event_id, COUNT(*) n, "
    "  SUM(CASE WHEN a.title_zh != '' THEN 1 ELSE 0 END) nt, "
    "  MIN(COALESCE(NULLIF(a.title_zh, ''), a.title)) ttl "
    "FROM articles a WHERE a.event_id != '' GROUP BY a.event_id "
    "HAVING nt > 0 ORDER BY nt DESC LIMIT 18").fetchall()

lines = ["== 已有中文译名的事件：重算后的标签 =="]
cn = 0
for r in rows:
    tag = c.execute("SELECT tags FROM event_tags WHERE event_id = ?",
                    (r["event_id"],)).fetchone()
    tags = tag["tags"] if tag else "(无)"
    has_cn = any("\u4e00" <= ch <= "\u9fff" for ch in tags)
    cn += 1 if has_cn else 0
    lines.append("[%d/%d 篇有译名] %s\n   -> %s\n   （标签含汉字：%s）"
                 % (r["nt"], r["n"], (r["ttl"] or "")[:64], tags, has_cn))

lines.append("")
lines.append("标签含汉字的事件数：%d / %d" % (cn, len(rows)))
lines.append("")
lines.append("== 对照：未翻译的英文事件（标签应仍为英文）==")
for r in c.execute(
        "SELECT a.event_id, MIN(a.title) ttl FROM articles a WHERE a.event_id != '' "
        "AND a.lang != 'zh' GROUP BY a.event_id LIMIT 6"):
    tag = c.execute("SELECT tags FROM event_tags WHERE event_id = ?",
                    (r["event_id"],)).fetchone()
    lines.append("%s\n   -> %s" % ((r["ttl"] or "")[:62], tag["tags"] if tag else "(无)"))

open(_os.path.join(_DESKTOP, "_chk2.txt"), "w", encoding="utf-8").write("\n".join(lines))
print("zh_tagged=%d/%d" % (cn, len(rows)))
print("report -> _chk2.txt lines=%d" % len(lines))
