"""分析交叉印证的构成：跨机构 vs 同机构内部互转。

背景：把印证度从"按站点计数"改为"按机构计数"后，多机构印证事件降为 0。
需要查清这是数据事实，还是标题 SimHash 阈值过严导致的漏检。

输出：
- 各阈值下的同机构 / 跨机构近似重复对数
- 跨机构样例（人工判断是否同一事件）
- 各机构内部互转规模（验证"同集团多站点"确实存在）
"""

from __future__ import annotations

import collections
import io
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import store, dedup                                     # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLDS = (3, 5, 7, 10, 14)


def main() -> int:
    conn = store.connect()
    rows = conn.execute(
        "SELECT id, source_id, source_name, source_org, region, title, title_hash "
        "FROM articles WHERE title_hash != ''"
    ).fetchall()
    docs = []
    for r in rows:
        try:
            h = int(r["title_hash"], 16)
        except (TypeError, ValueError):
            continue
        if h:
            docs.append((r["source_id"], r["source_org"], r["source_name"],
                         r["region"], r["title"], h))

    out = []
    out.append("参与分析的文章: %d 篇" % len(docs))
    orgs = collections.Counter(d[1] for d in docs)
    out.append("机构数: %d" % len(orgs))
    out.append("按机构分布（前 15）:")
    for org, n in orgs.most_common(15):
        out.append("  %-24s %d" % (org, n))
    out.append("")

    # 预计算所有对的海明距离（一次性）
    pairs = []
    for a, b in itertools.combinations(docs, 2):
        d = dedup.hamming64(a[5], b[5])
        if d <= max(THRESHOLDS):
            pairs.append((d, a, b))

    out.append("=" * 78)
    out.append("同机构 / 跨机构 近似重复对统计")
    out.append("-" * 78)
    out.append("%-10s %10s %10s %10s" % ("阈值", "合计", "同机构", "跨机构"))
    for thr in THRESHOLDS:
        sel = [p for p in pairs if p[0] <= thr]
        same = [p for p in sel if p[1][1] == p[2][1]]
        cross = [p for p in sel if p[1][1] != p[2][1]]
        out.append("%-10d %10d %10d %10d" % (thr, len(sel), len(same), len(cross)))
    out.append("")

    cross3 = [p for p in pairs if p[0] <= 3 and p[1][1] != p[2][1]]
    out.append("=" * 78)
    out.append("跨机构近似重复样例（hamming<=3，人工判断是否同一事件）")
    out.append("-" * 78)
    if not cross3:
        out.append("  （无）")
    for d, a, b in cross3[:20]:
        out.append("[d=%d] %s <%s>" % (d, a[4][:36], a[1]))
        out.append("        %s <%s>" % (b[4][:36], b[1]))
    out.append("")

    cross10 = [p for p in pairs if p[0] <= 10 and p[1][1] != p[2][1]]
    out.append("=" * 78)
    out.append("放宽到 hamming<=10 的跨机构样例（看是否有被漏掉的同事件）")
    out.append("-" * 78)
    if not cross10:
        out.append("  （无）")
    for d, a, b in cross10[:25]:
        out.append("[d=%2d] %s <%s>" % (d, a[4][:34], a[1]))
        out.append("         %s <%s>" % (b[4][:34], b[1]))
    out.append("")

    out.append("=" * 78)
    out.append("同机构内部互转样例（hamming<=3）")
    out.append("-" * 78)
    same3 = [p for p in pairs if p[0] <= 3 and p[1][1] == p[2][1]]
    for d, a, b in same3[:15]:
        out.append("[d=%d] %s <%s|%s>" % (d, a[4][:30], a[2], a[0]))
        out.append("        %s <%s|%s>" % (b[4][:30], b[2], b[0]))
    out.append("")

    # 同一事件的跨机构是否存在但标题差异大：检查新华社与军网同主题
    cn_pairs = [p for p in pairs if p[0] <= 14
                and p[1][3] == "cn" and p[2][3] == "cn" and p[1][1] != p[2][1]]
    out.append("=" * 78)
    out.append("中文源之间的跨机构近似重复（hamming<=14）")
    out.append("-" * 78)
    if not cn_pairs:
        out.append("  （无）")
    for d, a, b in cn_pairs[:20]:
        out.append("[d=%2d] %s <%s>" % (d, a[4][:36], a[1]))
        out.append("         %s <%s>" % (b[4][:36], b[1]))

    log_path = os.path.join(PROJECT_ROOT, "logs", "org_analysis.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
