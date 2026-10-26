# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""第二轮自查：折叠同稿、按来源检索、翻译预热候选挑选。"""
import os, sys, time, subprocess, importlib

ROOT = _ROOT
OUT = _os.path.join(_DESKTOP, "_test_search2.txt")
sys.path.insert(0, ROOT)
os.chdir(ROOT)
rep = []
def w(s=""):
    rep.append(str(s))

rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src"), os.path.join(ROOT, "scripts")],
                    capture_output=True, text=True)
w("compileall rc = %d %s%s" % (rc.returncode, rc.stdout[-300:], rc.stderr[-600:]))
print("compileall rc =", rc.returncode)

mods = ["config", "models", "fetch", "parsers", "parsers_html", "dedup", "similarity",
        "cluster", "credibility", "normalize", "store", "collect", "llm", "brief",
        "verify", "scheduler", "keywords", "search", "app", "cli"]
bad = []
for m in mods:
    try:
        importlib.import_module("src." + m)
    except Exception as exc:
        bad.append("%s -> %s: %s" % (m, type(exc).__name__, exc))
w("模块导入 %d/%d" % (len(mods) - len(bad), len(mods)))
for b in bad:
    w("  [失败] " + b)
print("imports bad =", bad)

from src import search, store, cluster, credibility, app
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import warm_translations as warm

conn = store.connect()
store.init_db(conn)

w()
w("=" * 78)
w("一、同一通稿折叠")
w("=" * 78)
th = conn.execute(
    "SELECT COUNT(*) AS t, SUM(CASE WHEN title_hash != '' THEN 1 ELSE 0 END) AS h "
    "FROM articles").fetchone()
w("  title_hash 非空的文章：%d/%d" % (h := (th["h"] or 0), th["t"]))

for q in ["hypersonic", "Lockheed Martin unveils", "无人机"]:
    rows = search.search_articles(conn, q, hours=0, limit=20)
    multi = [r for r in rows if r["dup_sites"] > 1]
    w("  「%s」-> %d 行，其中被折叠过的 %d 行" % (q, len(rows), len(multi)))
    for r in rows[:4]:
        tag = "  [%d 站同稿: %s]" % (r["dup_sites"], "、".join(r["dup_names"])) \
            if r["dup_sites"] > 1 else ""
        w("     %.2f  %s%s" % (r["score"], r["title"][:56], tag))

w()
w("=" * 78)
w("二、按来源名检索（新增能力）")
w("=" * 78)
for q in ["中国新闻社", "新华社 无人机", "DefenseMirror"]:
    t0 = time.time()
    rows = search.search_articles(conn, q, hours=0, limit=8)
    w("  「%s」-> %d 行，%.0f ms" % (q, len(rows), (time.time() - t0) * 1000))
    for r in rows[:3]:
        w("     %.2f 标题命中%d 摘要%d 来源%d | %s | %s"
          % (r["score"], r["hit_title"], r["hit_summary"], r["hit_source"],
             r["source_org"][:14], r["title"][:48]))

w()
w("=" * 78)
w("三、排序合理性：标题命中必须排在正文命中前")
w("=" * 78)
rows = search.search_articles(conn, "无人机", hours=0, limit=30)
ok = True
prev = 99
for r in rows:
    if r["hit_title"] == 0 and prev == 1:
        pass
    prev = 1 if r["hit_title"] else 0
titles = [r for r in rows if r["hit_title"]]
others = [r for r in rows if not r["hit_title"] and not r["hit_summary"]]
w("  标题命中 %d 条，正文命中 %d 条" % (len(titles), len(others)))
if titles and others:
    w("  最低标题命中分 %.2f vs 最高正文命中分 %.2f -> 顺序正确：%s"
      % (min(t["score"] for t in titles), max(o["score"] for o in others),
         min(t["score"] for t in titles) > max(o["score"] for o in others)))

w()
w("=" * 78)
w("四、翻译预热：候选挑选（不调模型）")
w("=" * 78)
w("  " + warm.progress(conn))
for lim in (40, 200, 1000):
    picked = warm.select_pending(conn, lim, 0, False)
    w("  --limit %-5d -> 实选 %d 条" % (lim, len(picked)))
picked = warm.select_pending(conn, 10, 0, False)
w("  抽样前 10 条待译标题：")
for r in picked:
    w("     [lang=%s] %s" % (r["lang"], r["title"][:62]))

w()
w("=" * 78)
w("五、界面组件与搜索回调")
w("=" * 78)
rows_t, note = app.ui_search("诺贝尔", 0, 30)
w("  ui_search('诺贝尔', 0, 30) -> %d 行" % len(rows_t))
w("  提示语：\n     " + note.replace("\n", "\n     "))
w("  表头（%d 列）：%s" % (len(app.SEARCH_HEADERS), " | ".join(app.SEARCH_HEADERS)))
if rows_t:
    w("  首行：%s" % str(rows_t[0])[:260])

rows_t2, note2 = app.ui_search("", 0, 30)
w("  ui_search('') -> %d 行（应为 0，且给出提示）" % len(rows_t2))
rows_t3, note3 = app.ui_search("zzzz不存在", 0, 30)
w("  ui_search('zzzz不存在') -> %d 行" % len(rows_t3))

demo = app.build_ui()
w("  build_ui() OK：组件 %d 个" % len(demo.blocks))

open(OUT, "w", encoding="utf-8").write("\n".join(rep))
print("report ->", OUT, "lines:", len(rep))
