"""诊断 HTML 列表页抽取：打印提取器的内部状态，定位为什么抽不到条目。"""

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
    source_id = sys.argv[1] if len(sys.argv) > 1 else "mod_gov"
    src = next(s for s in config.load_sources(only_enabled=False) if s.id == source_id)

    session = build_session()
    res = fetchmod.fetch(src.url, session=session, timeout=15, retries=1)
    page = parsers.decode_bytes(res.content, res.encoding)

    out = []
    out.append("源 %s  URL %s  ok=%s bytes=%d" % (src.id, src.url, res.ok, len(res.content)))
    out.append("li 标签数=%d  dd=%d  tr=%d  a 标签数=%d"
               % (len(re.findall(r"<li\b", page, re.I)),
                  len(re.findall(r"<dd\b", page, re.I)),
                  len(re.findall(r"<tr\b", page, re.I)),
                  len(re.findall(r"<a\b", page, re.I))))
    out.append("日期样式出现次数: %d" % len(parsers_html.DATE_RE.findall(page)))
    out.append("SCRIPT 剥离后长度 %d -> %d"
               % (len(page), len(parsers_html.SCRIPT_STYLE_RE.sub(" ", page))))
    out.append("")

    stripped = parsers_html.SCRIPT_STYLE_RE.sub(" ", page)
    for label, text in (("原始", page), ("剥离script", stripped)):
        ex = parsers_html._ListExtractor(src.url)
        ex.debug = True
        try:
            ex.feed(text)
            ex.close()
            err = "ok"
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
        out.append("--- %s --- feed=%s  emits=%d  debug_pairs=%d"
                   % (label, err, len(ex.items), len(ex.debug_pairs)))
        for item, ctx in ex.debug_pairs[:8]:
            out.append("   [%s] %s" % (item.published, item.title[:44]))
            out.append("      ctx: %s" % ctx[:170])
        out.append("")

    # 不要求日期时能抽到多少
    items_no_date = parsers_html.parse_list_page(
        page, src.url, max_items=30, require_date=False
    )
    out.append("require_date=False -> %d 条" % len(items_no_date))
    for item in items_no_date[:8]:
        out.append("   [%s] %s" % (item.published, item.title[:50]))
        out.append("      %s" % item.link)

    log_path = os.path.join(PROJECT_ROOT, "logs", "html_debug.txt")
    with io.open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
