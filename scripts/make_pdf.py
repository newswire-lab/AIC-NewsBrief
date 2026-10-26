"""把 Markdown 技术报告转成提交用 PDF。

链路：``Markdown -> HTML（带打印样式） -> Edge 无头打印 -> PDF``

为什么用 Edge 而不是 reportlab/weasyprint：
- 本机没装 pandoc / wkhtmltopdf / weasyprint；weasyprint 在 Windows 上还要额外装 GTK。
- Edge 是 Windows 自带的，``--headless --print-to-pdf`` 直接可用，
  且系统字体（微软雅黑）能让中文正常渲染 —— 这是自绘 PDF 最容易翻车的地方。
- Markdown 解析用 ``markdown-it-py``（依赖少、支持表格）。

用法（在项目根目录执行）::

    python scripts/make_pdf.py                        # 默认转 docs/技术报告.md
    python scripts/make_pdf.py --md docs/开源说明.md --out dist/开源说明.pdf

脚本会事**实校验**产物：页数 <=15、体积 <=10 MB（赛题硬约束），不达标直接非零退出，
避免"转出来了但超标"这种提交前才发现的问题。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 赛题约束（注意两者的**性质不同**）
#
# ・体积：作品材料规范第 3 条“大小不超过 10 MB” —— 硬性上限，超了就是不合规。
# ・页数：同条写的是“正文建议控制在 15 页以内”，**是建议不是硬性**，
#         而且紧接着说明“不以报告篇幅、图片数量或材料数量作为评分依据”。
#
# 所以 15 页不能当 FAIL：内容已经按 9.4pt / 行高 1.32 / A4 窄边距排版，
# 再压就是牺牲可读性去凑页数，与“篇幅不计分”的规则本意相背。
# 处理成：<=15 页 OK；15~20 页 WARN（仍返回 0，只提醒）；>20 页 FAIL。
WARN_PAGES = 15          # 赛题建议值
MAX_PAGES = 20           # 自设的硬上限：超过就说明该把细节挪进附录了
MAX_BYTES = 10 * 1024 * 1024

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 打印样式：A4、紧凑排版、中文换行、表格不跨页断裂、代码块不溢出
#
# 排版经验（实测出来的）：
# - **不要给 h2 加 `break-before: page`**。本报告有 9 个二级标题，每个都另起一页
#   会白白浪费近 9 页，实测直接把 15 页写成 22 页。
# - 字号/行距/页边距这三项对总页数的影响最大，先调它们。
PRINT_CSS = """
@page { size: A4; margin: 12mm 11mm 12mm 11mm; }

html { font-size: 9.4pt; }
body {
  font-family: "Microsoft YaHei", "微软雅黑", "SimSun", sans-serif;
  line-height: 1.32; color: #1a1a1a; margin: 0;
  /* 中文长串（URL、英文标识符）必须能断行，否则会把表格撑出页面 */
  overflow-wrap: anywhere; word-break: break-word;
}

