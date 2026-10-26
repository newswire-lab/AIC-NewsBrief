# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""检索 + 翻译预热的自查脚本（结果写 UTF-8 报告，避免 GBK 控制台乱码）。"""
import os, sys, time, subprocess, traceback

ROOT = _ROOT
OUT = _os.path.join(_DESKTOP, "_test_search.txt")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

rep = []


def w(s=""):
    rep.append(str(s))


rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src"), os.path.join(ROOT, "scripts")],
                    capture_output=True, text=True)
w("compileall rc = %d" % rc.returncode)
if rc.stdout.strip():
    w("stdout: " + rc.stdout[-500:])
if rc.stderr.strip():
    w("stderr: " + rc.stderr[-1500:])
print("compileall rc =", rc.returncode)

import importlib
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
print("imports ok =", not bad)

w()
w("=" * 78)
w("一、关键词解析 parse_query")
w("=" * 78)
from src import search, store, keywords
for q in ["诺贝尔经济学奖", "无人机  反制", "Lockheed, PrSM", "  空格  分隔  ",
          "a b c d e f g h", "", "  ", "「引号」测试"]:
    w("  %-22r -> %s" % (q, search.parse_query(q)))

w()
w("=" * 78)
w("二、检索实测（含耗时）")
w("=" * 78)
conn = store.connect()
store.init_db(conn)
w("库存：%s" % search.coverage(conn))

QUERIES = [
    ("诺贝尔", 0),
    ("无人机", 0),
    ("hypersonic", 0),
    ("Lockheed", 0),
    ("飓风", 0),
    ("中国新闻社", 0),
    ("不存在的词xyzzy", 0),
    ("诺贝尔", 24),
    ("nucleus", 0),
]
for q, hrs in QUERIES:
    t0 = time.time()
    rows = search.search_articles(conn, q, hours=hrs, limit=10)
    ms = (time.time() - t0) * 1000
    w()
    w("[%s] hours=%d -> 命中 %d 条，%.0f ms" % (q, hrs, len(rows), ms))
    for i, r in enumerate(rows[:5], 1):
        w("  %d. 分%.2f | 标题命中%d 摘要命中%d | %s | %s"
          % (i, r["score"], r["hit_title"], r["hit_summary"],
             r["source_org"][:16], r["ts"]))
        w("     %s" % r["title"][:70])
        if r["translated"]:
            w("     原文: %s" % r["title_orig"][:66])
        w("     片段: %s" % (r["snippet"] or "")[:70])
        w("     url=%s" % r["url"][:80])

w()
w("=" * 78)
w("三、多关键词 AND 语义验证")
w("=" * 78)
one = search.search_articles(conn, "无人机", limit=100)
two = search.search_articles(conn, "无人机 反制", limit=100)
w("  「无人机」命中 %d 条；「无人机 反制」命中 %d 条（应为子集）" % (len(one), len(two)))
w("  子集关系成立：%s" % (set(r["url"] for r in two) <= set(r["url"] for r in one)))

w()
w("=" * 78)
w("四、翻译预热：待处理清单统计（不调模型）")
w("=" * 78)
import scripts.warm_translations as warm
for lim, hrs in ((40, 0), (200, 0), (200, 168)):
    sql, params = warm.build_query(lim, hrs, False)
    n = len(conn.execute(sql, params).fetchall())
    w("  limit=%d hours=%d -> 候选 %d 条" % (lim, hrs, n))
w("  " + warm.progress(conn))

row = conn.execute("SELECT COUNT(*) AS t FROM articles WHERE lang != 'zh'").fetchone()
w("  lang != 'zh' 的文章总数：%d" % row["t"])
row = conn.execute("SELECT COUNT(*) AS t FROM articles WHERE lang = 'zh'").fetchone()
w("  lang =  'zh' 的文章总数：%d" % row["t"])

# 抽查：标为 zh 但标题其实没有汉字的（说明 lang 字段不可靠的情况有多少）
suspect = 0
samples = []
for r in conn.execute("SELECT title, lang, source_name FROM articles WHERE lang = 'zh'"):
    t = r["title"] or ""
    han = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    if t and han == 0:
        suspect += 1
        if len(samples) < 8:
            samples.append((r["source_name"], t))
w("  「lang=zh 但标题里一个汉字都没有」的文章：%d 篇" % suspect)
for s in samples:
    w("     [%s] %s" % (s[0], s[1][:66]))

open(OUT, "w", encoding="utf-8").write("\n".join(rep))
print("report ->", OUT, "lines:", len(rep))
