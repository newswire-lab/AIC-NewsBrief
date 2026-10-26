"""作品简介字数校验：确保不突破大赛规定的「简介 ≤300 字」硬性上限。

**为什么单独写一个脚本**

赛题规则要求作品简介不超过 300 字。这个上限看似简单，但「300 字」有三种常见口径，
而三者差异很大（本项目旧版简介分别为 304 / 340 / 421，**三种口径全部超限**）：

1. **中文字符**：只数 CJK 汉字 —— 最宽松
2. **中文字符 + 中文标点**：接近 Word 的「中文字符和朝鲜语单词」统计
3. **不计空格的总字符数**：最严格，也最接近在线表单的字符数限制

本项目按**最严格口径**（不计空格总字符）控制，这样无论评委用哪种方式统计都合规。

统计时会先去掉 Markdown 加粗标记 ``**`` —— 它是排版语法，不是正文。

用法::

    python scripts/check_brief_length.py              # 校验 docs/作品简介与名称.md
    python scripts/check_brief_length.py --verbose    # 额外打印逐行统计

退出码：0 = 合规；1 = 超限（可直接用于提交流程的门禁检查）。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys

try:                                     # 控制台可能是 GBK，避免打印异常中断
    sys.stdout.reconfigure(errors="replace")       # type: ignore[attr-defined]
except Exception:                        # noqa: BLE001
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DOC = os.path.join(PROJECT_ROOT, "docs", "作品简介与名称.md")

LIMIT = 300

# 正文起止标记：位于「## 作品简介」标题之后、下一个 "---" 分隔线之前
START_RE = re.compile(r"^##\s*作品简介")
END_RE = re.compile(r"^---\s*$")

CN_PUNCT = set("，。、；：？！（）《》〈〉「」『』【】—…·～")
CN_QUOTES = set("\u201c\u201d\u2018\u2019")        # “ ” ‘ ’


def is_cjk(ch: str) -> bool:
    """是否为中日韩统一表意文字（即通常说的「汉字」）。"""
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def extract_brief(text: str) -> str:
    """从 Markdown 文档中抽出简介正文。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if START_RE.match(line):
            start = i + 1
            break
    if start is None:
        raise SystemExit("未找到「## 作品简介」标题，无法定位正文")

    body = []
    for line in lines[start:]:
        if END_RE.match(line):
            break
        body.append(line)
    return "\n".join(body)


def count(brief: str) -> dict:
    """分别按三种口径统计字数。"""
    plain = brief.replace("**", "").replace("*", "")   # 去掉 Markdown 强调标记
    cjk = punct = ascii_ch = other = nospace = 0
    per_line = []
    for line in plain.splitlines():
        c = {"cjk": 0, "punct": 0, "ascii": 0, "other": 0, "nospace": 0}
        for ch in line:
            if ch.isspace():
                continue
            c["nospace"] += 1
            if is_cjk(ch):
                c["cjk"] += 1
            elif ch in CN_PUNCT or ch in CN_QUOTES:
                c["punct"] += 1
            elif ord(ch) < 128:
                c["ascii"] += 1
            else:
                c["other"] += 1
        cjk += c["cjk"]
        punct += c["punct"]
        ascii_ch += c["ascii"]
        other += c["other"]
        nospace += c["nospace"]
        per_line.append((line, c))

    return {
        "cjk": cjk,
        "punct": punct,
        "ascii": ascii_ch,
        "other": other,
        "nospace": nospace,
        "cjk_plus_punct": cjk + punct,
        "per_line": per_line,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="作品简介字数校验（上限 300 字）")
    parser.add_argument("--doc", default=DEFAULT_DOC, help="简介文档路径")
    parser.add_argument("--limit", type=int, default=LIMIT, help="字数上限")
    parser.add_argument("--verbose", action="store_true", help="打印逐行统计")
    args = parser.parse_args()

    if not os.path.exists(args.doc):
        print(f"[错误] 找不到文件: {args.doc}")
        return 1

    with io.open(args.doc, encoding="utf-8") as f:
        text = f.read()

    brief = extract_brief(text)
    st = count(brief)

    if args.verbose:
        print("逐行统计（行内容截断显示）")
        print("-" * 68)
        for line, c in st["per_line"]:
            if c["nospace"] == 0:
                continue
            print("  %-34s 汉字%3d 标点%3d ASCII%3d 合计%3d"
                  % (line[:34], c["cjk"], c["punct"], c["ascii"], c["nospace"]))
        print("-" * 68)

    rows = [
        ("中文汉字", st["cjk"]),
        ("中文字符 + 中文标点", st["cjk_plus_punct"]),
        ("不计空格总字符数（最严格）", st["nospace"]),
    ]

    print("作品简介字数校验")
    print("=" * 46)
    over = False
    for name, value in rows:
        mark = "OK " if value <= args.limit else "超限"
        if value > args.limit:
            over = True
        print("  %-26s %5d  / %d   %s" % (name, value, args.limit, mark))
    print("=" * 46)
    print("  其他字符（符号等）%13d" % st["other"])
    print("  ASCII（模型名/数字等）%9d" % st["ascii"])

    if over:
        print("\n[失败] 至少一种口径超过 %d 字，请压缩简介正文。" % args.limit)
        return 1

    margin = args.limit - st["nospace"]
    print(f"\n[通过] 三种口径均在 {args.limit} 字以内，最严口径余量 {margin} 字符。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
