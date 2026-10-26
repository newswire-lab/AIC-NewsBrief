"""dump 文章链接周边的原始 HTML，用于确认列表页的真实结构。"""

from __future__ import annotations

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, parsers                                        # noqa: E402
from src import fetch as fetchmod                                      # noqa: E402
from src import parsers_html                                           # noqa: E402
from src.fetch import build_session                                    # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "mod_gov"
    span = int(sys.argv[2]) if len(sys.argv) > 2 else 420
    if arg.startswith("http"):
        target_id, target_url = arg, arg
    else:
        src = next(s for s in config.load_sources(only_enabled=False) if s.id == arg)
        target_id, target_url = src.id, src.url

    session = build_session()
    res = fetchmod.fetch(target_url, session=session, timeout=15, retries=1)
    page = parsers.decode_bytes(res.content, res.encoding)

    out = []
    out.append("目标 %s  URL %s  ok=%s bytes=%d"
               % (target_id, target_url, res.ok, len(page)))

    # 找出形如 content_12345678.html 的正文链接（比 _is_article_href 更严格，
    # 用于避开 "wzdt_226173/index.html" 这类栏目页导航）
    strict = re.compile(r"\d{6,}\.html?(?:[?#]|$)", re.I)
    hits = [m for m in re.finditer(r"href=[\"']([^\"']+)[\"']", page, re.I)
            if strict.search(m.group(1))]
    out.append("正文链接数（严格匹配）: %d" % len(hits))
    out.append("")

    for i, m in enumerate(hits[:3], 1):
        start = max(0, m.start() - span)
        end = min(len(page), m.end() + span)
        out.append("=" * 78)
        out.append("链接 %d: %s" % (i, m.group(1)))
        out.append("-" * 78)
        out.append(page[start:end])
        out.append("")

    # 所有"短文本节点里的日期"按文档顺序列出，看日期相对链接的位置
    out.append("=" * 78)
    out.append("文档中所有含日期的文本位置（按出现顺序）")
    out.append("-" * 78)
    date_nodes = [m for m in re.finditer(r">([^<>]{0,40}?)</", page)
                  if m.group(1) and parsers_html.DATE_RE.search(m.group(1))]
    for m in date_nodes[:20]:
        out.append("pos=%7d  text=%r" % (m.start(), m.group(1).strip()[:40]))
    out.append("共 %d 个（最多显示 20）" % len(date_nodes))

    # 链接位置列表，便于对照
    out.append("")
    out.append("-" * 78)
    out.append("前 10 个文章链接的位置")
    for m in hits[:10]:
        out.append("pos=%7d  %s" % (m.start(), m.group(1)[:70]))

    log_path = os.path.join(PROJECT_ROOT, "logs", "html_context.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("%-56s li=%-4d dateNodes=%-3d strictLinks=%-4d bytes=%d"
          % (target_url[:56], len(re.findall(r"<li\b", page, re.I)),
             len(date_nodes), len(hits), len(page)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
