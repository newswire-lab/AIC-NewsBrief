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

版式采用「双轨」：
  1) 右上角四段导航（① 背景 ② 创新点 ③ 作品介绍 ④ 展望）——讲一条连贯的故事线；
  2) 每页 kicker 仍标「第几项 / 分值」——保证页面顺序与评分表一致，不漏项。
四段只是把相邻页「缝」成故事：页码顺序不变，评分项先后也就不变。
每页标题写成承接上页的句式，正文上方再给一句过渡句（bridge），避免跳话题。

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


PARTS = ["① 背景", "② 创新点", "③ 作品介绍", "④ 展望"]


def part_strip(s, cur, dark=False):
    """右上角四段导航，当前段高亮——让评委随时知道讲到第几部分。"""
    box = s.shapes.add_textbox(Inches(7.10), Inches(0.30), Inches(5.50), Inches(0.32))
    tf = box.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    off = RGBColor(0x7A, 0x81, 0x89) if dark else RGBColor(0xB4, 0xBA, 0xC2)
    sep = RGBColor(0x45, 0x4B, 0x52) if dark else RGBColor(0xDC, 0xE0, 0xE4)
    for i, name in enumerate(PARTS):
        r = p.add_run()
        r.text = name
        style(r, 12, i == cur, ACCENT if i == cur else off)
        if i < len(PARTS) - 1:
            r2 = p.add_run()
            r2.text = "  ›  "
            style(r2, 12, False, sep)
    return box


