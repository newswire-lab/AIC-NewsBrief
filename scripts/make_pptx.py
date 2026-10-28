#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成答辩 PPT（16:9，13 页，含演讲者备注）。

为什么用脚本生成而不是手工做
--------------------------
内容全部来自 `docs/技术报告.md` 的实测数字。脚本化后改数字只改一处，
不会出现「报告改了 PPT 没改」的口径不一致；评审要源文件时也能说明数据出自哪一节。

用法：
    python scripts/make_pptx.py                 # 默认输出 dist/AIC-NewsBrief-答辩.pptx
    python scripts/make_pptx.py --out xxx.pptx

注意：中文字体必须同时设置 latin 与 East Asian（a:ea），否则 PowerPoint 回退成宋体。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lxml import etree  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.dml.color import RGBColor  # noqa: E402
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN  # noqa: E402
from pptx.oxml.ns import qn  # noqa: E402
from pptx.util import Emu, Inches, Pt  # noqa: E402

FONT = "Microsoft YaHei"
INK = RGBColor(0x1F, 0x23, 0x28)
GREY = RGBColor(0x5A, 0x62, 0x6B)
ACCENT = RGBColor(0xE8, 0x59, 0x0C)
LIGHT = RGBColor(0xF6, 0xF7, 0xF9)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
DARK = RGBColor(0x12, 0x15, 0x18)

W = Inches(13.333)
H = Inches(7.5)


def style(run, size=18, bold=False, color=INK, name=FONT):
    f = run.font
    f.size = Pt(size)
    f.bold = bold
    f.color.rgb = color
    f.name = name
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = etree.SubElement(rPr, qn("a:ea"))
    ea.set("typeface", name)


def textbox(slide, left, top, width, height, lines, align=PP_ALIGN.LEFT,
            anchor=MSO_ANCHOR.TOP, spacing=1.25):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for i, item in enumerate(lines):
        if isinstance(item, str):
            item = (item, 18, False, INK)
        text, size, bold, color = item
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        r = p.add_run()
        r.text = text
        style(r, size, bold, color)
    return box


def slide_base(prs, title, kicker=None):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bar = s.shapes.add_shape(1, Emu(0), Emu(0), W, Inches(0.10))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()
    top = Inches(0.42)
    if kicker:
        textbox(s, Inches(0.72), top, Inches(11.8), Inches(0.35),
                [(kicker, 13, True, ACCENT)])
        top = Inches(0.78)
    textbox(s, Inches(0.72), top, Inches(11.9), Inches(0.8),
            [(title, 29, True, INK)])
    return s


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def bullets(slide, items, left=Inches(0.85), top=Inches(1.75),
            width=Inches(11.6), height=None, size=19, gap_after=9):
    if height is None:
        height = H - top - Inches(0.35)
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    for i, it in enumerate(items):
        head, body = it if isinstance(it, tuple) else (None, it)
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap_after)
        p.line_spacing = 1.28
        r0 = p.add_run()
        r0.text = "▍ "
        style(r0, size, True, ACCENT)
        if head:
            r1 = p.add_run()
            r1.text = head
            style(r1, size, True, INK)
        r2 = p.add_run()
        r2.text = body
        style(r2, size, False, INK if head else GREY)
    return box


def add_picture_fit(slide, path, left, top, max_w, max_h):
    from PIL import Image

    with Image.open(path) as im:
        iw, ih = im.size
    ratio = min(max_w / iw, max_h / ih)
    pic = slide.shapes.add_picture(str(path), left, top,
                                   Emu(int(iw * ratio)), Emu(int(ih * ratio)))
    pic.line.color.rgb = RGBColor(0xD5, 0xD9, 0xDE)
    return pic


def add_table(slide, data, left, top, width, height, col_sizes=None, size=14):
    rows, cols = len(data), len(data[0])
    tbl = slide.shapes.add_table(rows, cols, left, top, width, height).table
    if col_sizes:
        total = sum(col_sizes)
        for i, c in enumerate(col_sizes):
            tbl.columns[i].width = Emu(int(width * c / total))
    for r in range(rows):
        for c in range(cols):
            cell = tbl.cell(r, c)
            cell.text = ""
            p = cell.text_frame.paragraphs[0]
            run = p.add_run()
            run.text = str(data[r][c])
            style(run, size, r == 0, WHITE if r == 0 else INK)
            cell.fill.solid()
            cell.fill.fore_color.rgb = ACCENT if r == 0 else (LIGHT if r % 2 == 0 else WHITE)
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.08)
            cell.margin_top = Inches(0.04)
            cell.margin_bottom = Inches(0.04)
    return tbl


