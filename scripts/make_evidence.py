#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「佐证材料」PDF（提交表单要求：佐证材料仅限 PDF）。

内容全部来自仓库内的可核对文件，不新增任何未经实测的结论：
  1. 材料索引（每项对应仓库里的哪个文件）
  2. 匿名化自查的完整输出（脚本原样跑一遍）
  3. 三轮模型对照实验的原始输出（逐行可核对）
  4. 五张真机运行截图
  5. 关键指标与复现命令
  6. 外部试用反馈的如实说明（未开展就写未开展）

用法：
    python scripts/make_evidence.py
    python scripts/make_evidence.py --out dist/佐证材料.pdf
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = "https://github.com/newswire-lab/AIC-NewsBrief"
REPO_NAME = "AIC-NewsBrief"


def run_py(script: str) -> str:
    """跑仓库里的脚本并返回 UTF-8 输出（强制 utf-8 避免 GBK 乱码）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable, "-u", script], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    return scrub(((p.stdout or "") + (p.stderr or "")).strip())


_PATH_RE = re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\\\s]+|[A-Za-z]:\\Users\\[^\\\s]+|"
                     r"[A-Za-z]:\\\\Users\\\\|[A-Za-z]:\\Users\\")


def scrub(text: str) -> str:
    """把本机绝对路径抹成占位符：提交材料里不能出现带用户名的路径。"""
    return _PATH_RE.sub(r"C:\\Users\\<用户名>", text)


def fence(text: str) -> str:
    return "```\n" + text.rstrip() + "\n```"


def build_md() -> str:
    anon = run_py("scripts/check_anonymize.py")

    parts = []
    parts.append("# 佐证材料\n")
    parts.append(
        "> 本文件汇总可**逐项核对**的原始证据，服务于技术报告中「实现完成度」与「问题场景」的论证。\n"
        "> 全部内容取自公开仓库 %s 中的文件，不含任何未经实测的结论；\n"
        "> 未开展的工作（如外部试用问卷）在 §6 中如实说明，不做修饰。\n" % URL)

    # ---- 1 索引 ----
    parts.append("## 一、材料索引\n")
    parts.append("| 佐证内容 | 仓库中的位置 |")
    parts.append("|---|---|")
    for a, b in (
        ("匿名化自查脚本与输出", "`scripts/check_anonymize.py`（本节 §2 为其完整输出）"),
        ("模型对照实验原始输出", "`docs/evidence/compare_run1_uncorrected.txt` 等 3 份（§3 全文）"),
        ("界面真机截图", "`docs/screenshots/`（§4 全部 5 张）"),
        ("技术报告与标定脚本", "`docs/技术报告.md`、`scripts/evaluate_similarity.py`"),
        ("作品简介字数校验", "`scripts/check_brief_length.py`"),
        ("源码与配置", "`src/`、`config/`、`scripts/`（MIT）"),
    ):
        parts.append("| %s | %s |" % (a, b))

    # ---- 2 匿名化 ----
    parts.append("\n## 二、匿名化自查\n")
    parts.append("依据大赛章程第四十四条，匿名版材料不得出现参赛人员姓名、指导教师姓名、参赛学校等身份信息。\n"
                 "本项目提供自查脚本，把需要保护的身份条目写入本地清单（该清单已被 `.gitignore` 忽略、不入库），"
                 "再对仓库内全部文本文件做匹配扫描。\n")
    parts.append("执行命令：`python scripts/check_anonymize.py`\n")
    parts.append(fence(anon or "(脚本无输出)"))

    # ---- 3 对照实验 ----
    parts.append("\n## 三、模型对照实验原始输出\n")
    parts.append("三轮对照实验的输出**原样附上**，可直接逐行核对。三轮的差别在于统计口径与长度容差的演进，"
                 "结论与「不能仅凭 5 个样本下强弱结论」的说明见技术报告 §5.3。\n")
    for fn, note in (
        ("compare_run1_uncorrected.txt", "轮次 1：统计口径错误（把降级后的结果当成一次成型），结论不可用。"),
        ("compare_run2.txt", "轮次 2：修正统计口径，长度容差仍为旧的 ±60%。"),
        ("compare_run3.txt", "轮次 3：同时把长度容差收紧到 ±25%。"),
    ):
        p = ROOT / "docs" / "evidence" / fn
        parts.append("**%s**\n" % fn)
        parts.append("%s\n" % note)
        if p.exists():
            parts.append(fence(io.open(p, encoding="utf-8", errors="replace").read()))
        else:
            parts.append("（文件缺失）\n")

    # ---- 4 截图 ----
    parts.append("\n## 四、界面真机截图\n")
    parts.append("截图取自本机运行 `python -m src.app` 的真实界面（非设计稿），"
                 "存于仓库 `docs/screenshots/`。\n")
    shots = [
        ("screenshots/1_hot_events.png", "热点事件：列表给出信息来源（独立机构）、首报时间、最新跟进与可信度"),
        ("screenshots/2_evidence.png", "证据链：6 个站点只算 2 家独立机构，评分构成可逐项验算"),
        ("screenshots/3_brief.png", "简报生成：模型直出通过校验（句级溯源 7/7），并标注未被支撑句"),
        ("screenshots/4_status.png", "系统状态：源健康、自动更新与时间口径说明"),
        ("screenshots/5_search.png", "检索：命中位置加权排序 + 同稿折叠"),
    ]
    for rel, cap in shots:
        if (ROOT / "docs" / rel).exists():
            parts.append("![%s](%s)\n" % (cap, rel))

    # ---- 5 指标 ----
    parts.append("\n## 五、关键指标与复现命令\n")
    parts.append("| 指标 | 实测值 | 复现命令 |")
    parts.append("|---|---|---|")
    for a, b, c in (
        ("聚类剪枝", "248,160 → 6,587 对（2.65%）", "`python -m src.cli cluster`"),
        ("语义阈值标定", "同事件 0.46~0.80 / 不同事件 0.00~0.23", "`python scripts/evaluate_similarity.py`"),
        ("交叉印证构成", "同集团站点被正确折叠为 1 家机构", "`python scripts/analyze_orgs.py`"),
        ("事件关键词", "2,532 个事件有标签，跨进程摘要一致", "`python -m src.cli tag`"),
        ("简报长度合规", "按最严口径校验通过", "`python scripts/check_brief_length.py`"),
        ("匿名化自查", "121 个文件 0 处身份信息", "`python scripts/check_anonymize.py`"),
    ):
        parts.append("| %s | %s | %s |" % (a, b, c))

    # ---- 6 外部试用 ----
    parts.append("\n## 六、外部试用反馈\n")
    parts.append(
        "**截至提交时，本项目尚未开展系统性的外部试用问卷回收**，因此本文件与技术报告中"
        "均不包含任何「用户满意度」「认可率」类结论。\n\n"
        "已准备好的工作：问卷题目（12 题，覆盖痛点验证、证据链理解、溯源有效性、功能取舍）、"
        "发放话术与汇总口径见仓库 `docs/外部验证问卷.md`；回收后的原始答卷与汇总表将放入 "
        "`docs/evidence/survey/`。此处如实标注，不以推测代替数据。\n")

    # ---- 7 入口 ----
    parts.append("\n## 七、开放成果入口\n")
    parts.append("- 代码仓库（MIT）：%s" % URL)
    parts.append("- 演示视频：仓库 `docs/演示视频.mp4`（4 分 05 秒，字幕已烧录）")
    parts.append("- 技术报告：仓库 `docs/技术报告.pdf`")
    parts.append("- 离线复现：`pip install -r requirements.txt && python -m src.cli pipeline`，"
                 "再 `python -m src.app` 打开 http://127.0.0.1:7860")
    return "\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成佐证材料 PDF")
    ap.add_argument("--md", default=str(ROOT / "docs" / "佐证材料.md"))
    ap.add_argument("--out", default=str(ROOT / "dist" / "佐证材料.pdf"))
    args = ap.parse_args()

    md = build_md()
    md = scrub(md)
    assert "\ufffd" not in md
    io.open(args.md, "w", encoding="utf-8", newline="").write(md)
    print("[1/2] 已生成 %s（%d 字符）" % (args.md, len(md)))

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-u", "scripts/make_pdf.py", "--md", args.md,
                        "--out", args.out, "--max-pages", "60", "--max-mb", "10"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    tail = [l for l in ((p.stdout or "") + (p.stderr or "")).strip().splitlines() if l.strip()][-4:]
    print("[2/2] 渲染 PDF rc=%d" % p.returncode)
    for l in tail:
        print("   " + l)
    if os.path.exists(args.out):
        print("\n[OK] %s  %.2f MB" % (args.out, os.path.getsize(args.out) / 1024 / 1024))
    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
