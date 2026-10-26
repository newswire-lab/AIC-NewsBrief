#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把文档里的仓库链接占位符替换为真实地址。

用法：
    python scripts/fill_links.py https://github.com/<用户名>/AIC-NewsBrief
    python scripts/fill_links.py https://gitee.com/<用户名>/AIC-NewsBrief --dry-run

说明：README 顶部有一个 `{{REPO_URL}}` 占位符。建好公开仓库后跑一次即可，
之后重新打包发布包（logs/_tmp_make_repo.py）就会带上真实链接。
"""
import argparse
import glob
import io
import os
import re
import sys

TOKENS = ["{{REPO_URL}}", "<repo-url>"]


def main() -> int:
    ap = argparse.ArgumentParser(description="填充仓库链接占位符")
    ap.add_argument("url", help="仓库地址，如 https://github.com/you/AIC-NewsBrief")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    url = args.url.strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9._\-]+/", url):
        print("[!] 地址不合法: %s" % url)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)

    SKIP = {"docs/进度交接.md"}  # 内部文档不入库，其中的占位符是说明文字
    files = [p for p in (["README.md"] + sorted(glob.glob("docs/*.md"))
                         + sorted(glob.glob("scripts/**/*.md", recursive=True)))
             if p.replace("\\", "/") not in SKIP]
    total = 0
    for p in files:
        if not os.path.exists(p):
            continue
        t = io.open(p, encoding="utf-8").read()
        n = sum(t.count(tok) for tok in TOKENS)
        if not n:
            continue
        total += n
        print("%-34s %d 处" % (p, n))
        if args.dry_run:
            continue
        for tok in TOKENS:
            t = t.replace(tok, url)
        assert "\ufffd" not in t, p
        io.open(p, "w", encoding="utf-8", newline="").write(t)

    if not total:
        print("[i] 没有找到占位符（可能已经填过了）")
    elif args.dry_run:
        print("[i] dry-run：共 %d 处待填" % total)
    else:
        print("[✓] 已填入 %d 处：%s" % (total, url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
