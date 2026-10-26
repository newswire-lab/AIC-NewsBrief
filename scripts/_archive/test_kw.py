# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""关键词标签自查：跨进程确定性 + 输出质量抽样。

用法：python _test_kw.py            跑对比
      python _test_kw.py --dump X   （内部使用）打标签并把结果写 X
"""
import os, sys, json, argparse, subprocess, time

ROOT = _ROOT
TMP = _DESKTOP
OUT = os.path.join(TMP, "_test_kw_v6.txt")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

ap = argparse.ArgumentParser()
ap.add_argument("--dump", default=None)
args, _ = ap.parse_known_args()

from src import keywords, store, cluster   # noqa: E402

conn = store.connect()
store.init_db(conn)

if args.dump:
    t0 = time.time()
    res = keywords.tag_all(conn, verbose=False)
    rows = conn.execute(
        "SELECT event_id, tags, n_docs FROM event_tags ORDER BY event_id").fetchall()
    payload = {"result": res,
               "tags": {r["event_id"]: [r["tags"], r["n_docs"]] for r in rows}}
    with open(args.dump, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=1)
    print("dumped %d tags in %.1fs" % (len(rows), time.time() - t0))
    raise SystemExit(0)

rep = []
def w(s=""):
    rep.append(str(s))

w("MIN_LEN=%d MAX_LEN=%d MAX_TAG_CHARS=%d TITLE_BOOST=%s"
  % (keywords.MIN_LEN, keywords.MAX_LEN, keywords.MAX_TAG_CHARS,
     keywords.TITLE_BOOST))
w("候选生成自测：%s" % keywords._candidates("长征丨长征精神薪火相传")[:12])
w("最长候选长度：%d（应该 <= MAX_LEN）"
  % max(len(x) for x in keywords._candidates("习近平经济思想引领中国加快发展新质生产力")))

# ---------------- 1. 跨进程确定性 ----------------
w("=" * 78)
w("一、跨进程确定性（这是本项目不做大模型标签的核心理由，必须成立）")
w("=" * 78)
dumps = []
for seed in ("0", "1", "99999"):
    out = os.path.join(TMP, "_kw_dump_%s.json" % seed)
    env = dict(os.environ, PYTHONHASHSEED=seed)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--dump", out],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        w("  [失败] seed=%s rc=%d\n%s\n%s" % (seed, r.returncode, r.stdout[-800:], r.stderr[-2000:]))
        break
    w("  seed=%-6s %s" % (seed, r.stdout.strip()))
    dumps.append(open(out, encoding="utf-8").read())

if len(dumps) == 3:
    same = dumps[0] == dumps[1] == dumps[2]
    w("  三个 PYTHONHASHSEED 下输出**完全一致**：%s" % same)
    if not same:
        a = json.loads(dumps[0])["tags"]
        b = json.loads(dumps[1])["tags"]
        diff = [k for k in a if a[k] != b.get(k)]
        w("  不一致的事件数：%d / %d" % (len(diff), len(a)))
        for k in diff[:5]:
            w("     %s\n       A=%s\n       B=%s" % (k, a[k], b.get(k)))

# ---------------- 2. 回归断言：旧版坏标签不得再出现 ----------------
BAD = ["近平总书记引", "领健康中国建", "总书记引领健", "记引领健康中",
       "征丨长征精神", "长征精神薪火", "总书记关切事", "习近平出席美",
       "席美国总统特", "普在白宫举行"]
w()
w("=" * 78)
w("二、回归断言：修复前实测出现的坏标签，现在必须一个都不剩")
w("=" * 78)
tags = json.loads(dumps[0])["tags"] if dumps else {}
alltags = {eid: t for eid, (t, nd) in tags.items()}
for bad in BAD:
    hits = [eid for eid, t in alltags.items() if bad in t.split("、")]
    w("  %-12s 仍作为标签出现的事件数：%d" % (bad, len(hits)))
    assert not hits, "旧版坏标签 %s 仍然存在！" % bad
w("  断言全部通过 ✅")
pipe = [eid for eid, t in alltags.items() if "丨" in t]
w("  标签里含分隔符「丨」的事件数：%d（应为 0）" % len(pipe))
assert not pipe

# ---------------- 3. 质量抽样 ----------------
tags = json.loads(dumps[0])["tags"] if dumps else {}
w()
w("=" * 78)
w("二、标签数量分布（检验去冗是否过于激进）")
w("=" * 78)
from collections import Counter
dist = Counter()
empty = 0
for eid, (t, nd) in tags.items():
    n = len([x for x in t.split("、") if x]) if t else 0
    if n == 0:
        empty += 1
    dist[n] += 1
w("  事件总数 %d，空标签 %d" % (len(tags), empty))
for n in sorted(dist):
    w("    %d 个标签：%d 个事件（%.0f%%）" % (n, dist[n], dist[n] * 100.0 / max(1, len(tags))))
w("  平均标签数：%.2f" % (sum(n * c for n, c in dist.items()) / max(1, len(tags))))

w()
w("=" * 78)
w("三、修复前的问题样例（回归验证）")
w("=" * 78)
for eid in ("evt_4f4f77191fd098", "evt_4662c89e718b81", "evt_6edab3d71481cf"):
    row = conn.execute(
        "SELECT MIN(title) AS t, COUNT(*) AS n FROM articles WHERE event_id = ?",
        (eid,)).fetchone()
    w("  %s  [%s 篇]" % (eid, row["n"]))
    w("     标题：%s" % (row["t"] or "")[:70])
    w("     标签：%s" % (tags.get(eid, ["（无）"])[0]))

w()
w("=" * 78)
w("四、多机构印证事件（最需要标签说清楚“是什么”的场景）")
w("=" * 78)
for r in conn.execute(
        "SELECT event_id, tags FROM event_tags WHERE event_id IN ("
        "  SELECT event_id FROM articles WHERE event_id != '' "
        "  GROUP BY event_id HAVING COUNT(DISTINCT source_org) > 1) ORDER BY event_id"):
    ttl = conn.execute("SELECT MIN(title) AS t FROM articles WHERE event_id = ?",
                       (r["event_id"],)).fetchone()["t"]
    w("  %s" % (ttl or "")[:66])
    w("     -> %s" % r["tags"])

w()
w("=" * 78)
w("五、中文源 / 英文源分别抽样")
w("=" * 78)
w("  — 中国新闻社（中文）—")
for r in conn.execute(
        "SELECT MIN(a.title) AS t, t2.tags FROM articles a "
        "JOIN event_tags t2 ON t2.event_id = a.event_id "
        "WHERE a.source_org = '中国新闻社' AND a.event_id != '' "
        "GROUP BY a.event_id LIMIT 6"):
    w("     %s" % (r["t"] or "")[:56])
    w("        -> %s" % r["tags"])
w("  — 英文源 —")
for r in conn.execute(
        "SELECT MIN(a.title) AS t, t2.tags FROM articles a "
        "JOIN event_tags t2 ON t2.event_id = a.event_id "
        "WHERE a.lang != 'zh' AND a.event_id != '' "
        "GROUP BY a.event_id LIMIT 6"):
    w("     %s" % (r["t"] or "")[:56])
    w("        -> %s" % r["tags"])

open(OUT, "w", encoding="utf-8").write("\n".join(rep))
print("report ->", OUT, "lines:", len(rep))
