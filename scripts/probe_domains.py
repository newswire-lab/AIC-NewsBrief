"""探测非军事领域的新闻源，用于把作品从"军事简报"重构为"多领域聚合"。

已知规律：新华网与人民网的旧版 RSS 路径有固定模式且可用，
因此优先批量试这些模式，再试其他媒体的常见 feed 路径。
"""

from __future__ import annotations

import concurrent.futures as cf
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import parsers                                        # noqa: E402
from src import fetch as fetchmod                              # noqa: E402
from src.fetch import build_session                            # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANDIDATES = [
    # ---- 新华网 RSS 族（已知模式，高概率可用）----
    ("xh_tech", "科技", "http://www.xinhuanet.com/tech/news_tech.xml"),
    ("xh_finance", "财经", "http://www.xinhuanet.com/finance/news_finance.xml"),
    ("xh_edu", "教育", "http://www.xinhuanet.com/edu/news_edu.xml"),
    ("xh_health", "健康", "http://www.xinhuanet.com/health/news_health.xml"),
    ("xh_legal", "法治", "http://www.xinhuanet.com/legal/news_legal.xml"),
    ("xh_energy", "能源", "http://www.xinhuanet.com/energy/news_energy.xml"),
    ("xh_overseas", "国际", "http://www.xinhuanet.com/overseas/news_overseas.xml"),
    ("xh_ent", "文娱", "http://www.xinhuanet.com/ent/news_ent.xml"),
    # ---- 人民网 RSS 族 ----
    ("rm_finance", "财经", "http://www.people.com.cn/rss/finance.xml"),
    ("rm_it", "科技", "http://www.people.com.cn/rss/it.xml"),
    ("rm_edu", "教育", "http://www.people.com.cn/rss/edu.xml"),
    ("rm_sci", "科技", "http://www.people.com.cn/rss/sci.xml"),
    ("rm_health", "健康", "http://www.people.com.cn/rss/health.xml"),
    ("rm_legal", "法治", "http://www.people.com.cn/rss/legal.xml"),
    ("rm_house", "房产", "http://www.people.com.cn/rss/house.xml"),
    ("rm_finance2", "财经", "http://www.people.com.cn/rss/ec_finance.xml"),
    # ---- 中新网 RSS 族 ----
    ("cn_finance", "财经", "https://www.chinanews.com.cn/rss/finance.xml"),
    ("cn_it", "科技", "https://www.chinanews.com.cn/rss/it.xml"),
    ("cn_cj", "财经", "https://www.chinanews.com.cn/rss/cj.xml"),
    ("cn_edu", "教育", "https://www.chinanews.com.cn/rss/edu.xml"),
    # ---- 科技媒体 ----
    ("jiqizhixin", "科技", "https://www.jiqizhixin.com/rss"),
    ("qbitai", "科技", "https://www.qbitai.com/feed"),
    ("36kr", "科技财经", "https://36kr.com/feed"),
    ("ithome", "科技", "https://www.ithome.com/rss/"),
    ("sspai", "科技生活", "https://sspai.com/feed"),
    ("cnbeta", "科技", "https://www.cnbeta.com.tw/backend.php"),
    ("leiphone", "科技", "https://www.leiphone.com/feed"),
    # ---- 财经媒体 ----
    ("yicai", "财经", "https://www.yicai.com/rss/"),
    ("eeo", "财经", "http://www.eeo.com.cn/rss.xml"),
    ("cls", "财经", "https://www.cls.cn/rss"),
    ("cs", "财经", "https://www.cs.com.cn/rss/"),
    # ---- 中央媒体其他频道 ----
    ("gmw_tech", "科技", "https://tech.gmw.cn/rss.xml"),
    ("china_tech", "科技", "http://tech.china.com.cn/rss.xml"),
]


def probe(item):
    tag, domain, url = item
    try:
        res = fetchmod.fetch(url, session=build_session(), timeout=8, retries=0)
    except Exception as exc:
        return "EXC  %-14s %-8s %s" % (tag, domain, str(exc)[:40])
    if not res.ok:
        return "FAIL %-14s %-8s %-5s %s" % (tag, domain, str(res.status or "-"),
                                            (res.error or "")[:36])
    items, feed_title = parsers.parse_feed(res.content, res.encoding)
    if items:
        return "OK   %-14s %-8s n=%-4d %s" % (tag, domain, len(items), feed_title[:30])
    return "HTML %-14s %-8s status=%s" % (tag, domain, res.status)


def main() -> int:
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        lines = sorted(ex.map(probe, CANDIDATES))
    body = "\n".join(lines)
    print(body)
    with io.open(os.path.join(PROJECT_ROOT, "logs", "domain_feeds.txt"), "w",
                 encoding="utf-8") as f:
        f.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
