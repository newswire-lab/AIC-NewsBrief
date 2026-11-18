#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成国赛（全国半决赛）答辩 PPT —— 严格按评分表 6 个打分项顺序编排。

为什么单独出一个脚本
--------------------
半决赛通知（全智赛组委会〔2026〕83 号）明确要求：
「由参赛团队代表对照评分表打分项顺序进行 PPT 汇报」。
初赛那版 PPT 是按「问题→方案→成品→算法→效果→过程→开源→局限→成果」讲的，
评委拿着评分表逐项打分时对不上号。本脚本把版式改成六个分节，每页 kicker 直接
标出「第几项 / 分值」，评委可以边听边在表上打勾。

时长预算：汇报 + 演示合计 4 分 50 秒（初赛要求 3~5 分钟，半决赛要求 ≤6 分钟，
取交集）。每页备注里写死了口播词、单页秒数与累计时间，录制时照读即可。

用法：
    python scripts/make_pptx_national.py            # → dist/AIC-NewsBrief-答辩-国赛.pptx
    python scripts/make_pptx_national.py --out x.pptx

注意：中文字体必须同时设置 latin 与 East Asian（a:ea），否则 PowerPoint 回退成宋体。
"""
from __future__ import annotations

import argparse
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

TEAM = "孤寡"                      # 开场必须报出的团队名称
PRODUCT = "多源新闻聚合与可溯源简报智能体"


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
        textbox(s, Inches(0.72), top, Inches(11.9), Inches(0.35),
                [(kicker, 13, True, ACCENT)])
        top = Inches(0.78)
    textbox(s, Inches(0.72), top, Inches(11.9), Inches(0.8),
            [(title, 28, True, INK)])
    return s


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def bullets(slide, items, left=Inches(0.85), top=Inches(1.72),
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


def line(slide, left, top, width, color=ACCENT):
    ln = slide.shapes.add_shape(1, left, top, width, Inches(0.05))
    ln.fill.solid()
    ln.fill.fore_color.rgb = color
    ln.line.fill.background()
    return ln


def build(out_path: Path) -> Path:
    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    soft = RGBColor(0xC9, 0xCF, 0xD6)
    shot = ROOT / "docs" / "screenshots"

    # ========== 1 封面（0:00–0:20，20s） ==========
    s = dark_slide(prs)
    textbox(s, Inches(0.9), Inches(1.55), Inches(11.6), Inches(1.0),
            [(PRODUCT, 41, True, WHITE)])
    textbox(s, Inches(0.95), Inches(2.75), Inches(11.6), Inches(0.6),
            [("同一个事件，有几家独立机构在说？可信度多高？", 23, False, ACCENT)])
    line(s, Inches(0.95), Inches(3.55), Inches(2.2))
    textbox(s, Inches(0.95), Inches(3.85), Inches(11.2), Inches(2.0), [
        ("第八届全球校园人工智能算法精英大赛 · 算法主题赛 · AI+开源", 18, False, soft),
        (f"团队名称：{TEAM}", 20, True, WHITE),
        ("开源仓库：github.com/newswire-lab/AIC-NewsBrief（MIT）", 18, False, soft),
    ], spacing=1.5)
    notes(s,
          "【0:00–0:20｜20 秒】\n"
          f"口播：「评委老师好，我们的团队名称是「{TEAM}」，作品是{PRODUCT}。\n"
          "我们不做又一个新闻 App，只回答一个可验证的问题——这条信息，有几家独立机构在说。」\n"
          "要点：开场先报团队名称（通知要求），然后一句定位，不要念 PPT 标题。")

    # ========== 2 问题与场景价值（15 分）0:20–0:55，35s ==========
    s = slide_base(prs, "问题：多源新闻的两个硬伤", "评分项 1/6 · 问题与场景价值（15 分）")
    bullets(s, [
        ("用户与场景：",
         "想核实一条新闻真伪的读者、内容审核人员与研究者；他们每天面对几百条互相转载的报道，"
         "却没有工具告诉他们「这几家是不是同一家」。"),
        ("硬伤一 · 转载改写后互相矛盾，且无法判断信源独立性。",
         "全库 2,551 个事件中，234 个拥有 ≥2 个站点，但只有 18 个（0.7%）由 ≥2 家独立机构报道——"
         "「多家报道」这件事被系统性高估了。"),
        ("硬伤二 · 生成式摘要会幻觉，且无法逐句核对。",
         "模型写得通顺，却说不出每个事实句出自哪一篇原文。"),
        ("现有工具：",
         "要么只聚合不判断，要么只生成不溯源；两者都不提供可验证的印证口径。"),
    ], size=17)
    notes(s,
          "【0:20–0:55｜35 秒】\n"
          "口播：先说场景——读者看到十条转载，其实可能只有一家在讲。我们统计了全库 2,551 个事件，\n"
          "234 个有多个站点，但真正的两家以上独立机构只有 18 个，占 0.7%。所以「多家报道」这件事\n"
          "是被系统性高估的。第二个硬伤是 AI 摘要会幻觉，你没法逐句核对它到底从哪来。\n"
          "这两点就是我们要解决的问题，也是评分的第一个打分项。")

    # ========== 3 方案流水线（创新 20 分）0:55–1:25，30s ==========
    s = slide_base(prs, "方案：一条可校验的流水线", "评分项 2/6 · 创新性与方案设计（20 分）")
    textbox(s, Inches(0.85), Inches(1.70), Inches(11.7), Inches(1.1), [
        ("采集 → 去重 → 事件聚类 → 剪枝 → 可信度 → 简报生成 → 事实校验 → 不过就降级",
         21, True, INK)], spacing=1.35)
    bullets(s, [
        ("采集：", "自研 RSS/HTML 解析（纯标准库），30 分钟一轮，写入 SQLite。"),
        ("去重：", "自研 64 位 SimHash，去掉同一篇稿子的重复转载。"),
        ("聚类：", "字符 2/3-gram + IDF 加权 TF-IDF + 余弦相似度，不依赖中文分词词典。"),
        ("可信度：", "按「独立机构数」而非站点数计算，同一出版集团折叠为一家。"),
        ("简报与校验：", "本地开源模型负责写；写完逐句回查原文，核不过就退回原文摘录。"),
    ], top=Inches(2.95), size=17)
    notes(s,
          "【0:55–1:25｜30 秒】\n"
          "口播：方案是一条八步流水线。前面六步全部自研，只把「写」这一步交给开源模型，\n"
          "最后一步是逐句校验，校验不过就不发，退回原文摘录。\n"
          "这样做的目的只有一个：产物里每一句话都能指回具体原文。")

    # ========== 4 四个自研创新点 1:25–2:03，38s ==========
    s = slide_base(prs, "四个自研创新点（自主实现部分）",
                   "评分项 2/6 · 创新性与方案设计（20 分）· 续")
    bullets(s, [
        ("① 事件聚类 + 剪枝：",
         "先按关键词倒排取候选对，只对候选对算相似度，实测比对量压到 2.65%，"
         "全量两两比对（数百万对）降到可实时完成的规模。"),
        ("② 机构级去重（关键口径创新）：",
         "Defense News / Army Times / Military Times / Navy Times / Air Force Times 同属一家出版集团，"
         "5 个站点互转只计 1 家；若按站点数计，单个事件会被虚报成 6 家印证。"),
        ("③ 无词典新词发现：",
         "用字符 2/3-gram 的频次与凝聚度抓未登录词（人名、装备型号），不装 jieba、不要词典，"
         "冷启动也能用。"),
        ("④ 事实一致性校验 + 自动降级：",
         "逐句核对是否有原文支撑，未支撑句按原串删除后重校验；仍不过就整篇降级为抽取式摘要，"
         "宁可生硬也不让没根据的话发出去。"),
    ], size=17)
    notes(s,
          "【1:25–2:03｜38 秒】\n"
          "口播：四个自研点里，第二个最容易被忽略但最影响结论——机构级去重。\n"
          "防务新闻那五家其实是同一个出版集团，页面互转，如果按站点数算，一个事件会被虚报成 6 家印证。\n"
          "我们按机构算，只计 1 家。第四点是校验和降级：核不过就不硬发，退回原文摘录。\n"
          "这两点合起来，才让「可信度」这个数字站得住。")

    # ========== 5 AI 与开源融合（20 分）2:03–2:38，35s ==========
    s = slide_base(prs, "AI 与开源：边界清楚，许可合规",
                   "评分项 3/6 · AI 与开源融合（20 分）")
    add_table(s, [
        ["开源资源", "许可证", "在本作品中的作用"],
        ["Qwen2.5-3B-Instruct", "Apache-2.0",
         "只负责「写」：生成短标题与约 300 字简报；本地 4-bit NF4，实测 1.92 GB 显存，全程不联网"],
        ["PyTorch 2.11.0 / transformers 5.17.0", "BSD-3-Clause / Apache-2.0",
         "加载本地权重并推理；权重不入库，提供带 SHA256 校验的一键下载脚本"],
        ["Gradio 6.28.0", "Apache-2.0", "6 页签界面：板块新闻 / 热点事件 / 搜索 / 证据链 / 简报生成 / 系统状态"],
    ], Inches(0.72), Inches(1.68), Inches(11.9), Inches(2.05),
        col_sizes=[3.1, 2.5, 6.3], size=13)
    bullets(s, [
        ("融合边界：", "AI 只承担「写」；聚类、剪枝、可信度、校验、降级全部团队自研，"
                     "换掉模型作品仍然成立（后端可切 local / online）。"),
        ("主动不用（说明不是拿来的）：", "feedparser、beautifulsoup4、simhash、scikit-learn、jieba "
                                  "均以自研实现替代，减少编译型依赖，冷启动可离线部署。"),
    ], top=Inches(4.05), size=16)
    notes(s,
          "【2:03–2:38｜35 秒】\n"
          "口播：AI 和开源的边界我们划得很清楚。开源模型只负责写这一段，其余全部自研，\n"
          "所以把模型换掉，作品照样成立。\n"
          "反向也说明一下：分词、相似度、去重这些库我们都主动没用，是自己实现的。\n"
          "许可方面：模型是 Apache-2.0，权重不分发，仓库里给下载脚本和 SHA256 校验。")

    # ========== 6 成品（实现 25 分）2:38–2:58，20s ==========
    s = slide_base(prs, "成品：6 个页签，全部可点可查",
                   "评分项 4/6 · 实现完成度与可验证效果（25 分）")
    if (shot / "3_brief.png").exists():
        add_picture_fit(s, shot / "3_brief.png",
                        Inches(0.72), Inches(1.72), Inches(5.85), Inches(4.6))
    if (shot / "2_evidence.png").exists():
        add_picture_fit(s, shot / "2_evidence.png",
                        Inches(6.72), Inches(1.72), Inches(5.85), Inches(4.6))
    textbox(s, Inches(0.72), Inches(6.45), Inches(11.9), Inches(0.8), [
        ("左：简报生成——按机构数与时间窗选事件，生成后逐句给出依据　右：证据链——7 篇报道 2 个站点，只算 1 家独立机构",
         15, False, GREY)], spacing=1.2)
    notes(s,
          "【2:38–2:58｜20 秒】\n"
          "口播：左边是简报生成页，先按机构数和时间窗筛出事件，生成后系统会把每一句的依据标出来；\n"
          "右边是证据链，这个事件有 7 篇报道、来自 2 个站点，但只算 1 家独立机构，\n"
          "每条报道都能点回具体的原文。截图取自 2026-11-18 真机运行。\n"
          "界面一共 6 个页签，都可以现场操作。")

    # ========== 7 实测数据 2:58–3:28，30s ==========
    s = slide_base(prs, "实测数据（全部可复现）",
                   "评分项 4/6 · 实现完成度与可验证效果（25 分）· 续")
    add_table(s, [
        ["指标", "实测值"],
        ["采集规模（2026-10-27 深夜）", "3,707 篇报道 / 2,551 个事件"],
        ["剪枝后需比对的候选对", "2.65%（全量两两比对的占比）"],
        ["多机构印证", "18 / 2,551 = 0.7%；另有 234 个事件有 ≥2 站点但仅单机构"],
        ["简报带依据句比例", "7/7 = 100%（阈值 80%）"],
        ["简报长度", "217 字（素材 807 字 → 自适应目标 274 字）"],
        ["生成 / 含校验耗时", "12.2 秒 / 34.0 秒（RTX 5060 Laptop 8GB，4-bit）"],
    ], Inches(0.72), Inches(1.70), Inches(11.9), Inches(3.15),
        col_sizes=[4.0, 7.9], size=14)
    bullets(s, [
        ("缺陷驱动的改进：", "跑真实数据过程中暴露并修掉 8 个缺陷，包括阈值未校准、"
                        "并列时按名称排序造成的隐性偏见、降级路径未触发等（技术报告 §4.5.5）。"),
    ], top=Inches(5.20), size=16)
    notes(s,
          "【2:58–3:28｜30 秒】\n"
          "口播：这些数字都能复现。采集到 3,707 篇、2,551 个事件；剪枝后只比对 2.65% 的候选对；\n"
          "简报 7 句全部有原文依据，217 字，生成 12 秒、含校验 34 秒。\n"
          "更重要的是过程：跑真实数据时暴露出 8 个缺陷，我们逐个修掉并留了记录，\n"
          "比如并列时按名称排序会让某个机构永远排前面，这是一个隐性偏见。")

    # ========== 8 开放成果（15 分）3:28–3:53，25s ==========
    s = slide_base(prs, "开放成果与复用价值",
                   "评分项 5/6 · 开放成果与复用价值（15 分）")
    bullets(s, [
        ("已公开：", "GitHub 仓库 newswire-lab/AIC-NewsBrief，MIT 许可，106 个文件——"
                  "源码、技术报告、演示视频、真机截图、三轮对照实验原始输出全部在库内。"),
        ("可直接复用：", "parsers / dedup / similarity 三个模块纯标准库实现，可单独摘出来用于其他中文文本项目；"
                    "新词发现模块不依赖词典，可作为独立工具使用。"),
        ("复现成本：", "本地 8 GB 显存可跑通（4-bit 量化后模型占 1.92 GB）；"
                  "权重由使用者按官方渠道获取，仓库不参与再分发。"),
        ("维护计划：", "源清单与调参参数集中在 config/ 下，已参数化；"
                  "后续计划向上游相关开源项目提交 Issue 与改进补丁。"),
    ], size=17)
    notes(s,
          "【3:28–3:53｜25 秒】\n"
          "口播：成果已经全部公开，MIT 许可，106 个文件，包括对照实验的原始输出，\n"
          "评审可以直接核对。\n"
          "复用上，解析、去重、相似度这三个模块是纯标准库实现，可以单独拿走用。\n"
          "维护上，配置都参数化了，后面会向上游项目提 Issue 和补丁。")

    # ========== 9 作品演示 3:53–4:23，30s ==========
    s = dark_slide(prs)
    textbox(s, Inches(1.0), Inches(2.35), Inches(11.3), Inches(1.0),
            [("作品演示（约 30 秒录屏）", 40, True, WHITE)], align=PP_ALIGN.CENTER)
    line(s, Inches(5.55), Inches(3.55), Inches(2.2))
    textbox(s, Inches(1.0), Inches(3.85), Inches(11.3), Inches(1.8), [
        ("板块新闻 → 热点事件 → 证据链 → 简报生成", 21, False, ACCENT),
        ("录屏已嵌入本页，录制时直接播放，无需现场操作", 15, False, soft),
    ], align=PP_ALIGN.CENTER, spacing=1.6)
    notes(s,
          "【3:53–4:23｜30 秒】\n"
          "口播（播放前）：「下面用三十秒演示核心流程。」\n"
          "—— 播放本页嵌入的录屏，不要现场敲命令，避免环境意外 ——\n"
          "播放结束后接：「演示到这里，回到结论。」\n"
          "※ 正式录制前请把录屏插入本页（插入 → 视频 → 此设备），并设为自动播放。")

    # ========== 10 局限与后续（材料 5 分）4:23–4:50，27s ==========
    s = slide_base(prs, "局限与后续计划（如实说明）",
                   "评分项 6/6 · 材料规范与表达（5 分）")
    bullets(s, [
        ("局限一：", "跨机构印证的样本本身就小——0.7%，只够说明口径有效，不足以做统计结论。"),
        ("局限二：", "语料以英文防务类为主，中文源多为短摘要，模型直出通过率明显偏低。"),
        ("局限三：", "端侧 3B 模型有长度与稳定性上限，长素材仍需降级到抽取式。"),
        ("后续：", "事件时间线、移动端适配、开放式评测集，以及向上游项目提交补丁。"),
        ("材料：", "技术报告、佐证材料、开源清单、演示视频、公开仓库均已在报名系统提交，全部可核验。"),
    ], size=17)
    notes(s,
          "【4:23–4:50｜27 秒】\n"
          "口播：最后如实说三个局限。第一，跨机构印证只有 0.7%，它能说明口径有效，但不足以支撑统计结论。\n"
          "第二，语料偏英文防务类，中文源摘要太短，模型直出通过率低。第三，端侧 3B 有上限，长素材还得降级。\n"
          "后续做时间线、移动端和开放式评测集。汇报完毕，谢谢评委老师。\n"
          "★ 总时长控制在 4 分 50 秒，不要超 5 分钟。")

    prs.save(str(out_path))
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist" / "AIC-NewsBrief-答辩-国赛.pptx"))
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    build(out)
    n = len(Presentation(str(out)).slides)
    size = out.stat().st_size / 1024
    print("[OK] %s" % out)
    print("     页数 %d ｜ 体积 %.0f KB ｜ 目标时长 4:50" % (n, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
