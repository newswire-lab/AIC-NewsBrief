"""验证通用 HTML 列表页抽取：打印抽取结果 + 对应容器文本，便于人工核对。

用法：
    python scripts/check_html.py                 # 验证全部启用的 html 源
    python scripts/check_html.py mod_gov         # 只验证指定源
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, parsers, parsers_html              # noqa: E402
from src import fetch as fetchmod                          # noqa: E402
from src.fetch import build_session                        # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    wanted = set(sys.argv[1:])
    sources = [s for s in config.load_sources(only_enabled=True) if s.kind == "html"]
    if wanted:
        sources = [s for s in sources if s.id in wanted]

    out = []
    out.append("启用的 html 源: %d 个 -> %s" % (len(sources), ", ".join(s.id for s in sources)))
    out.append("")

    session = build_session()
    for src in sources:
        res = fetchmod.fetch(src.url, session=session, timeout=15, retries=1)
        out.append("=" * 78)
        out.append("源 %s (%s)  tier=%d" % (src.id, src.name, src.tier))
        out.append("URL %s" % src.url)
        out.append("ok=%s status=%s bytes=%d" % (res.ok, res.status, len(res.content)))
        if not res.ok:
            out.append("")
            continue

        page = parsers.decode_bytes(res.content, res.encoding)
        items = parsers_html.parse_list_page(
            page, src.url, max_items=src.max_items, debug=True
        )
        out.append("抽取到 %d 条" % len(items))
        pairs = getattr(parsers_html.parse_list_page, "_last_debug", [])
        for idx, (item, ctx) in enumerate(pairs[:12], 1):
            ts = item.published.strftime("%Y-%m-%d %H:%M") if item.published else "无时间"
            out.append("  %2d. [%s] %s" % (idx, ts, item.title[:50]))
            out.append("      url: %s" % item.link)
            out.append("      容器原文: %s" % ctx[:150])
        out.append("")

    log_path = os.path.join(PROJECT_ROOT, "logs", "html_check.txt")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
