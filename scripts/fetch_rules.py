"""抓取赛题页面上的附件（PDF），并尽力抽取文本。

用途：拿到《AIC·AI+开源竞赛规则及作品提交要求》与《技术报告参考大纲》的原文，
因为评分标准与报告大纲直接决定作品要做什么、怎么交。
"""

from __future__ import annotations

import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import parsers                                        # noqa: E402
from src import fetch as fetchmod                              # noqa: E402
from src.fetch import build_session                            # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(PROJECT_ROOT, "docs", "rules")
TRACK_URL = "https://www.aicomp.cn/tracks/tracks-5/4924.html"

PDF_LINK_RE = re.compile(r'href=["\']([^"\']+\.pdf)["\']', re.I)


def try_extract_pdf_text(path: str) -> str:
    """尽力用已安装的库抽取 PDF 文本，都没有则返回提示。"""
    for module_name in ("pypdf", "PyPDF2", "pdfminer.high_level"):
        try:
            if module_name == "pdfminer.high_level":
                from pdfminer.high_level import extract_text  # type: ignore
                return extract_text(path)
            module = __import__(module_name)
            if module_name == "pypdf":
                reader = module.PdfReader(path)
            else:
                reader = module.PdfReader(path)
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            return "\n".join(pages)
        except ImportError:
            continue
        except Exception as exc:
            return "[抽取失败: %s: %s]" % (type(exc).__name__, exc)
    return "[未安装 PDF 解析库，跳过文本抽取。可执行: pip install pypdf]"


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    session = build_session()

    res = fetchmod.fetch(TRACK_URL, session=session, timeout=20, retries=1)
    if not res.ok:
        print("抓取赛题页面失败: %s %s" % (res.status, res.error))
        return 1

    page = parsers.decode_bytes(res.content, res.encoding)
    links = []
    for raw in PDF_LINK_RE.findall(page):
        if raw.startswith("//"):
            raw = "https:" + raw
        elif raw.startswith("/"):
            raw = "https://www.aicomp.cn" + raw
        if raw not in links:
            links.append(raw)

    print("从赛题页面找到 %d 个 PDF 附件：" % len(links))
    for link in links:
        print("  - %s" % link)

    lines = ["抓取自: %s" % TRACK_URL, ""]
    for idx, link in enumerate(links, 1):
        r = fetchmod.fetch(link, session=session, timeout=30, retries=1)
        if not r.ok or not r.content:
            print("  [FAIL] %s  %s" % (link, r.error or r.status))
            lines.append("附件 %d 下载失败: %s (%s)" % (idx, link, r.error or r.status))
            continue
        filename = os.path.basename(link.split("?")[0])
        # 文件名是 URL 编码的中文，做一次解码便于阅读
        try:
            from urllib.parse import unquote
            filename = unquote(filename)
        except Exception:
            pass
        safe = re.sub(r'[\\/:*?"<>|]', "_", filename)
        path = os.path.join(OUT_DIR, "附件%d_%s" % (idx, safe))
        with open(path, "wb") as f:
            f.write(r.content)
        print("  [OK] %s  (%.1f KB) -> %s" % (link.split("/")[-1][:40],
                                             len(r.content) / 1024.0, os.path.basename(path)))
        text = try_extract_pdf_text(path)
        txt_path = os.path.splitext(path)[0] + ".txt"
        with io.open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        lines.append("附件 %d: %s" % (idx, os.path.basename(path)))
        lines.append("  文本长度: %d" % len(text))
        lines.append(text[:400])
        lines.append("")

    report = os.path.join(OUT_DIR, "抓取报告.txt")
    with io.open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("报告: %s" % report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
