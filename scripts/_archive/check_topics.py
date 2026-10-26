# -*- coding: utf-8 -*-
"""回归：修完图注噪声与词表后，重点看几个案例是否判对了。"""
import os, subprocess, sys
from pathlib import Path

# 用 __file__ 反推项目根，**不写本机绝对路径**：
# 用户名出现在文件里会被 scripts/check_anonymize.py 判为匿名化违规，
# 提交材料（含源码包）中不能出现形如 C:\Users\<姓名> 的路径。
ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, ROOT)
os.chdir(ROOT)
rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src")], capture_output=True, text=True)
print("compileall rc =", rc.returncode, rc.stderr[-300:])

from src import store, topics

conn = store.connect()
store.init_db(conn)
print("strip_noise('…（无人机照片）中新社记者 摄') ->",
      repr(topics.strip_noise("河南郑州（无人机照片）中新社记者 范晓恒 摄")))
print()
res = topics.classify_all(conn, verbose=True)

rep = []
rep.append("=" * 78)
rep.append("重点案例回归")
rep.append("=" * 78)
CASES = [
    "河南郑州：秋日“粉黛花海”吸引游人",
    "三峡水库蓄水进入最后冲刺阶段",
    "中国商品期货成交量连续多年居世界前列",
    "乌军星链操作或受 SpaceX 新规影响专家称",
    "俄外交部：日方在俄以记者身份从事情报活动",
    "央行：今年9月末 社会融资规模存量同比增长7.1%",
    "美陆军将600名士官转隶太空作战部门",
    "李强出席第140届中国进出口商品交易会开幕式并发表主旨演讲",
]
for t in CASES:
    row = conn.execute("SELECT topic, topic2 FROM articles WHERE title = ? "
                       "OR title_zh = ? LIMIT 1", (t, t)).fetchone()
    if row:
        rep.append("  %-38s → **%s**%s" % (t[:38], row["topic"],
                                           "（次：" + row["topic2"] + "）" if row["topic2"] else ""))
    else:
        rep.append("  %-38s → （库里没这条）" % t[:38])

rep.append("")
rep.append("=" * 78)
rep.append("各分区抽样（修复后）")
rep.append("=" * 78)
for name, n in topics.stats(conn):
    rep.append("【%s】%d 篇" % (name, n))
    for r in conn.execute(
            "SELECT title, title_zh, source_org FROM articles WHERE topic = ? "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT 4", (name,)):
        rep.append("   %-44s ｜ %s" % ((r["title_zh"] or r["title"])[:44],
                                       r["source_org"][:12]))
    rep.append("")

open(os.path.join(ROOT, "logs", "_check_topics_report.txt"), "w",
     encoding="utf-8").write("\n".join(rep))
print("report -> logs/_check_topics_report.txt lines=%d" % len(rep))
