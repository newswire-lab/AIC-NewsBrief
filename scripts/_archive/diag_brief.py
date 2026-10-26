# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""诊断：简报生成为什么慢。分别复现「界面路径」与「复用后端路径」并计时。"""
import os, sys, time, json

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src import brief as brief_mod, llm, store

rep = []
def w(s=""):
    rep.append(str(s))


def pick_event(conn, want_multi=True):
    sql = ("SELECT event_id, COUNT(*) n, COUNT(DISTINCT source_org) k "
           "FROM articles WHERE event_id != '' GROUP BY event_id ")
    sql += "HAVING k > 1 ORDER BY n DESC LIMIT 5" if want_multi else "ORDER BY n DESC LIMIT 5"
    rows = conn.execute(sql).fetchall()
    return rows[0]["event_id"] if rows else None


conn = store.connect()
store.init_db(conn)
eid = pick_event(conn)
row = conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT source_org) k, "
                   "SUM(LENGTH(COALESCE(NULLIF(summary_zh,''), summary))) chars "
                   "FROM articles WHERE event_id = ?", (eid,)).fetchone()
w("测试事件：%s  文章 %d 篇 / 机构 %d 家 / 材料约 %d 字符"
  % (eid, row["n"], row["k"], row["chars"] or 0))
cfg = llm.load_llm_config()
w("局部配置：max_new_tokens=%s temperature=%s do_sample=%s brief_chars=%s"
  % (cfg["local"].get("max_new_tokens"), cfg["local"].get("temperature"),
     cfg["local"].get("do_sample"), cfg["task"].get("brief_chars")))
w()

# ---------- A. 复现界面路径：每次点击都 build_backend(cfg, kind) ----------
w("=" * 78)
w("A. 界面路径（ui_generate 现在就是这么写的）：每次都新建后端实例")
w("=" * 78)
for i in (1, 2):
    t0 = time.time()
    backend = llm.build_backend(cfg, "local")       # 与 app.ui_generate 一致
    t_new = time.time() - t0
    t1 = time.time()
    try:
        res = brief_mod.generate_brief(conn, eid, cfg=cfg, backend=backend)
        err = None
    except Exception as exc:
        res, err = {}, "%s: %s" % (type(exc).__name__, exc)
    t_gen = time.time() - t1
    w("第 %d 次：新建后端 %.2fs ｜ 生成 %.1fs ｜ 合计 %.1fs"
      % (i, t_new, t_gen, t_new + t_gen))
    if err:
        w("     [异常] " + err)
    else:
        ver = res.get("verification") or {}
        w("     后端=%s 模型=%s elapsed_ms=%s 字数=%s 降级=%s accepted=%s"
          % (res.get("backend"), res.get("model"), res.get("elapsed_ms"),
             len(res.get("brief") or ""), bool(res.get("_fallback")),
             ver.get("accepted")))
        w("     result 键：%s" % sorted(res.keys()))
        ex = res.get("_extra") or res.get("extra") or {}
        w("     extra=%s" % json.dumps(ex, ensure_ascii=False)[:300])

# ---------- B. 复用同一个后端实例 ----------
w()
w("=" * 78)
w("B. 复用后端（先 get_backend 建一次，之后复用）")
w("=" * 78)
t0 = time.time()
shared = llm.get_backend(cfg)
w("首次取后端 %.2fs（此时还没加载权重，是懒加载）" % (time.time() - t0))
for i in (1, 2):
    t1 = time.time()
    res = brief_mod.generate_brief(conn, eid, cfg=cfg, backend=shared)
    t_gen = time.time() - t1
    ver = res.get("verification") or {}
    w("第 %d 次：生成 %.1fs ｜ 降级=%s accepted=%s 字数=%s"
      % (i, t_gen, bool(res.get("_fallback")), ver.get("accepted"),
         len(res.get("brief") or "")))

open(_os.path.join(_DESKTOP, "_diag_brief.txt"), "w", encoding="utf-8").write("\n".join(rep))
print("report -> _diag_brief.txt lines=%d" % len(rep))
