# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""确认「生成简报 / 重新生成」两个绑定的接线与并发配置。"""
import os, sys

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from src import app

demo = app.build_ui()
fns = getattr(demo, "fns", {}) or {}
print("组件数 =", len(demo.blocks), "｜事件绑定数 =", len(fns))
hits = []
for fid, fn in fns.items():
    name = getattr(fn, "fn", None)
    label = getattr(fn, "__name__", None) or getattr(getattr(name, "func", None), "__name__", None) \
        or type(name).__name__
    cid = getattr(fn, "concurrency_id", None)
    lim = getattr(fn, "concurrency_limit", None)
    if "ui_generate" in str(label) or "partial" in str(label):
        hits.append((fid, str(label), cid, lim))
for h in hits:
    print("  binding", h[0], "|", h[1], "| concurrency_id =", h[2], "| limit =", h[3])
print("生成类绑定数 =", len(hits), "（应为 2：生成 + 重新生成）")

# 直接验证 force 语义
from src import store
conn = store.connect()
row = conn.execute("SELECT event_id FROM briefs WHERE brief != '' LIMIT 1").fetchone()
if row:
    eid = row["event_id"]
    import time
    t0 = time.time()
    md, src = app.ui_generate(eid, "local")
    dt = time.time() - t0
    print("对已有简报的事件调 ui_generate：%.3fs｜命中缓存=%s｜简报 %d 字符"
          % (dt, "来自本地缓存" in md, len(md)))
