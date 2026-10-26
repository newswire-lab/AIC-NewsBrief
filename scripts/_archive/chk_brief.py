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
r = c.execute("SELECT COUNT(*) n FROM briefs").fetchone()
print("briefs=%d" % r["n"])
for x in c.execute("SELECT event_id, created_at, backend, accepted, headline "
                   "FROM briefs ORDER BY created_at DESC LIMIT 4"):
    print("  %s | %s | accepted=%s | %s"
          % (x["created_at"], x["backend"], x["accepted"], (x["headline"] or "")[:40]))
r = c.execute("SELECT COUNT(*) t, SUM(CASE WHEN title_zh != '' THEN 1 ELSE 0 END) d "
              "FROM articles").fetchone()
print("articles=%d translated=%d" % (r["t"], r["d"] or 0))
r = c.execute("SELECT COUNT(*) n FROM event_tags").fetchone()
print("event_tags=%d" % r["n"])
