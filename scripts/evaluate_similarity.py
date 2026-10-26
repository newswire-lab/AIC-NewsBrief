"""评估语义相似度阈值：确定能把"同一事件的不同措辞"归并、又不误合并的取值。

同时做人工可判断的样例检验（sanity check），避免只看统计数字。
"""

from __future__ import annotations

import collections
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import store                                          # noqa: E402
from src.similarity import TitleSimilarityIndex, score_pair    # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLDS = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70)

SANITY_PAIRS = [
    # (期望, 说明, a, b)
    ("高", "同一事件·标点差异", "习近平抵达华盛顿，开始对美国进行国事访问",
     "习近平抵达华盛顿对美国进行国事访问"),
    ("高", "同一事件·动词差异", "陆军某旅开展飞行训练", "陆军某旅组织飞行训练"),
    ("高", "同一事件·省略", "海军某护卫舰支队开展海上多科目训练",
     "海军某护卫舰支队开展海上多科目训练"),
    ("低", "不同军种·不同地点", "陆军某旅开展飞行训练", "海军某大队开展海上训练"),
    ("低", "同句式·不同对象", "习近平会见西班牙首相桑切斯",
     "习近平会见阿联酋阿布扎比王储哈立德"),
    ("低", "完全不同主题", "2026年9月国防部例行记者会文字实录",
     "Greenland welcomes US military pact but trust still shaky"),
    ("低", "同一装备·不同事件", "F-16 fighter jet crashes at US air base",
     "US Air Force grounds F-16 fleet for inspection"),
]


def main() -> int:
    conn = store.connect()
    rows = conn.execute(
        "SELECT source_id, source_name, source_org, region, title "
        "FROM articles WHERE title != ''"
    ).fetchall()
    docs = [(str(i), r["title"]) for i, r in enumerate(rows)]

    index = TitleSimilarityIndex(docs)
    out = []
    out.append("参与评估的文章: %d 篇" % index.size())
    out.append("")

    # ---- 1. 人工样例检验 ----
    out.append("=" * 78)
    out.append("样例检验（人眼可判断）")
    out.append("-" * 78)
    for expect, why, a, b in SANITY_PAIRS:
        s = score_pair(a, b)
        flag = "OK " if ((expect == "高") == (s >= 0.5)) else "!! "
        out.append("%s 期望%s  相似度=%.3f  (%s)" % (flag, expect, s, why))
        out.append("      A: %s" % a[:52])
        out.append("      B: %s" % b[:52])
    out.append("")

    # ---- 2. 阈值扫描 ----
    pair_sims = []
    for i in range(index.size()):
        for j in index.candidates(i, topk=25):
            if j <= i:
                continue
            sim = index.similarity(i, j)
            if sim <= 0:
                continue
            pair_sims.append((sim, i, j))
    out.append("候选对总数（倒排索引剪枝后）: %d" % len(pair_sims))
    out.append("")
    out.append("%-8s %8s %8s %8s %10s" % ("阈值", "合计", "同机构", "跨机构", "跨机构中文"))
    for thr in THRESHOLDS:
        sel = [p for p in pair_sims if p[0] >= thr]
        same = [p for p in sel if rows[p[1]]["source_org"] == rows[p[2]]["source_org"]]
        cross = [p for p in sel if rows[p[1]]["source_org"] != rows[p[2]]["source_org"]]
        cn_cross = [p for p in cross
                    if rows[p[1]]["region"] == "cn" and rows[p[2]]["region"] == "cn"]
        out.append("%-8.2f %8d %8d %8d %10d"
                   % (thr, len(sel), len(same), len(cross), len(cn_cross)))
    out.append("")

    # ---- 3. 跨机构样例（人工判断是否真同一事件）----
    cross_all = [p for p in pair_sims
                 if p[0] >= 0.45 and rows[p[1]]["source_org"] != rows[p[2]]["source_org"]]
    cross_all.sort(reverse=True)
    out.append("=" * 78)
    out.append("跨机构候选对（相似度>=0.45，按相似度降序，最多 30 对）")
    out.append("-" * 78)
    if not cross_all:
        out.append("  （无）")
    for sim, i, j in cross_all[:30]:
        out.append("[%.3f] %s <%s>" % (sim, rows[i]["title"][:38], rows[i]["source_org"]))
        out.append("        %s <%s>" % (rows[j]["title"][:38], rows[j]["source_org"]))
    out.append("")

    # ---- 4. 同机构高相似对：用于确认"同集团互转"仍能被抓到 ----
    same_hi = [p for p in pair_sims
               if p[0] >= 0.7 and rows[p[1]]["source_org"] == rows[p[2]]["source_org"]]
    out.append("=" * 78)
    out.append("同机构高相似对数量（相似度>=0.70）: %d" % len(same_hi))
    out.append("-" * 78)
    for sim, i, j in sorted(same_hi, reverse=True)[:8]:
        out.append("[%.3f] %s" % (sim, rows[i]["title"][:48]))
        out.append("        %s" % rows[j]["title"][:48])

    # ---- 5. 相似度分布 ----
    out.append("")
    out.append("=" * 78)
    out.append("相似度分布（候选对）")
    out.append("-" * 78)
    buckets = collections.Counter()
    for sim, _, _ in pair_sims:
        buckets[round(sim, 1)] += 1
    for key in sorted(buckets):
        out.append("  %.1f : %d" % (key, buckets[key]))

    log_path = os.path.join(PROJECT_ROOT, "logs", "similarity_eval.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
