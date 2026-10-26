# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
# -*- coding: utf-8 -*-
"""归因测量：把「生成简报慢」拆成三段——权重加载 / 实时归一化 / 正式生成。

测法：
  1. 预热后端，量出**权重加载**到底多久；
  2. 路径 A：复现修复前的界面写法（每次 build_backend 新建实例）→ 看重复加载；
  3. 路径 B：复用同一个后端 → 看生成本身多久；
  4. 单独量 build_material（中文事件 vs 外文事件），分离**实时归一化**的开销。
"""
import os, sys, time

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from src import brief as brief_mod, llm, store

rep = []
def w(s=""):
    rep.append(str(s))
def dump():
    open(_os.path.join(_DESKTOP, "_diag2.txt"), "w", encoding="utf-8").write("\n".join(rep))

conn = store.connect()
store.init_db(conn)

def event_of(where, order="n DESC"):
    rows = conn.execute(
        "SELECT event_id, COUNT(*) n, COUNT(DISTINCT source_org) k, "
        "  SUM(CASE WHEN lang != 'zh' THEN 1 ELSE 0 END) n_foreign "
        "FROM articles WHERE event_id != '' AND " + where +
        " GROUP BY event_id ORDER BY " + order + " LIMIT 3").fetchall()
    return rows

cn = event_of("lang = 'zh'")
en = event_of("lang != 'zh'")
w("中文事件候选：")
for r in cn:
    w("  %s  %d 篇 / %d 机构 / 外文 %d 篇" % (r["event_id"], r["n"], r["k"], r["n_foreign"]))
w("外文事件候选：")
for r in en:
    w("  %s  %d 篇 / %d 机构 / 外文 %d 篇" % (r["event_id"], r["n"], r["k"], r["n_foreign"]))
dump()

cn_id = cn[0]["event_id"]
cfg = llm.load_llm_config()

# ---------- 1. 权重加载 ----------
w()
w("=" * 78)
w("1. 权重加载耗时（只在缓存为空时发生一次）")
w("=" * 78)
t0 = time.time()
shared = llm.get_backend(cfg, "local")
t_get = time.time() - t0
w("  get_backend（只建对象、不加载）        %.2fs" % t_get)
t0 = time.time()
shared.warm()
t_warm = time.time() - t0
w("  warm()（真正把权重读进显存）            **%.1fs**" % t_warm)
t0 = time.time()
again = llm.get_backend(cfg, "local")
w("  再取一次 get_backend                 %.3fs（%.3fs 是缓存命中）"
  % (time.time() - t0, time.time() - t0))
w("  两次拿到的是同一实例：%s" % (again is shared))
dump()

# ---------- 2. 路径 A：修复前界面写法 ----------
w()
w("=" * 78)
w("2. 路径 A —— 修复前 app.ui_generate 的写法：每次 build_backend(cfg, kind)")
w("=" * 78)
for i in (1, 2):
    backend = llm.build_backend(cfg, "local")     # 新实例，self._model 为 None
    t0 = time.time()
    try:
        res = brief_mod.generate_brief(conn, cn_id, cfg=cfg, backend=backend)
        err = None
    except Exception as exc:
        res, err = {}, "%s: %s" % (type(exc).__name__, exc)
    dt = time.time() - t0
    w("  第 %d 次：%.1fs%s" % (i, dt, ("   [异常] " + err) if err else ""))
    if not err:
        ver = res.get("verification") or {}
        w("      目标字数=%s 实际=%s 降级=%s accepted=%s elapsed_ms=%s"
          % (res.get("brief_chars_target"), len(res.get("brief") or ""),
             bool(res.get("_fallback")), ver.get("accepted"), res.get("elapsed_ms")))
        gm = res.get("generation_meta") or {}
        w("      input_tokens=%s output_tokens=%s load_mode=%s"
          % (gm.get("input_tokens"), gm.get("output_tokens"), gm.get("load_mode")))
    dump()

# ---------- 3. 路径 B：复用后端 ----------
w()
w("=" * 78)
w("3. 路径 B —— 修复后的写法：复用 llm.get_backend(cfg, kind)")
w("=" * 78)
for i in (1, 2):
    t0 = time.time()
    res = brief_mod.generate_brief(conn, cn_id, cfg=cfg, backend=shared)
    dt = time.time() - t0
    ver = res.get("verification") or {}
    w("  第 %d 次：%.1fs  目标字数=%s 实际=%s 降级=%s accepted=%s"
      % (i, dt, res.get("brief_chars_target"), len(res.get("brief") or ""),
         bool(res.get("_fallback")), ver.get("accepted")))
    gm = res.get("generation_meta") or {}
    if gm:
        w("      input_tokens=%s output_tokens=%s" % (gm.get("input_tokens"),
                                                      gm.get("output_tokens")))
    if res.get("_expanded"):
        w("      触发了扩写（第二次模型调用）")
    dump()

# ---------- 4. 实时归一化开销 ----------
w()
w("=" * 78)
w("4. build_material 里的「实时归一化」开销")
w("=" * 78)
for label, r in (("中文事件", cn[0]), ("外文事件", en[0])):
    eid = r["event_id"]
    before = conn.execute(
        "SELECT COUNT(*) n FROM articles WHERE event_id = ? AND lang != 'zh' "
        "AND (summary_zh IS NULL OR summary_zh = '')", (eid,)).fetchone()["n"]
    t0 = time.time()
    material, sources = brief_mod.build_material(conn, eid, backend=shared, cfg=cfg)
    dt = time.time() - t0
    after = conn.execute(
        "SELECT COUNT(*) n FROM articles WHERE event_id = ? AND lang != 'zh' "
        "AND (summary_zh IS NULL OR summary_zh = '')", (eid,)).fetchone()["n"]
    w("  %s %s：%d 篇 / 外文待归一化 %d 篇 → 本次实际归一化 **%d 篇**"
      % (label, eid, r["n"], before, before - after))
    w("      build_material 耗时 **%.1fs**，材料 %d 字符"
      % (dt, len(material)))
    notes = sum(1 for s in sources if s.get("normalized"))
    w("      标注为「已归一化」的来源数：%d" % notes)
    dump()

w()
w("=" * 78)
w("结论汇总")
w("=" * 78)
w("  权重加载一次：%.1fs（修复前**每次点击**都要付这一笔）" % t_warm)
dump()
print("report -> _diag2.txt lines=%d" % len(rep))
