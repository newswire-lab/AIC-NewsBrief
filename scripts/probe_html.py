"""一次性探查脚本：dump 目标列表页结构，用于设计通用 HTML 抽取规则。

用法：
    python scripts/probe_html.py
    python scripts/probe_html.py https://example.com/list/
"""

from __future__ import annotations

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import parsers                                   # noqa: E402
from src import fetch as fetchmod                         # noqa: E402
from src.fetch import build_session                       # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URLS = [
    "http://www.mod.gov.cn/gongbu/",
    "https://www.mod.gov.cn/gongbu/",
]

DATE_RE = re.compile(r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?")
ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def main() -> int:
    urls = sys.argv[1:] or DEFAULT_URLS
    session = build_session()
    out: list[str] = []

    for url in urls:
        res = fetchmod.fetch(url, session=session, timeout=15, retries=1)
        out.append("=" * 78)
        out.append("URL   : %s" % url)
        out.append("ok=%s status=%s enc=%s bytes=%d err=%s"
                   % (res.ok, res.status, res.encoding, len(res.content), res.error[:100]))
        if not res.ok:
            out.append("")
            continue

        text = parsers.decode_bytes(res.content, res.encoding)
        out.append("decoded chars: %d" % len(text))

        anchors = ANCHOR_RE.findall(text)
        dates = DATE_RE.findall(text)
        out.append("anchor 总数: %d   date 出现次数: %d" % (len(anchors), len(dates)))

        out.append("--- 锚点样例（前 15）---")
        for href, inner in anchors[:15]:
            label = TAG_RE.sub("", inner).strip()
            out.append("  %-72s | %s" % (href[:72], label[:36]))

        out.append("--- 锚点 + 日期（疑似列表项，最多 25 行）---")
        shown = 0
        for href, inner in anchors:
            label = TAG_RE.sub("", inner).strip()
            if len(label) < 6:
                continue
            # 在锚点后 240 字符内找日期
            pos = text.find(href)
            window = text[pos: pos + 400] if pos >= 0 else ""
            m = DATE_RE.search(window)
            if not m:
                continue
            out.append("  [%s] %-60s | %s" % (m.group(0), label[:60], href[:60]))
            shown += 1
            if shown >= 25:
                break
        out.append("疑似列表项条数（前 25 内）: %d" % shown)

        out.append("--- 原始片段：首个含列表结构的 800 字符 ---")
        m = re.search(r"<ul[\s\S]{0,800}", text, re.I)
        if m:
            out.append(m.group(0))
        out.append("")

    log_path = os.path.join(PROJECT_ROOT, "logs", "html_probe.txt")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