def slide_base(prs, title, kicker=None, part=None, bridge=None):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bar = s.shapes.add_shape(1, Emu(0), Emu(0), W, Inches(0.10))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()
    if part is not None:
        part_strip(s, part)
    top = Inches(0.30)
    if kicker:
        textbox(s, Inches(0.72), top, Inches(6.3), Inches(0.30),
                [(kicker, 12, True, ACCENT)])
        top = Inches(0.64)
    textbox(s, Inches(0.72), top, Inches(11.9), Inches(0.62),
            [(title, 26, True, INK)])
    if bridge:
        textbox(s, Inches(0.72), Inches(1.28), Inches(11.9), Inches(0.34),
                [("↳  " + bridge, 14, False, GREY)])
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
    textbox(s, Inches(0.95), Inches(3.78), Inches(11.2), Inches(2.4), [
        ("第八届全球校园人工智能算法精英大赛 · 算法主题赛 · AI+开源", 18, False, soft),
        (f"团队名称：{TEAM}", 20, True, WHITE),
        ("开源仓库：github.com/newswire-lab/AIC-NewsBrief（MIT）", 18, False, soft),
        ("本次汇报四个部分：① 背景　→　② 创新点　→　③ 作品介绍　→　④ 展望", 16, False, ACCENT),
    ], spacing=1.45)
    notes(s,
          "【0:00–0:20｜20 秒】\n"
          f"口播：「评委老师好，我们的团队名称是「{TEAM}」，作品是{PRODUCT}。\n"
          "我们不做又一个新闻 App，只回答一个可验证的问题——这条信息，有几家独立机构在说。」\n"
          "要点：开场先报团队名称（通知要求），然后一句定位，不要念 PPT 标题。")

    # ========== 2 问题与场景价值（15 分）0:20–0:55，35s ==========
    s = slide_base(prs, "乱象：十几家都在转，其实只有一家在说",
                   "① 背景 · 评分项 1/6 · 问题与场景价值（15 分）",
                   part=0, bridge="从最基础的一句话说起：这条新闻，到底有几家在说。")
    bullets(s, [
        ("现象：",
         "一条消息十几家媒体都在转，点开一看，标题不同、内容几乎一字不差——同一件事被抄了十几遍。"),
        ("数字：",
         "全库 2,551 个事件里，234 个有多个网站在转，但真正由两家以上独立机构报道的只有 18 个，"
         "占 0.7%——「多家报道」被系统性高估了。"),
        ("后果：",
         "读者以为「都报了」，其实只有一家在说；AI 再一写、算法再一推，越传越像真的。"),
        ("现有工具：",
         "要么只聚合不判断，要么只生成不溯源，都回答不了「到底几家在说」。"),
        ("我们的做法：",
         "不猜真假，只数机构——数的是机构，不是网站；每一句都能点回原文。"),
    ], size=16)
    notes(s,
          "【0:20–0:55｜35 秒】\n"
          "口播：先说个天天见的现象——一条消息十几家媒体都在转，点开一看，标题不同、内容几乎一字不差，\n"
          "就像同一个班交了十份作业，其实一个人抄了九遍。我们统计了全库 2,551 个事件，\n"
          "234 个有多个网站在转，但真正由两家以上独立机构报道的只有 18 个，占 0.7%。\n"
          "所以「多家报道」被系统性高估了。我们的做法很直接：不猜真假，只数机构——\n"
          "数机构不是数网站，每一句都要能点回原文。")

    # ========== 3 方案流水线（创新 20 分）0:55–1:25，30s ==========
    s = slide_base(prs, "一条能逐句校验的流水线",
                   "② 创新点 · 评分项 2/6 · 创新性与方案设计（20 分）",
                   part=1, bridge="问题清楚了，它怎么跑？")
    textbox(s, Inches(0.85), Inches(1.70), Inches(11.7), Inches(1.1), [
        ("采集 → 去重 → 归到同一件事 → 粗筛 → 数机构 → 生成 → 逐句核对 → 不过就退回原文",
         20, True, INK)], spacing=1.35)
    bullets(s, [
        ("采集：", "自己写抓取程序，不用第三方库；每 30 分钟跑一轮，存进本地数据库。"),
        ("去重：", "同一篇稿子被转了十遍，只留一遍。"),
        ("归类 + 粗筛：", "先按关键词粗筛出可能重复的稿子，只对这些逐对比，比对量压到 2.65%。"),
        ("数机构：", "数的是「几家机构」，不是「几个网站」；同一集团的站点只算一家。"),
        ("生成 + 核对：", "本地模型负责写；写完一句一句回原文找依据，找不到就退回原文摘录。"),
    ], top=Inches(2.95), size=17)
    notes(s,
          "【0:55–1:25｜30 秒】\n"
          "口播：问题清楚了，它怎么跑？八步：采集、去重、归到同一件事、粗筛、数机构、生成、\n"
          "逐句核对，核不过就退回原文。前面六步全部自己写，只把「写」这一步交给开源模型。\n"
          "目的只有一个：产物里每一句话都能指回具体原文。")

    # ========== 4 四个自研创新点 1:25–2:03，38s ==========
    s = slide_base(prs, "关键在这四处：每一处都堵上一步的漏洞",
                   "② 创新点 · 评分项 2/6 · 创新性与方案设计（20 分）· 续",
                   part=1, bridge="流水线搭起来了，哪几处是我们自己写的？")
    bullets(s, [
        ("① 只算该算的：",
         "先按关键词粗筛出可能重复的稿子，只对这些逐对比。全量两两比对有上百万对，实测只剩 2.65%。"),
        ("② 数机构，不数网站：",
         "Defense News、Army Times 这几家其实是同一个集团，我们表里它名下有 6 个站点。"
         "按网站算，一个事件会被吹成 6 家印证；按机构算，只算 1 家。"),
        ("③ 不装词典也能抓新词：",
         "人名、装备型号天天在变，词库里没有。不靠词典，看字片段出现得多不多，自己把新词找出来。"),
        ("④ 核不过就不硬发：",
         "写完逐句回原文找依据；找不到的句子能删就删，删完还不过就整篇退回原文摘录。"),
    ], size=17)
    notes(s,
          "【1:25–2:03｜38 秒】\n"
          "口播：四个自研点，我先说三处关键的。一是聚类加剪枝，只对可能重复的组合算相似度；\n"
          "二是机构级去重，这条最影响结论——防务新闻那五家其实是同一个出版集团，\n"
          "按站点算会被虚报成 6 家印证，按机构算只计 1 家。三是无词典新词发现。\n"
          "第四处是校验加降级，是最后一道兜底：核不过就不硬发，退回原文摘录。")

    # ========== 5 AI 与开源融合（20 分）2:03–2:38，35s ==========
    s = slide_base(prs, "边界：AI 只负责「写」，其余全部自研",
                   "② 创新点 · 评分项 3/6 · AI 与开源融合（20 分）",
                   part=1, bridge="自己写的那几处说完了，开源模型用在哪？")
    add_table(s, [
        ["开源资源", "许可证", "在本作品中的作用"],
        ["Qwen2.5-3B-Instruct", "Apache-2.0",
         "只负责「写」：起短标题、写约 300 字简报；在自己电脑上跑，占约 1.92 GB 显存，全程不联网"],
        ["PyTorch 2.11.0 / transformers 5.17.0", "BSD-3-Clause / Apache-2.0",
         "把模型加载起来跑推理；模型文件不进仓库，给下载脚本和校验值"],
        ["Gradio 6.28.0", "Apache-2.0", "做界面：六个页签，在浏览器里直接点"],
    ], Inches(0.72), Inches(1.68), Inches(11.9), Inches(2.05),
        col_sizes=[3.1, 2.5, 6.3], size=13)
    bullets(s, [
        ("分工：", "模型只负责「写」，其余全部自己实现；把模型换掉，作品照样成立。"),
        ("反过来说：", "解析、去重、相似度这些现成的库，我们主动没用，都是自己写的——"
                   "依赖少，装好就能离线跑。"),
    ], top=Inches(4.05), size=16)
    notes(s,
          "【2:03–2:38｜35 秒】\n"
          "口播：AI 和开源的边界我们划得很清楚。开源模型只负责写这一段，其余全部自研，\n"
          "所以把模型换掉，作品照样成立。\n"
          "反向也说明一下：分词、相似度、去重这些库我们都主动没用，是自己实现的。\n"
          "许可方面：模型是 Apache-2.0，权重不分发，仓库里给下载脚本和 SHA256 校验。")

    # ========== 6 成品（实现 25 分）2:38–2:58，20s ==========
    s = slide_base(prs, "成品：六个页签，全部可点可查",
                   "③ 作品介绍 · 评分项 4/6 · 实现完成度与可验证效果（25 分）",
                   part=2, bridge="三处不同说完了，看它实际的样子。")
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
          "口播：三处不同说完了，看它实际的样子。左边是简报生成，先按机构数和时间窗筛出事件，\n"
          "生成后每一句都标出依据；右边是证据链，这个事件有 7 篇报道、来自 2 个站点，\n"
          "每条报道都能点回具体的原文。截图取自 2026-11-18 真机运行。\n"
          "界面一共 6 个页签，都可以现场操作。")

    # ========== 7 实测数据 2:58–3:28，30s ==========
    s = slide_base(prs, "效果：这些数字都能复现",
                   "③ 作品介绍 · 评分项 4/6 · 实现完成度与可验证效果（25 分）· 续",
                   part=2, bridge="界面能点，里面的数字经不经得起查？")
    add_table(s, [
        ["指标", "实测值"],
        ["采集规模（2026-10-27 深夜）", "3,707 篇 / 2,551 个事件（答辩当日已增至 4,298 / 3,038）"],
        ["剪枝后需要比对的组合", "只剩 2.65%（其余直接跳过）"],
        ["多机构印证", "18 / 2,551 = 0.7%（234 个事件有多个站点，但只有一家机构）"],
        ["每句都有原文依据", "7/7 = 100%（门槛 80%）"],
        ["简报长度", "217 字（素材 807 字，目标 274 字）"],
        ["生成 / 含核对耗时", "12.2 秒 / 34.0 秒（8 GB 显存的笔记本）"],
    ], Inches(0.72), Inches(1.70), Inches(11.9), Inches(3.15),
        col_sizes=[4.0, 7.9], size=14)
    bullets(s, [
        ("更值钱的是过程：", "跑真实数据时暴露并修掉 8 个缺陷——比如分区同分时按名称排序，"
                        "政治永远压过民生，疫苗接种的稿子被判成了政治。"),
    ], top=Inches(5.20), size=16)
    notes(s,
          "【2:58–3:28｜30 秒】\n"
          "口播：这些数字都能复现。采集到 3,707 篇、2,551 个事件；需要两两比对的组合只剩 2.65%；\n"
          "简报 7 句全部有原文依据，217 字，生成 12 秒、含核对 34 秒。\n"
          "更重要的是过程：跑真实数据时暴露出 8 个缺陷，我们逐个修掉并留了记录，\n"
          "比如并列时按名称排序会让某个机构永远排前面，这是一个隐性偏见。")

    # ========== 8 开放成果（15 分）3:28–3:53，25s ==========
    s = slide_base(prs, "开放：能拿走，也能复现",
                   "③ 作品介绍 · 评分项 5/6 · 开放成果与复用价值（15 分）",
                   part=2, bridge="数字站得住，放出来了什么？")
    bullets(s, [
        ("已经公开：", "GitHub 上 MIT 许可，110 个文件——源码、报告、视频、截图、"
                  "三轮对照实验的原始输出，都在库里。"),
        ("能单独拿走用：", "抓取、去重、相似度这三个模块只用标准库，摘出来就能放进别的项目。"),
        ("跑起来要多少：", "8 GB 显存的笔记本就能跑；模型文件不进仓库，按官方渠道自己下。"),
        ("后面怎么维护：", "数据源和参数都在 config/ 里，改配置就行；会向上游项目提 Issue 和补丁。"),
    ], size=17)
    notes(s,
          "【3:28–3:53｜25 秒】\n"
          "口播：成果已经全部公开，MIT 许可，110 个文件，包括对照实验的原始输出，\n"
          "评审可以直接核对。\n"
          "复用上，解析、去重、相似度这三个模块是纯标准库实现，可以单独拿走用。\n"
          "维护上，配置都参数化了，后面会向上游项目提 Issue 和补丁。")

    # ========== 9 作品演示 3:53–4:23，30s ==========
    s = dark_slide(prs)
    part_strip(s, 2, dark=True)
    textbox(s, Inches(1.0), Inches(2.35), Inches(11.3), Inches(1.0),
            [("作品演示（约 30 秒录屏）", 40, True, WHITE)], align=PP_ALIGN.CENTER)
    line(s, Inches(5.55), Inches(3.55), Inches(2.2))
    textbox(s, Inches(1.0), Inches(3.82), Inches(11.3), Inches(2.0), [
        ("↳  下面用三十秒看它跑一遍。", 16, False, soft),
        ("板块新闻 → 热点事件 → 证据链 → 简报生成", 21, False, ACCENT),
        ("录屏已嵌入本页，录制时直接播放，无需现场操作", 15, False, soft),
    ], align=PP_ALIGN.CENTER, spacing=1.5)
    notes(s,
          "【3:53–4:23｜30 秒】\n"
          "口播（播放前）：「下面用三十秒演示核心流程。」\n"
          "—— 播放本页嵌入的录屏，不要现场敲命令，避免环境意外 ——\n"
          "播放结束后接：「演示到这里，回到结论。」\n"
          "※ 正式录制前请把录屏插入本页（插入 → 视频 → 此设备），并设为自动播放。")

    # ========== 10 局限与后续（材料 5 分）4:23–4:50，27s ==========
    s = slide_base(prs, "局限与后续：如实说明",
                   "④ 展望 · 评分项 6/6 · 材料规范与表达（5 分）",
                   part=3, bridge="讲完了做成了什么，再说还没做成的。")
    bullets(s, [
        ("局限一：", "能印证的事件本身很少——100 个里不到 1 个。因此只能说「这么数是对的」，不敢拿它下统计结论。"),
        ("局限二：", "语料偏英文防务类；中文源多是短摘要，模型直接写容易不过。"),
        ("局限三：", "本地 30 亿参数的模型有上限，素材一长还是得退回原文摘录。"),
        ("后续：", "做事件时间线、手机端适配，再整理一套公开的测试集。"),
        ("材料：", "报告、佐证材料、开源清单、演示视频和仓库都已提交，可以逐项核对。"),
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
