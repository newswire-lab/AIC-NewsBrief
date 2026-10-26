"""批量探测候选新闻源，输出可直接用于补充 sources.yaml 的结果。

关注点：优先找**独立机构**（非 Sightline 集团、非同一官媒集团）的源，
因为实测表明跨机构印证才是可信度模型的有效信号。
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
    # 境外独立军事/防务媒体
    ("eurasiantimes", "https://www.eurasiantimes.com/feed/"),
    ("indiandefencereview", "https://indiandefencereview.com/feed/"),
    ("defenceblog", "https://defence-blog.com/feed/"),
    ("thedefensepost", "https://thedefensepost.com/feed/"),
    ("defensemirror", "https://www.defensemirror.com/rss"),
    ("militaryleak", "https://militaryleak.com/feed/"),
    ("globaldefensecorp", "https://globaldefensecorp.com/feed/"),
    ("armyrecognition", "https://armyrecognition.com/rss.xml"),
    ("defencetalk", "https://www.defencetalk.com/feed/"),
    ("airforce-technology", "https://www.airforce-technology.com/feed/"),
    ("naval-technology", "https://www.naval-technology.com/feed/"),
    ("army-technology", "https://www.army-technology.com/feed/"),
    ("shephardmedia", "https://www.shephardmedia.com/rss/"),
    ("military.com", "https://www.military.com/rss/daily-news.xml"),
    ("sightline-dn-forum", "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml"),
    # 通讯社 / 国际媒体（多数此前超时，此处复查）
    ("scmp", "https://www.scmp.com/rss/4/feed"),
    ("nhk", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
    ("cbc-world", "https://www.cbc.ca/webfeed/rss/rss-world"),
    ("abc-au-world", "https://www.abc.net.au/news/feed/51120/rss.xml"),
    ("voa", "https://www.voanews.com/api/zb_aq$qiyoyb"),
    ("dw-cn", "https://rss.dw.com/rdf/rss-chi-all"),
    ("rfi-cn", "https://www.rfi.fr/cn/rss"),
    ("zaobao", "https://www.zaobao.com/realtime/world/rss.xml"),
    # 中国境内其他媒体
    ("huanqiu-mil", "https://mil.huanqiu.com/rss.xml"),
    ("sina-mil", "https://mil.news.sina.com.cn/rss/mil.xml"),
    ("cankaoxiaoxi", "http://www.cankaoxiaoxi.com/rss.xml"),
    ("china-mil-news", "http://www.81.cn/gfbmap/content/2026-09/23/node_2.htm"),
    ("gmw", "https://www.gmw.cn/rss/politics.xml"),
    ("china-com-mil", "http://military.china.com/rss.xml"),
]


def probe(item):
    tag, url = item
    session = build_session()
    try:
        res = fetchmod.fetch(url, session=session, timeout=10, retries=0)
    except Exception as exc:
        return "EXC  %-20s %s" % (tag, str(exc)[:60])
    if not res.ok:
        return "FAIL %-20s %-22s %s" % (tag, str(res.status or "-"), res.error[:44])
    items, feed_title = parsers.parse_feed(res.content, res.encoding)
    if items:
        return "OK   %-20s n=%-4d %s" % (tag, len(items), feed_title[:34])
    head = parsers.decode_bytes(res.content[:90], res.encoding).replace("\n", " ")
    return "HTML %-20s status=%s head=%r" % (tag, res.status, head[:52])


def main() -> int:
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        lines = list(ex.map(probe, CANDIDATES))
    lines.sort()
    body = "\n".join(lines)
    print(body)
    log_path = os.path.join(PROJECT_ROOT, "logs", "feed_candidates.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
