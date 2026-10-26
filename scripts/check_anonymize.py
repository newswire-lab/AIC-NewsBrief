"""提交前匿名化自查。

依据《全球校园人工智能算法精英大赛章程》第四十四条：

    参赛材料提交规范。参赛团队提交作品时，须同步提交"承诺书"。
    匿名版作品中严禁出现参赛人员姓名、指导教师姓名、参赛学校等
    任何可识别身份信息，保障评审环节的公平公正。

本脚本扫描源码、配置与文档，报告：

1. 命中自查清单（config/anonymize_watchlist.txt）的身份信息
2. 形如 C:\\Users\\<用户名> 或 /home/<用户名> 的本机绝对路径
3. 邮箱、手机号等模式化隐私信息
4. （加 --strict 时）中文学校名模式 <2~10 个汉字>+大学/学院

用法：
    copy config\\anonymize_watchlist.txt.example config\\anonymize_watchlist.txt
    # 编辑副本，逐行写入姓名/学校/学号等
    python scripts/check_anonymize.py
    python scripts/check_anonymize.py --all        # 连 data/ 与 logs/ 一起扫
    python scripts/check_anonymize.py --strict     # 追加学校名模式

退出码：0 = 未发现问题；1 = 发现问题（需清理后再提交）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Iterable, List, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_EXTS = {
    ".py", ".md", ".yaml", ".yml", ".txt", ".json", ".toml", ".cfg", ".ini",
    ".html", ".htm", ".css", ".js", ".ts", ".bat", ".ps1", ".sh", ".example",
    ".gitignore", ".csv", ".xml",
}

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache"}
# dist/ 是构建产物目录（已在 .gitignore 中），里面只有 PDF 与中间 HTML；
# 中间 HTML 里的图片被解析成了 file:// 绝对 URI，必然含本机用户名，
# 但该文件本身不随作品分发，所以与 data/、logs/ 一样跳过。
DEFAULT_SKIP_DIRS = {"data", "logs", "dist"}
SKIP_FILES = {
    os.path.join("config", "anonymize_watchlist.txt"),   # 清单本身含真实信息
    os.path.join("scripts", "check_anonymize.py"),       # 本脚本含规则字面量
}

PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("本机绝对路径(Windows)", re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}[^\\\s\"'<>|]+")),
    ("本机绝对路径(Windows 反斜杠)", re.compile(r"[A-Za-z]:/Users/[^/\s\"'<>|]+")),
    ("本机绝对路径(POSIX 家目录)", re.compile(r"/(?:home|Users)/[A-Za-z0-9_.\-]{2,}")),
    ("邮箱地址", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("中国大陆手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

STRICT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("中文学校名", re.compile(r"[\u4e00-\u9fff]{2,10}(?:大学|学院|高级中学|中学)")),
    ("疑似学号", re.compile(r"(?<!\d)(?:19|20)\d{2}\d{6,10}(?!\d)")),
]

WHITELIST_PATTERNS = [
    # 许可协议、依赖声明里的版权人署名不算身份泄露
    re.compile(r"AIC-NewsBrief contributors"),
]

# 模板文件中有意使用的占位示例，不属于真实身份信息，扫描时跳过
SAMPLE_TOKENS = {
    "zhangsan@example.edu.cn", "example.edu.cn", "example.com",
    "张三", "李四", "示例大学", "示例大学计算机学院", "2022012345",
}


def is_sample(hit: str) -> bool:
    return hit in SAMPLE_TOKENS or "@example." in hit


def load_watchlist(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    terms: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            terms.append(line)
    return terms


def iter_files(skip_dirs: set) -> Iterable[str]:
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in skip_dirs]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            rel = os.path.relpath(os.path.join(root, name), PROJECT_ROOT)
            if rel.replace("/", os.sep) in SKIP_FILES:
                continue
            if ext in TEXT_EXTS or name in (".gitignore", "LICENSE"):
                yield os.path.join(root, name)


def is_whitelisted(text: str) -> bool:
    return any(p.search(text) for p in WHITELIST_PATTERNS)


def scan_file(path: str, watchlist: List[str], strict: bool) -> List[Tuple[int, str, str]]:
    findings: List[Tuple[int, str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return findings

    patterns = list(PATTERNS) + (STRICT_PATTERNS if strict else [])
    for lineno, line in enumerate(lines, 1):
        if is_whitelisted(line):
            continue
        for term in watchlist:
            if term and term in line and not is_sample(term):
                findings.append((lineno, "自查清单命中", term))
        for label, pattern in patterns:
            for m in pattern.finditer(line):
                hit = m.group(0)
                if is_whitelisted(hit) or is_sample(hit):
                    continue
                findings.append((lineno, label, hit))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="提交前匿名化自查")
    parser.add_argument("--all", action="store_true",
                        help="连同 data/ 与 logs/ 一起扫描（默认跳过）")
    parser.add_argument("--strict", action="store_true",
                        help="追加学校名/学号等宽松模式（可能有误报，需人工判断）")
    args = parser.parse_args()

    watchlist_path = os.path.join(PROJECT_ROOT, "config", "anonymize_watchlist.txt")
    watchlist = load_watchlist(watchlist_path)

    skip_dirs = set() if args.all else set(DEFAULT_SKIP_DIRS)

    print("=" * 74)
    print("匿名化自查")
    print("=" * 74)
    print(f"项目根目录 : {PROJECT_ROOT}")
    print(f"自查清单   : {watchlist_path}")
    if watchlist:
        print(f"           已加载 {len(watchlist)} 个自查词")
    else:
        print("           [提示] 清单不存在或为空，仅做模式化扫描。")
        print("                  建议先执行：copy config\\anonymize_watchlist.txt.example "
              "config\\anonymize_watchlist.txt")
        print("                  然后填入姓名/学校/学号等再运行。")
    print(f"扫描范围   : {'全部目录' if args.all else '跳过 data/ 与 logs/'}"
          f"{' + 宽松模式' if args.strict else ''}")
    print("-" * 74)

    total = 0
    files_scanned = 0
    for path in iter_files(skip_dirs):
        files_scanned += 1
        findings = scan_file(path, watchlist, args.strict)
        if not findings:
            continue
        rel = os.path.relpath(path, PROJECT_ROOT)
        print(f"\n[!] {rel}")
        seen = set()
        for lineno, label, hit in findings:
            key = (label, hit)
            if key in seen:
                continue
            seen.add(key)
            print(f"    第 {lineno} 行  {label}: {hit}")
            total += 1

    print("-" * 74)
    print(f"扫描文件 {files_scanned} 个，发现问题 {total} 处")
    if total == 0:
        print("结果：未发现可识别身份信息，可以提交。")
        return 0
    print("结果：存在需要处理的项，请清理后重新运行本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