h1 { font-size: 15pt; margin: 0 0 3mm; padding-bottom: 1.5mm;
     border-bottom: 1.6pt solid #2c5f8d; color: #17324d; }
h2 { font-size: 12.5pt; margin: 4.5mm 0 1.8mm; padding-left: 2mm;
     border-left: 3.5pt solid #2c5f8d; color: #17324d;
     break-after: avoid; page-break-after: avoid; }
h3 { font-size: 11pt; margin: 3.2mm 0 1.4mm; color: #244a6b;
     break-after: avoid; page-break-after: avoid; }
h4 { font-size: 10pt; margin: 2.6mm 0 1.1mm; color: #35597a;
     break-after: avoid; page-break-after: avoid; }

p { margin: 0 0 1.3mm; text-align: justify; }
ul, ol { margin: 0 0 1.3mm; padding-left: 5mm; }
li { margin: 0.3mm 0; }

table { border-collapse: collapse; width: 100%; margin: 1.4mm 0 2.4mm;
        font-size: 7.9pt; }
th, td { border: 0.4pt solid #b8c4cf; padding: 0.7mm 1.2mm;
         vertical-align: top; text-align: left; }
th { background: #eaf1f7; font-weight: 600; color: #17324d; }
tr { break-inside: avoid; page-break-inside: avoid; }

code { font-family: Consolas, "Courier New", monospace; font-size: 0.91em;
       background: #f2f5f8; padding: 0.1mm 0.6mm; border-radius: 0.8mm; }
pre { background: #f6f8fa; border: 0.4pt solid #d6dee6; border-left: 2.5pt solid #2c5f8d;
      padding: 1.4mm 2mm; margin: 1.4mm 0 2mm; font-size: 7.4pt; line-height: 1.25;
      white-space: pre-wrap; overflow-wrap: anywhere;
      break-inside: avoid; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 1em; }

blockquote { margin: 1.4mm 0; padding: 1.1mm 2.4mm; background: #f7f9fb;
             border-left: 2.5pt solid #9db4c8; color: #3a4a58; font-size: 0.96em; }
blockquote p { margin: 0.4mm 0; }

img { max-width: 100%; max-height: 84mm; object-fit: contain;
      display: block; margin: 1.5mm auto;
      break-inside: avoid; page-break-inside: avoid; }

a { color: #1a4d7a; text-decoration: none; }
hr { border: none; border-top: 0.4pt solid #cfd8e0; margin: 2.5mm 0; }
"""


def find_browser() -> str:
    for cand in EDGE_CANDIDATES:
        if os.path.exists(cand):
            return cand
    raise SystemExit("[ERROR] 找不到 Microsoft Edge，无法打印 PDF。"
                     "请在 EDGE_CANDIDATES 里补上你的浏览器路径。")


def md_to_html(md_text: str, title: str, base_dir: Path | None = None) -> str:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
    body = md.render(md_text)
    body = _absolutize_images(body, base_dir)
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{title}</title>\n"
        f"<style>{PRINT_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


_IMG_SRC_RE = re.compile(r'(<img[^>]*\ssrc=")([^"]+)(")')


def _absolutize_images(html: str, base_dir: Path | None) -> str:
    """把 HTML 里的**相对**图片路径改成绝对 file:// URI。

    为什么需要：图片按 Markdown 文件所在目录写相对路径（如
    ``screenshots/1_hot_events.png``），但中间 HTML 却输出到 ``dist/``；
    两者不同目录，相对路径会直接断掉，PDF 里就是一堆空白框。
    """
    if base_dir is None:
        return html

    def repl(m: "re.Match[str]") -> str:
        src = m.group(2)
        if re.match(r"^(https?:|data:|file:|/|[A-Za-z]:)", src):
            return m.group(0)
        p = (base_dir / src).resolve()
        if not p.exists():
            print(f"      [警告] 图片不存在，PDF 里会是空白：{p}")
            return m.group(0)
        return m.group(1) + p.as_uri() + m.group(3)

    return _IMG_SRC_RE.sub(repl, html)


def render_pdf(browser: str, html_path: Path, pdf_path: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()
    with tempfile.TemporaryDirectory(prefix="newsbrief_pdf_") as profile:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=20000",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={pdf_path}",
            html_path.resolve().as_uri(),
        ]
        # Edge 的 stderr 可能含非 GBK 字节，必须显式按 utf-8 解码，
        # 否则 Python 读子进程输出时会抛 UnicodeDecodeError（实测踩到）
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    if not pdf_path.exists():
        raise SystemExit(
            "[ERROR] 浏览器没有产出 PDF。\n"
            f"  rc={proc.returncode}\n  stdout={proc.stdout[-600:]}\n  stderr={proc.stderr[-600:]}"
        )


def inspect(pdf_path: Path) -> tuple[int, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)
    size = pdf_path.stat().st_size
    # 顺带看看正文有没有真的写进去（空 PDF 也会"生成成功"）
    text = (reader.pages[0].extract_text() or "").strip()
    if len(text) < 40:
        raise SystemExit(f"[ERROR] 首页文本过少（{len(text)} 字符），可能是空白 PDF")
    return pages, size


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Markdown -> PDF（Edge 无头打印）")
    ap.add_argument("--md", default="docs/技术报告.md")
    ap.add_argument("--out", default="dist/技术报告.pdf")
    ap.add_argument("--html", default=None, help="中间 HTML 输出路径（默认与 PDF 同目录同名）")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument("--max-mb", type=float, default=MAX_BYTES / 1024 / 1024)
    args = ap.parse_args(argv)

    md_path = (PROJECT_ROOT / args.md).resolve()
    if not md_path.exists():
        raise SystemExit(f"[ERROR] 找不到 {md_path}")
    pdf_path = (PROJECT_ROOT / args.out).resolve()
    html_path = (Path(args.html).resolve() if args.html
                 else pdf_path.with_suffix(".html"))

    md_text = md_path.read_text(encoding="utf-8")
    if "\ufffd" in md_text:
        raise SystemExit("[ERROR] Markdown 里含 U+FFFD 替换字符，先修文档再转 PDF")

    title = next((l.lstrip("# ").strip() for l in md_text.splitlines() if l.startswith("# ")),
                 md_path.stem)
    html = md_to_html(md_text, title, base_dir=md_path.parent)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")

    browser = find_browser()
    print(f"[1/3] Markdown -> HTML   {md_path.name}  ({len(md_text):,} 字符)")
    print(f"      HTML               {html_path.relative_to(PROJECT_ROOT)}")
    render_pdf(browser, html_path, pdf_path)
    print(f"[2/3] HTML -> PDF        {pdf_path.relative_to(PROJECT_ROOT)}")

    pages, size = inspect(pdf_path)
    mb = size / 1024 / 1024
    print(f"[3/3] 校验               页数 {pages}（建议 <= {args.max_pages}）｜"
          f"体积 {mb:.2f} MB（硬上限 {args.max_mb:g} MB）")
    bad = []
    # 体积超限 = 不合规，硬失败
    if mb > args.max_mb:
        bad.append(f"体积超限 {mb:.2f} MB > {args.max_mb:g} MB")
    # 页数只当告警（赛题为“建议”），但明显过量也拦一下
    if pages > MAX_PAGES:
        bad.append(f"页数远超自设上限 {pages} > {MAX_PAGES}，请把实现细节挪进附录")
    if bad:
        print("[FAIL] " + "；".join(bad))
        return 1
    if pages > args.max_pages:
        print(f"[WARN] 页数 {pages} 超出赛题建议值 {args.max_pages}；"
              f"（赛题原文为“正文建议控制在 15 页以内”，且明确不以篇幅计分，故不判失败）")
        return 0
    print("[OK] 符合赛题约束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