def dark_slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bg = s.shapes.add_shape(1, Emu(0), Emu(0), W, H)
    bg.fill.solid()
    bg.fill.fore_color.rgb = DARK
    bg.line.fill.background()
    return s


def build(out_path: Path) -> Path:
    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    soft = RGBColor(0xC9, 0xCF, 0xD6)

    # ---- 1 封面 ----
    s = dark_slide(prs)
    textbox(s, Inches(0.9), Inches(1.7), Inches(11.6), Inches(1.0),
            [("多源新闻聚合与可溯源简报智能体", 43, True, WHITE)])
    textbox(s, Inches(0.95), Inches(2.9), Inches(11.6), Inches(0.6),
            [("同一个事件，有几家独立机构在说？可信度多高？", 24, False, ACCENT)])
    ln = s.shapes.add_shape(1, Inches(0.95), Inches(3.7), Inches(2.2), Inches(0.05))
    ln.fill.solid()
    ln.fill.fore_color.rgb = ACCENT
    ln.line.fill.background()
    textbox(s, Inches(0.95), Inches(4.0), Inches(11.2), Inches(1.7), [
        ("第八届全球校园人工智能算法精英大赛 · 算法创新赛", 18, False, soft),
        ("参赛方向：AI + 开源（开源赋能的 AI 应用创新）", 18, False, soft),
        ("开源仓库：github.com/newswire-lab/AIC-NewsBrief", 18, False, soft),
    ], spacing=1.5)
    notes(s, "开场一句话定位：我们不做又一个新闻 App，而是回答一个可验证的问题——这条信息有几家独立机构在说。"
             "全部算法自研，只把「写」这一步交给开源模型。")

    # ---- 2 问题 ----
    s = slide_base(prs, "问题：多源新闻的两个硬伤", "01 / 问题")
    bullets(s, [
        ("同一事件，多家媒体转载改写后互相矛盾。",
         "读者无法判断这几家是不是同一家 —— 全库 234 个事件有 ≥2 个站点，但只有 18 个（0.7%）是 ≥2 家独立机构。"),
        ("生成式摘要会幻觉，且无法逐句核对。",
         "模型写得通顺，却说不出每个事实句出自哪一篇原文。"),
        ("现有工具要么只聚合不判断，要么只生成不溯源。",
         "两者都不提供可验证的「印证」口径。"),
        ("我们的切入：把「印证」变成可计算的量。",
         "按机构去重计数 + 句级溯源覆盖率，两条都可复算。"),
    ], size=20)
    notes(s, "痛点不是「信息太多」，而是「无法判断该信谁」。重点讲「6 家媒体 → 2 家机构」这个实测例子。")

    # ---- 3 方案总览 ----
    s = slide_base(prs, "方案：一条可校验的流水线", "02 / 方案")
    steps = ["采集", "去重", "聚类", "可信度", "归一化", "生成", "校验"]
    hot = {"聚类", "可信度", "校验"}
    x = Inches(0.7)
    for i, st in enumerate(steps):
        box = s.shapes.add_shape(1, x, Inches(1.9), Inches(1.5), Inches(0.85))
        box.fill.solid()
        box.fill.fore_color.rgb = ACCENT if st in hot else LIGHT
        box.line.color.rgb = RGBColor(0xD5, 0xD9, 0xDE)
        p = box.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = st
        style(r, 17, True, WHITE if st in hot else INK)
        if i < len(steps) - 1:
            ar = s.shapes.add_shape(1, x + Inches(1.52), Inches(2.2),
                                    Inches(0.3), Inches(0.26))
            ar.fill.solid()
            ar.fill.fore_color.rgb = RGBColor(0xB8, 0xBF, 0xC7)
            ar.line.fill.background()
        x += Inches(1.83)
    textbox(s, Inches(0.85), Inches(2.95), Inches(11.6), Inches(0.5),
            [("橙色三步是本项目的核心差异化：事件怎么归并、可信度怎么算、生成结果怎么验。",
              16, False, GREY)])
    bullets(s, [
        ("分工原则：", "能算的用自研算法，要「写」的才交给开源模型。"),
        ("校验不过就降级：", "句级依据覆盖率不足 → 自动退回抽取式摘要，宁可不好看也不失真。"),
        ("全链路可复算：", "每个分数都能用 python -m src.cli explain <event_id> 逐因子回放。"),
    ], top=Inches(3.7), size=19)
    notes(s, "这页给评审整体骨架，重点说清「自研 vs 开源」的分界线在哪。")

    # ---- 4 成品 ----
    s = slide_base(prs, "成品：6 个页签、12 个新闻分区", "03 / 成品")
    add_picture_fit(s, ROOT / "docs/screenshots/1_hot_events.png",
                    Inches(0.6), Inches(1.72), Inches(7.7), Inches(5.0))
    bullets(s, [
        ("列表直接给四个判断依据：", "信息来源（独立机构）、事件发生时间（首报）、最新跟进、可信度"),
        ("12 个分区：", "军事/政治/经济/科技/国际/社会/民生/文化/体育/法治/历史/其他"),
        ("自动更新：", "后台 30 分钟采集一轮；界面 60 秒自动重载，只读不写"),
        ("检索同稿折叠：", "同一通稿在 5 个站点上架时折叠为一行，标「N 站同稿」"),
    ], left=Inches(8.55), top=Inches(1.85), width=Inches(4.3), size=15, gap_after=10)
    notes(s, "真机截图，不是设计稿。强调评分构成可逐项验算。")

    # ---- 5 聚类 ----
    s = slide_base(prs, "自研算法①：事件聚类与剪枝", "04 / 核心算法")
    bullets(s, [
        ("三阶段归并：", "内容指纹精确匹配 → 标题 SimHash（海明 ≤3）→ IDF 加权语义相似度（≥0.50）"),
        ("分段桶剪枝：", "64 位 SimHash 切 4 段 16 位，海明 ≤3 的两串必有一段完全相同（抽屉原理）"),
        ("实测剪枝：", "朴素两两比较 248,160 对 → 实际比较 6,587 对，剪到 2.65%，与全量比较等价"),
        ("为什么还要语义层：", "119 对海明 ≤3 的重复全部同机构；跨机构即使把阈值放宽到 ≤10，仍为 0 对"),
        ("阈值是标定出来的：", "同事件样例 0.46~0.80，不同事件 0.00~0.23 → 取 0.50，偏保守"),
    ], size=19)
    notes(s, "两点说透：剪枝是等价变换不是近似；「字面重复几乎都是同机构互转」这个实测发现"
             "直接解释了为什么必须加语义层。")

    # ---- 6 可信度 ----
    s = slide_base(prs, "自研算法②：可信度与机构去重", "05 / 核心算法")
    textbox(s, Inches(0.85), Inches(1.72), Inches(11.6), Inches(1.4), [
        ("credibility = 100 × f_tier × f_time × f_cross", 26, True, INK),
        ("f_tier = 簇内最高来源等级（1.0 / 0.7 / 0.4）　　f_time = exp(-age / 72h)　　"
         "f_cross = 1 − 0.4 / k", 15, False, GREY),
    ])
    bullets(s, [
        ("k = 1 时 f_cross 封顶 0.60：", "单一机构的信息永远拿不到满分 —— 孤证与多源印证被明确区分"),
        ("按「机构」而非「站点」计数（关键修正）：",
         "Army / Navy / Air Force / Military Times + Defense News + C4ISRNET 同属一家出版集团，"
         "按站点会虚报成 5 家印证"),
        ("证据链页签如实展示折叠过程：", "6 站点 → 2 家独立机构；评分可验算 100 × 0.7 × 0.6413 × 0.8 = 35.91"),
    ], top=Inches(3.15), width=Inches(7.3), size=18)
    add_picture_fit(s, ROOT / "docs/screenshots/2_evidence.png",
                    Inches(8.4), Inches(4.45), Inches(4.3), Inches(2.3))
    notes(s, "整个项目最有想法的一处：把「5 家源印证」这个常见说法拆穿。评委只记一件事就记这个。")

    # ---- 7 新词发现 ----
    s = slide_base(prs, "自研算法③：无词典新词发现 → 事件关键词", "06 / 核心算法")
    bullets(s, [
        ("第一版失败（已整体废弃）：", "纯滑窗 2~4 字抽标签，产出的是标题碎片 —— 务院联 / 控机 / 将禁止中国测；"
         "全库 5195 个标签里纯符号占 4.5%、2 字以下占 13%"),
        ("根因不是参数：", "候选没有词边界。滑窗不保证切出的是词，短片段加合并救不回来"),
        ("现在三条规则：", "内聚度（互信息）≥2.0 ／ 自由度 max(左熵, 右熵)≥1.0 ／ 最大 n-gram 碎片消除"),
        ("阈值来自标定：", "28 个好词 vs 21 个手工坏词对照 → 好词命中 33/34、坏词残留 0/23"),
        ("确定性可复现：", "三个不同 PYTHONHASHSEED 起独立进程，摘要完全一致（digest 0bb7eba1bfbe7809）"),
        ("规模：", "词表 3228 词 / 1389 词对；2,532 个事件全部产出标签，全库约 1.2 秒"),
    ], size=17, gap_after=7)
    notes(s, "这页是「AI+开源」加分点：证明团队能把失败方案诊断到根因并重建，而不是一直调参。")

    # ---- 8 校验 ----
    s = slide_base(prs, "自研算法④：事实一致性校验（不过就降级）", "07 / 核心算法")
    add_picture_fit(s, ROOT / "docs/screenshots/3_brief.png",
                    Inches(0.55), Inches(1.68), Inches(7.6), Inches(5.05))
    bullets(s, [
        ("四类检查，全部自研：", "句级溯源覆盖率 / 未声明句检测 / 推测性表述检测 / 长度合规"),
        ("模型必须自报依据：", "输出带 claims（句子 + 来源编号）的严格 JSON"),
        ("不过就降级：", "自动退回抽取式摘要，如实标注 生成后端: extractive"),
        ("实测通过样例：", "句级溯源 7/7 = 100%、217 字、3 家独立机构、12.2 秒"),
        ("通过后仍要剔除：", "有 1 句未被原文支撑 → 按原串删除后再校验"),
    ], left=Inches(8.45), top=Inches(1.85), width=Inches(4.4), size=15, gap_after=10)
    notes(s, "强调「比例判据管整体、剔除管单句」不能互相替代。这最容易被打断，先讲清楚。")

    # ---- 9 数据 ----
    s = slide_base(prs, "实测数据（全部可复现）", "08 / 效果")
    add_table(s, [
        ["指标", "实测值", "口径"],
        ["单轮采集", "约 3,500 条 / 29 个源", "2026-09-24 本机"],
        ["入库规模", "3,707 篇 / 2,551 个事件 / 2,532 个事件有标签", "截至 2026-10-27 运行库"],
        ["聚类剪枝", "248,160 → 6,587 对（2.65%）", "等价变换"],
        ["多源印证", "234 个事件有 ≥2 站点，但仅 18 个有 ≥2 家独立机构", "全库统计"],
        ["可信度去重", "6 站点 → 2 家独立机构", "证据链页签"],
        ["事件关键词", "2,532 个事件有标签（99%），全库约 1.2 秒", "跨进程逐字节一致"],
        ["简报生成", "3B / 4-bit，显存 1.92 GB，单次 12.2 秒", "8 GB 显卡可跑"],
        ["库内检索", "12~187 ms（LIKE 全表扫描）", "千级库"],
    ], Inches(0.8), Inches(1.72), Inches(11.7), Inches(4.6),
        col_sizes=[2.0, 5.6, 3.0], size=15)
    textbox(s, Inches(0.85), Inches(6.45), Inches(11.6), Inches(0.5),
            [("每条数字都能用仓库脚本重跑：evaluate_similarity.py / compare_models.py / check_brief_length.py",
              13, False, GREY)])
    notes(s, "背下几个最硬的：2.65% 剪枝、6 站点→2 机构、7/7 溯源、1.92 GB 显存。")

    # ---- 10 缺陷驱动 ----
    s = slide_base(prs, "缺陷驱动的改进（这部分才是工程能力）", "09 / 过程")
    add_table(s, [
        ["模块", "缺陷", "修法"],
        ["关键词", "子串内聚度高于整词（务院 4.73 > 国务院 4.26）", "加最大 n-gram 规则"],
        ["关键词", "词对跨段拼接（预期中新社）", "词对逐段生成"],
        ["关键词", "虚词表误伤词尾（军事基地 → 军事基）", "拆 NO_START / NO_END"],
        ["分区", "平局按 Unicode 码位排序 → 政治恒胜", "显式语义优先级"],
        ["分区", "图注被当正文（无人机照片）", "判分前剔除"],
        ["校验", "比例闸门通过但正文留着错句", "通过后逐句剔除"],
        ["采集", "HTML 源只解析列表页，从不抓详情", "补详情页抓取 + 存量回填"],
    ], Inches(0.8), Inches(1.68), Inches(11.7), Inches(3.95),
        col_sizes=[1.3, 6.6, 3.4], size=14)
    textbox(s, Inches(0.85), Inches(5.8), Inches(11.7), Inches(1.2), [
        ("共同特征：每一处都只有跑真实数据才看得见，读代码时每条都看起来是对的。", 17, True, ACCENT),
        ("8 个关键词缺陷 + 3 个分类 bug + 3 个校验层缺陷，全部有实测证据与修复记录。", 15, False, GREY),
    ])
    notes(s, "评审最看重「实现完成度」。这页证明不是写完就交，而是用真实数据把缺陷一个个挖出来。")

    # ---- 11 AI + 开源 ----
    s = slide_base(prs, "AI + 开源：边界清楚，许可合规", "10 / 开源融合")
    bullets(s, [
        ("开源模型是核心组件，不是装饰：", "Qwen2.5-3B-Instruct（Apache-2.0），4-bit NF4 量化，"
         "承担标题、价值点、300 字简报生成与跨语言归一化"),
        ("自研算法是自主边界：", "RSS/Atom/RDF 解析、HTML 抽取、SimHash 去重、IDF 相似度、聚类、可信度、"
         "新词发现、12 分区分类、检索、事实校验 —— 全部自研"),
        ("刻意未用的现成库：", "feedparser / simhash / scikit-learn / jieba，都可替换，但选择自己实现以划清边界"),
        ("许可与数据合规：", "MIT 许可；第三方清单逐项列明；只聚合公开发布内容，原文仅本地存档不分发"),
        ("匿名与自查：", "提供 check_anonymize.py；填入 10 条真实身份条目后扫描 121 个文件，0 处命中"),
    ], size=18)
    notes(s, "直接对应 20 分的「AI 与开源融合」。核心论点：开源模型解决「写」，"
             "自研算法解决「算」与「验」，两者不可互相替代。")

    # ---- 12 局限 ----
    s = slide_base(prs, "已知局限（如实说明）", "11 / 局限")
    bullets(s, [
        ("跨机构印证事件总量偏少：", "早期 705 篇批次实测仅 2 个；当前库 18 个（占 0.7%）—— "
         "属数据事实而非算法缺陷（同社稿件被多站转载），已新增 4 个独立机构源缓解"),
        ("通过率与素材厚度强相关：", "素材仅 267 字的事件要求写 300 字且句句可核对并不现实 → 目标字数自适应"),
        ("无 NER：", "音译专名与地名分不开，仍有个别残渣（斯特）；词表随语料增长自动重建"),
        ("语义层对中文官方模板标题有误判：", "中共中央办公厅印发某办法 类相似度 0.508，贴近阈值"),
        ("HTML 抽取对纯 JS 渲染页无效：", "站点改版会失效 → 提供 python -m src.cli health 体检"),
    ], size=18)
    notes(s, "主动承认局限比被问出来强，每条都配了「已做的缓解」。")

    # ---- 13 结尾 ----
    s = dark_slide(prs)
    textbox(s, Inches(0.95), Inches(1.5), Inches(11.6), Inches(0.9),
            [("开放成果与复现入口", 40, True, WHITE)])
    textbox(s, Inches(1.0), Inches(2.65), Inches(11.3), Inches(3.4), [
        ("代码仓库　github.com/newswire-lab/AIC-NewsBrief（MIT，100+ 个文件）", 19, False, soft),
        ("技术报告　docs/技术报告.pdf（16 页）", 19, False, soft),
        ("演示视频　docs/演示视频.mp4（4 分 05 秒，含字幕）", 19, False, soft),
        ("复现　pip install -r requirements.txt && python -m src.cli pipeline", 19, False, soft),
        ("　　　python -m src.app　→　http://127.0.0.1:7860", 19, False, soft),
    ], spacing=1.55)
    textbox(s, Inches(1.0), Inches(6.25), Inches(11.3), Inches(0.6),
            [("谢谢　—　欢迎提问", 22, True, ACCENT)])
    notes(s, "结尾给三个可点击入口，让评委能立刻验证。")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成答辩 PPT")
    ap.add_argument("--out", default=str(ROOT / "dist" / "AIC-NewsBrief-答辩.pptx"))
    args = ap.parse_args()
    p = build(Path(args.out))
    n = len(Presentation(str(p)).slides)
    print("[OK] %s  %d 页  %.1f KB" % (p, n, os.path.getsize(p) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
