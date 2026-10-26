# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""诊断简报质量：重复度、字数目标 vs 素材实际事实量。"""
import os, re, sys, json

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from src import brief as brief_mod, store

rep = []
def w(s=""):
    rep.append(str(s))

SENT_SPLIT = re.compile(r"(?<=[。！？；;])\s*")
BOILER = re.compile(r"^\[\d+\] 来源：.*$|^时间：.*$|^标题：.*$|^正文：|"
                    r"（已由开源模型归一化为中文事实要点）|"
                    r"（外文材料，[^）]*）|（该源未提供正文，仅有标题）",
                    re.M)


def sentences(text):
    return [s.strip() for s in SENT_SPLIT.split(text or "") if len(s.strip()) >= 6]


def dup_ratio(text):
    """重复内容占比：把所有与别的句子高度重合的句子的字数加总 / 总字数。"""
    ss = sentences(text)
    if len(ss) < 2:
        return 0.0, []
    def ngrams(s, n=4):
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    gs = [ngrams(s) for s in ss]
    flagged, pairs = set(), []
    for i in range(len(ss)):
        for j in range(i + 1, len(ss)):
            a, b = gs[i], gs[j]
            if not a or not b:
                continue
            jac = len(a & b) / len(a | b)
            cont = len(a & b) / min(len(a), len(b))     # 包含度：短句被长句覆盖多少
            if jac >= 0.35 or cont >= 0.7:
                flagged.add(i)
                flagged.add(j)
                if len(pairs) < 6:
                    pairs.append((round(jac, 2), round(cont, 2), ss[i][:34], ss[j][:34]))
    total = sum(len(s) for s in ss)
    dup_chars = sum(len(ss[i]) for i in flagged)
    return (dup_chars / total if total else 0.0), pairs


conn = store.connect()
store.init_db(conn)

w("=" * 78)
w("已存简报的重复度")
w("=" * 78)
rows = conn.execute("SELECT event_id, payload_json FROM briefs "
                    "ORDER BY created_at DESC").fetchall()
for r in rows:
    try:
        p = json.loads(r["payload_json"])
    except Exception:
        continue
    b = p.get("brief") or ""
    dr, pairs = dup_ratio(b)
    srcs = p.get("sources") or []
    mat = sum(len(s.get("text") or "") for s in srcs)
    w("%s  后端=%s 降级=%s" % (r["event_id"], p.get("backend"), bool(p.get("_fallback"))))
    w("   简报 %d 字 / 素材正文 %d 字 / 目标 %s 字 / 句数 %d"
      % (len(b), mat, p.get("brief_chars_target"), len(sentences(b))))
    w("   **重复占比 %.0f%%**" % (dr * 100))
    for jac, cont, s1, s2 in pairs[:3]:
        w("      相似(jac=%s cont=%s)：%s  ≈  %s" % (jac, cont, s1, s2))

# ---------- 目标字数是怎么算出来的 ----------
w()
w("=" * 78)
w("目标字数 vs 素材实际事实量")
w("=" * 78)
for r in conn.execute(
        "SELECT event_id, COUNT(*) n FROM articles WHERE event_id != '' "
        "GROUP BY event_id HAVING n BETWEEN 1 AND 3 "
        "ORDER BY RANDOM() LIMIT 6").fetchall():
    eid = r["event_id"]
    material, sources = brief_mod.build_material(conn, eid)
    facts = BOILER.sub("", material)
    facts = re.sub(r"\s+", "", facts)
    raw = re.sub(r"\s+", "", material)
    w("%s  %d 篇｜材料 %d 字（去掉版式只剩 **%d 字**）→ 目标 %d 字"
      % (eid, r["n"], len(raw), len(facts),
         brief_mod.adaptive_brief_chars(material, 300)))

open(_os.path.join(_DESKTOP, "_diag_quality.txt"), "w", encoding="utf-8").write("\n".join(rep))
print("report -> _diag_quality.txt lines=%d" % len(rep))
