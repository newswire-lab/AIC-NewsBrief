# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""验证简报质量改造：目标字数是否随事实量走、重复是否被削掉。"""
import os, re, subprocess, sys, time

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src")], capture_output=True, text=True)
print("compileall rc =", rc.returncode, rc.stderr[-400:])

from src import brief as brief_mod, llm, store

rep = []
def w(s=""):
    rep.append(str(s))

SENT = re.compile(r"(?<=[。！？；;])\s*")

def dup_ratio(text):
    ss = [s.strip() for s in SENT.split(text or "") if len(s.strip()) >= 6]
    if len(ss) < 2:
        return 0.0
    gs = [{s[i:i + 4] for i in range(max(0, len(s) - 3))} for s in ss]
    flagged = set()
    for i in range(len(ss)):
        for j in range(i + 1, len(ss)):
            a, b = gs[i], gs[j]
            if a and b and len(a & b) / min(len(a), len(b)) >= 0.55:
                flagged.add(i); flagged.add(j)
    total = sum(len(s) for s in ss)
    return sum(len(ss[i]) for i in flagged) / total if total else 0.0


conn = store.connect()
store.init_db(conn)
cfg = llm.load_llm_config()
w("allow_expand = %s（应为 False）" % cfg["task"].get("allow_expand"))

# 按事实量挑三个代表性事件
cands = []
for r in conn.execute("SELECT event_id, COUNT(*) n FROM articles "
                      "WHERE event_id != '' GROUP BY event_id "
                      "HAVING n BETWEEN 1 AND 6").fetchall():
    material, sources = brief_mod.build_material(conn, r["event_id"])
    cands.append((brief_mod.fact_chars(material), r["event_id"], r["n"]))
cands.sort()
picks = [cands[0], cands[len(cands) // 2], cands[-1]] if cands else []
w("")
w("=" * 78)
w("目标字数：改前 vs 改后（改前 = min(300, max(120, 材料总长×0.45))）")
w("=" * 78)
for facts, eid, n in picks:
    material, _ = brief_mod.build_material(conn, eid)
    old = max(120, min(300, int(len(material) * 0.45)))
    w("  %s  %d 篇｜材料 %d 字，事实 %d 字 → 改前目标 **%d** 字，改后目标 **%d** 字"
      % (eid, n, len(material), facts, old,
         brief_mod.adaptive_brief_chars(material, 300)))

w("")
w("=" * 78)
w("实跑生成（三个不同事实量的事件）")
w("=" * 78)
backend = llm.get_backend(cfg)
for facts, eid, n in picks:
    t0 = time.time()
    res = brief_mod.generate_brief(conn, eid, cfg=cfg, backend=backend)
    b = res.get("brief") or ""
    ver = res.get("verification") or {}
    w("")
    w("  %s  %d 篇｜事实 %d 字" % (eid, n, facts))
    w("    目标 %s 字 → 实际 **%d 字**｜降级=%s accepted=%s｜耗时 %.1fs"
      % (res.get("brief_chars_target"), len(b), bool(res.get("_fallback")),
         ver.get("accepted"), time.time() - t0))
    w("    自动删除重复句：%s ｜ 残留重复占比 **%.0f%%**"
      % (res.get("_dropped_duplicates", 0), dup_ratio(b) * 100))
    w("    正文前 120 字：%s" % b[:120].replace("\n", " "))
    store.save_brief(conn, res)

open(_os.path.join(_DESKTOP, "_test_quality2.txt"), "w", encoding="utf-8").write("\n".join(rep))
print("report -> _test_quality2.txt lines=%d" % len(rep))
