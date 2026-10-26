# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""验证简报缓存：首次生成 vs 二次读缓存 vs 强制重新生成。"""
import os, sys, time

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import subprocess
rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src")], capture_output=True, text=True)
print("compileall rc =", rc.returncode, rc.stderr[-400:])

from src import app, store

rep = []
def w(s=""):
    rep.append(str(s))

conn = store.connect()
store.init_db(conn)
# 找一个还没有简报的、材料较薄的事件（避免等太久）
eid = conn.execute(
    "SELECT a.event_id FROM articles a WHERE a.event_id != '' "
    "AND a.event_id NOT IN (SELECT event_id FROM briefs) "
    "GROUP BY a.event_id ORDER BY COUNT(*) ASC, "
    "  SUM(LENGTH(COALESCE(NULLIF(a.summary_zh,''), a.summary))) ASC LIMIT 1").fetchone()
eid = eid["event_id"] if eid else None
row = conn.execute("SELECT COUNT(*) n, MIN(title) t FROM articles WHERE event_id = ?",
                   (eid,)).fetchone()
w("测试事件：%s  %d 篇  %s" % (eid, row["n"], (row["t"] or "")[:50]))

w("")
w("=" * 78)
w("1. 首次生成（无缓存）")
w("=" * 78)
conn.execute("DELETE FROM briefs WHERE event_id = ?", (eid,))
conn.commit()
t0 = time.time()
md, src = app.ui_generate(eid, "local")
w("  耗时 **%.1fs**｜简报 %d 字符｜来源块 %d 字符"
  % (time.time() - t0, len(md), len(src)))
w("  前 3 行：")
for line in md.splitlines()[:3]:
    w("    " + line[:90])
w("  含「来自本地缓存」提示：%s" % ("来自本地缓存" in md))
w("  含「事实一致性校验」：%s" % ("事实一致性校验" in md))

w("")
w("=" * 78)
w("2. 第二次点击（应读缓存、不调模型）")
w("=" * 78)
t0 = time.time()
md2, _ = app.ui_generate(eid, "local")
dt = time.time() - t0
w("  耗时 **%.2fs**" % dt)
w("  含「来自本地缓存」提示：%s" % ("来自本地缓存" in md2))
w("  与首次输出一致（除提示行）：%s" % (md2.replace("> 📌", "X").splitlines()[1:] ==
                                        md.splitlines()[1:] or "内容有差异，见下"))
w("  加速比：%.0fx" % (1.0 / max(dt, 1e-6) * 0)) if False else None

w("")
w("=" * 78)
w("3. 强制重新生成（force=True）")
w("=" * 78)
t0 = time.time()
md3, _ = app.ui_generate(eid, "local", force=True)
w("  耗时 **%.1fs**｜含缓存提示：%s" % (time.time() - t0, "来自本地缓存" in md3))

w("")
w("=" * 78)
w("4. 界面组件")
w("=" * 78)
demo = app.build_ui()
w("  build_ui() OK：组件 %d 个" % len(demo.blocks))

open(_os.path.join(_DESKTOP, "_test_cache.txt"), "w", encoding="utf-8").write("\n".join(rep))
print("report -> _test_cache.txt lines=%d" % len(rep))
