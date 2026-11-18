#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从答辩 PPT 的演讲者备注生成《答辩视频录制脚本》。

为什么单独生成
--------------
录制要求是「无快进、无剪辑、一镜到底」，也就是说不能靠后期补救，
只能照着稿子一遍过。把 PPT 备注抽成一张表，录的时候打印出来照着念，
既不漏打分项，也不会超时。

用法：
    python scripts/make_record_script.py
    python scripts/make_record_script.py --pptx dist/xxx.pptx --out docs/xxx.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from pptx import Presentation  # noqa: E402

NOTE_RE = re.compile(r"【(\d+:\d+)–(\d+:\d+)｜(\d+)\s*秒】")


def mmss(sec: int) -> str:
    return "%d:%02d" % (sec // 60, sec % 60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pptx", default=str(ROOT / "dist" / "AIC-NewsBrief-答辩-国赛.pptx"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "答辩视频录制脚本.md"))
    a = ap.parse_args()

    prs = Presentation(a.pptx)
    rows, body = [], []
    total = 0

    for i, slide in enumerate(prs.slides, 1):
        note = ""
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text or ""
        note = note.strip()

        # 页面标题：跳过右上角四段导航条与 kicker，取第一个正文文本框首行
        title = ""
        for shp in slide.shapes:
            if not shp.has_text_frame:
                continue
            lines = [x.strip() for x in shp.text_frame.text.splitlines() if x.strip()]
            if not lines:
                continue
            first = lines[0]
            if "›" in first or "评分项" in first:   # 四段导航条 / 「第几项·分值」kicker
                continue
            title = first
            break

        m = NOTE_RE.search(note)
        if m:
            start, secs = m.group(1), int(m.group(3))
            total += secs
            rows.append((i, title, start, secs, mmss(total)))
            rest = NOTE_RE.sub("", note).strip()
        else:
            rows.append((i, title, "-", 0, "-"))
            rest = note

        body.append("### 第 %d 页 · %s\n\n```\n%s\n```\n" % (i, title, rest or "(无备注)"))

    head = [
        "# 答辩视频录制脚本（国赛 · 全国半决赛）",
        "",
        "> 依据《关于举办第八届全球校园人工智能算法精英大赛算法主题赛全国半决赛的通知》"
        "（全智赛组委会〔2026〕83 号）附件《比赛流程》整理。",
        "> 本文件由 `scripts/make_record_script.py` 从 `dist/AIC-NewsBrief-答辩-国赛.pptx` "
        "的演讲者备注自动生成，改 PPT 备注即可同步更新。",
        "",
        "## 一、硬性规则（违反即无效或扣分）",
        "",
        "| 规则 | 具体要求 |",
        "|---|---|",
        "| 录制工具 | **腾讯会议**录制，不能用本地录屏软件 |",
        "| 出镜 | 全体队员进入会议并**开启摄像头**，全程在线出镜，画面能清晰识别每个人 |",
        "| 人员一致 | 出镜人员必须与报名系统一致，**不符视为作弊** |",
        "| 开场 | 视频开始后，先由团队代表清晰报出**团队名称** |",
        "| 顺序 | 对照**评分表打分项顺序**汇报：每页 kicker 标「第几项 / 分值」，"
        "右上角四段导航（① 背景 → ② 创新点 → ③ 作品介绍 → ④ 展望）标当前进度 |",
        "| 时长 | 汇报 + 演示 **≤ 6 分钟**；本脚本目标 **4:50**（同时满足初赛 3~5 分钟口径） |",
        "| 后期 | **无快进、无剪辑、无遮挡**，一镜到底 |",
        "| 画面 | PPT 共享屏幕与出镜人员**在同一画面内** |",
        "| 环境 | 全程安静，不允许出现视频遮挡 |",
        "",
        "## 二、录制前 5 分钟检查",
        "",
        "- [ ] 腾讯会议：开启「共享屏幕 → 共享 PPT 窗口」，勾选**同时共享电脑声音**（若演示有声音）",
        "- [ ] 布局切换到「宫格视图」，确认 3 人画面 + PPT 同框，且 PPT 字号看得清",
        "- [ ] 关闭微信 / QQ / 邮箱弹窗与系统通知，手机静音",
        "- [ ] 桌面只留 PPT 窗口，避免露出与作品无关的文件名、聊天窗口",
        "- [ ] 电脑桌面背景、会议昵称**不得含学校名、指导老师姓名**（会议昵称建议改为队员姓名）",
        "- [ ] PPT 第 9 页已插入演示录屏，设为自动播放",
        "- [ ] 准备计时：手机秒表放画面外，或队友用另一台设备看时间并用手势提示",
        "",
        "## 三、三人分工建议",
        "",
        "| 角色 | 负责页码 | 说明 |",
        "|---|---|---|",
        "| 主讲（建议队长） | 第 1–5 页 | 开场报团队名，讲 ① 背景 + ② 创新点（方案、四处自研、AI 与开源边界） |",
        "| 演示 / 数据 | 第 6–8 页 | 讲 ③ 作品介绍：成品界面、实测数据、开放成果 |",
        "| 收尾 | 第 9–10 页 | 播放演示录屏，讲 ④ 展望：局限与后续 |",        "",
        "> 分工可自行调整，但要保证**每个人都在画面里全程出镜**，不能只有主讲露脸。",
        "",
        "## 四、时间轴",
        "",
        "| 页 | 内容 | 开始 | 本页秒数 | 累计 |",
        "|---:|---|---|---:|---|",
    ]
    for i, title, start, secs, cum in rows:
        head.append("| %d | %s | %s | %d | %s |" % (i, title, start, secs, cum))
    head.append("| | **合计** | | **%d 秒 = %s** | |" % (total, mmss(total)))
    head.append("")
    head.append("## 五、逐页口播词")
    head.append("")
    head.append("> 方括号内是配速提示，不要念出来。带「口播：」的是可以直接照着读的话。")
    head.append("")

    text = "\n".join(head) + "\n" + "\n".join(body)
    text += """
## 六、录完之后

- [ ] 回看一遍：确认**从头到尾没有剪辑点**、没有黑屏跳变、没有被弹窗遮挡
- [ ] 确认开场第一句报出了团队名称
- [ ] 确认时长为 **4:50 左右，且不超过 6 分钟**（同时控制在 3~5 分钟内更稳妥）
- [ ] 导出 MP4，体积控制在 300 MB 以内（1080p / 30fps 通常 100~200 MB）
- [ ] 上传到报名系统**【视频类文件】**栏目，截止 **11 月 19 日 23:59**
- [ ] 上传后**下载一次自己的视频**，确认能正常播放（防止上传损坏）

## 七、常见导致无效的情况

1. 用了剪映 / OBS 录屏并做了剪辑 —— 通知明确「一试发现视为无效作品」；
2. 只有主讲出镜，其他队员只开麦不开摄像头；
3. PPT 里出现校名、校徽、学院简称、指导老师姓名（确需出现要**提前**发邮件到赛题负责人邮箱报备，地址见官方通知）；
4. 超时 —— 汇报 + 演示超过 6 分钟；
5. 逾期 —— 11 月 19 日 23:59 之后无法补交。
"""
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print("[OK] %s" % out)
    print("     总时长预算 %d 秒 = %s（上限 6:00）" % (total, mmss(total)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
