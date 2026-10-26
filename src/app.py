"""Gradio Web 界面：事件簇浏览、证据链查看、简报生成。

三个页签对应三条用户价值主线：

1. **热点事件** —— 按"印证机构数"排序，一眼看出哪些事件是多机构在说；
2. **证据链** —— 展开某个事件的可信度构成（三个因子的实际取值）与全部来源；
3. **简报生成** —— 调用开源模型生成简报，并实时展示**事实一致性校验报告**。

启动：
    python -m src.app                     默认开启后台自动采集（每 30 分钟一轮）
    python -m src.app --port 7860 --share
    python -m src.app --interval 10       改采集周期
    python -m src.app --no-auto-collect   关掉后台采集（只保留界面自动刷新）

关于"自动"的两层含义（两个独立机制，频率不同，不要混）：

- **数据自动更新**：`src.scheduler.AutoUpdater` 在守护线程里按周期跑
  「采集 -> 聚类 -> 打分」，保证库里始终有新鲜数据。默认 30 分钟一轮；
  启动时若数据已超过 24 小时，会立刻补跑一轮，避免一打开界面就空空如也。
- **界面自动刷新**：`gr.Timer` 定时重读数据库并重绘。默认 60 秒一次，
  只读不写，代价极低，用户全程无需点任何按钮。

两者独立：即使关掉后台采集，界面照样自动刷新（只是数据不会变新）。
"""

from __future__ import annotations

import argparse
import re
import os
import sys
import time
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr

from . import brief as brief_mod
from . import cluster, config, credibility, llm, scheduler, store, topics, verify
from . import search as search_mod
from .models import utcnow

PROJECT_ROOT = config.PROJECT_ROOT

RULE = "─" * 60

EVENT_HEADERS = [
    "事件ID", "可信度", "分区", "事件关键词",
    "信息来源（独立机构）", "机构数", "篇数",
    "事件发生时间", "最新跟进", "事件标题",
]

# 「时间窗口」选项：显示名 -> 小时数（0 表示不限）
HOURS_CHOICES = [
    ("最近 24 小时", 24),
    ("最近 48 小时", 48),
    ("最近 72 小时", 72),
    ("最近 7 天", 168),
    ("不限（全部历史）", 0),
]
DEFAULT_HOURS = 24

# 「分区」筛选。分区由**自研**分类器判定（src/topics.py，关键词加权、全库秒级、
# 同一份数据结果逐字节一致），选项从库内实际分布生成而不写死 ——
# 否则将来新增分区时下拉框会与数据脱节。
TOPIC_ANY = "全部分区"
TOPIC_ORDER = ("政治", "军事", "经济", "科技", "文化", "体育",
               "民生", "国际", "法治", "社会", "历史", "其他")


def topic_choices() -> List[Tuple[str, str]]:
    """分区下拉选项：第一项「全部分区」，其余按固定顺序 + 库内实际值。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        names = [n for n, _ in topics.stats(conn)]
    except Exception:
        names = []
    finally:
        conn.close()
    ordered = [n for n in TOPIC_ORDER if n in names]
    ordered += [n for n in names if n not in TOPIC_ORDER]
    return [(TOPIC_ANY, TOPIC_ANY)] + [(n, n) for n in ordered]

# 检索结果表头。标题列放的是「优先中文译名」，另开一列放原文标题，
# 这样用户既能快速读懂，也能一眼确认译文对应的原句。
SEARCH_HEADERS = [
    "相关度", "来源机构", "来源站点", "分区", "时间", "标题（外文自动译中文）",
    "原文标题", "命中片段", "可信度", "链接", "事件ID",
]

# 「板块（频道）」：参照新闻客户端的频道栏。
# 值 = 分区名，空串表示「要闻（全部）」。分区由 src/topics.py 自研分类器判定。
CHANNEL_CHOICES = [
    ("要闻（全部）", ""),
    ("政治", "政治"), ("军事", "军事"), ("经济", "经济"), ("民生", "民生"),
    ("文化", "文化"), ("体育", "体育"), ("科技", "科技"), ("国际", "国际"),
    ("法治", "法治"), ("社会", "社会"), ("历史", "历史"), ("其他", "其他"),
]
# 板块信息流的列：按新闻客户端的读法排 —— 时间在前、标题最宽、来源在后
# 标签 -> 值 反查表：防「界面传标签、查询要值」这类错配（见 ui_channel 注释）
_CHANNEL_LABEL_TO_VALUE = {label: value for label, value in CHANNEL_CHOICES}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(text: str) -> bool:
    """文本里是否含汉字（用来判断"这是不是已经是中文标题"）。"""
    return bool(_CJK_RE.search(text or ""))


def _short_text(text: str, limit: int) -> str:
    """按字数截断并以省略号收尾（界面列表用）。

    注意不要借用 :mod:`src.brief` 的 `_clip` —— 那是私有函数且 app.py 没有导入，
    曾在界面上直接抛 `name '_clip' is not defined`（2026-10-20 实测）。
    """
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def channel_title(orig: str, zh: str) -> str:
    """把一条新闻的标题拼成列表里显示的形式。

    用户反馈「有的还是英文显示，英文后面加中文方便一点」（2026-10-20）——
    实测 454 篇英文标题没有译文，列表里只剩英文。这里按三种情况处理：

    - 原文已是中文 → 直接用原文（译文要么为空、要么与原文重复）
    - 外文且有译文 → **外文原文 ｜ 中文译文**，对照着读
    - 外文且无译文 → 只能显示原文；这类要靠 ``scripts/warm_translations.py`` 补
    """
    orig = (orig or "").strip()
    zh = (zh or "").strip()
    if zh and zh != orig and not _has_cjk(orig):
        return f"{_short_text(orig, 52)}｜{_short_text(zh, 44)}"
    return orig or zh


CHANNEL_HEADERS = [
    "时间", "标题（外文自动译中文）", "来源机构", "来源站点", "分区", "可信度", "链接",
]

# 界面自动刷新间隔（秒）。只重读数据库，不触发采集，代价极低。
LIST_REFRESH_SECONDS = 60      # 热点事件表 + 新鲜度提示
CHOICES_REFRESH_SECONDS = 300  # 事件下拉框（会保留已选中项）
STATS_REFRESH_SECONDS = 120    # 系统状态

# 后台采集线程句柄，由 main() 装配；None 表示未启用自动采集
_UPDATER: Optional[scheduler.AutoUpdater] = None


def _age_text(ts: Optional[str]) -> str:
    """把一个 UTC ISO 时间戳渲染成"3.2 小时前"这类人话。"""
    if not ts:
        return "时间未知"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return "时间未知"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hours = (utcnow() - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
    return _fmt_age(max(0.0, hours))


def _fmt_age(hours: Optional[float]) -> str:
    """把"距今多少小时"渲染成人话。None 表示时间无法判定。"""
    if hours is None:
        return "时间未知"
    if hours < 1.0 / 60:
        return "刚刚"
    if hours < 1:
        return f"{max(1, int(hours * 60))} 分钟前"
    if hours < 48:
        return f"{hours:.1f} 小时前"
    return f"{hours / 24:.1f} 天前"


def _window_text(hours: Optional[float]) -> str:
    """把时窗数值渲染成中文描述。0/None 表示不限。"""
    h = int(hours or 0)
    if h <= 0:
        return "全部历史"
    if h < 48:
        return f"最近 {h} 小时"
    return f"最近 {h // 24} 天"


def _fmt_orgs(raw: Optional[str], keep: int = 3) -> str:
    """把 SQL 里 GROUP_CONCAT 出来的机构名整理成表格列。

    多于 keep 家时只列前 keep 家并标出总数，避免撑破列宽；
    完整名单在「证据链」页签里。
    """
    names = [x.strip() for x in (raw or "").split(",") if x.strip()]
    if not names:
        return "—"
    if len(names) <= keep:
        return "、".join(names)
    return "、".join(names[:keep]) + f" 等 {len(names)} 家"


# --------------------------------------------------------------------------
# 数据访问
# --------------------------------------------------------------------------

def _local_time(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%m-%d %H:%M")


def fetch_event_rows(min_orgs: int, limit: int,
                     hours: int = DEFAULT_HOURS,
                     topic: str = TOPIC_ANY) -> List[List[Any]]:
    conn = store.connect()
    store.init_db(conn)
    # 按分区筛选时先多取再过滤：LIMIT 在最外层，若只取 limit 条再筛，
    # 某个分区可能一条不剩，表格看起来像"没数据"。
    want = int(limit) * 6 if (topic and topic != TOPIC_ANY) else int(limit)
    rows = cluster.top_events(conn, limit=want, min_orgs=int(min_orgs),
                             hours=(int(hours) or None))
    out = []
    for row in rows:
        # 直接取 top_events 已经算好的分数。
        # 早期对每一行再调一次 credibility.explain() —— 那是一次整簇 SELECT + 重算，
        # 而本列表是每 60 秒刷新的，默认 15 行就多出 15 次查询。
        # 实测 row["credibility"] 与 explain() 的返回值完全一致（53.37 vs 53.37）。
        row_topic = row["topic"] or "其他"

        if topic and topic != TOPIC_ANY and row_topic != topic:

            continue

        if len(out) >= int(limit):

            break

        score = float(row["credibility"] or 0.0)
        out.append([
        row["event_id"],
        round(score, 1),
        row_topic,
            row["keywords"] or "—",
            _fmt_orgs(row["orgs"]),
            row["n_orgs"],
            row["n_articles"],
            f"{_local_time(row['earliest'])}（{_age_text(row['earliest'])}）",
            _local_time(row["latest"]),
            (row["display_title"] or row["sample_title"] or "")[:52],
        ])
    return out


def event_choices(min_orgs: int, limit: int,
                  hours: int = DEFAULT_HOURS) -> List[Tuple[str, str]]:
    conn = store.connect()
    store.init_db(conn)
    rows = cluster.top_events(conn, limit=int(limit), min_orgs=int(min_orgs),
                             hours=(int(hours) or None))
    out = []
    for row in rows:
        # 同上：不再逐行调 credibility.explain()。
        # 下拉框以**事件标题为主**（输入关键词可直接过滤），元信息收在方括号里。
        # 早期版本把元信息放在前面，用户扫列表时先看到分值/时窗，反而找不到是哪件事。
        title = (row["display_title"] or row["sample_title"] or "")[:56]
        label = (f"{title}   [{float(row['credibility'] or 0):.0f}分·{row['n_orgs']}机构·"
                 f"{_age_text(row['earliest'])}]")
        out.append((label, row["event_id"]))
    return out


def pick_event(min_orgs_value: int, hours_value: int = DEFAULT_HOURS):
    """构造「事件下拉框」的更新对象：预填选项并默认选中第一个。

    下拉框的 choices 必须在回调里通过 ``gr.update`` 下发。早期版本在 lambda 里
    把 ``event_choices()`` 写了两遍（一次取 choices、一次取默认值），造成同一份数据
    查两遍；这里统一为查一次、复用结果。

    返回 ``gr.update`` 而不是 ``(choices, value)``，是为了让调用方既能用在
    ``button.click`` 里，也能用在 ``demo.load`` 里。
    """
    choices = event_choices(int(min_orgs_value), 30, int(hours_value or 0))
    return gr.update(choices=choices, value=(choices[0][1] if choices else None))


# --------------------------------------------------------------------------
# 界面逻辑
# --------------------------------------------------------------------------

def ui_refresh_events(min_orgs: int, limit: int, hours: int = DEFAULT_HOURS,
                      topic: str = TOPIC_ANY):
    """重绘「热点事件」表 + 摘要 + 新鲜度提示。

    三者放在同一个回调里返回，这样定时器一次 tick 只打一遍数据库。
    """
    rows = fetch_event_rows(min_orgs, limit, hours, topic)
    window = _window_text(hours)
    if rows:
        # 说明判定口径：时窗按**最新跟进**判定，所以"事件发生时间"可能早于窗口起点。
        # 不写这句话，用户看到「最近 24 小时」里躺着一条 25 小时前发生的事件会以为是 bug。
        if int(hours or 0) <= 0:
            scope = "时窗：全部历史（不限时窗）"
        else:
            scope = f"时窗：{window}内仍有新报道（按最新跟进判定）"
        summary = (
            f"共 **{len(rows)}** 个事件｜{scope}｜"
            f"筛选：≥{int(min_orgs)} 家独立机构印证"
            + (f"｜分区：**{topic}**" if topic and topic != TOPIC_ANY else "")
        )
    else:
        summary = (
            f"**{window}内暂无满足条件的事件。**\n\n"
            f"- 后台会按周期自动采集，新事件无需手动刷新即会出现在本页（见上方状态行）；\n"
            f"- 若想先浏览历史数据，把上方「时间窗口」改成「不限（全部历史）」。"
        )
    return rows, summary, ui_auto_status()


# 最近一次各页签下拉框的筛选参数。
#
# **为什么需要**：`gr.Timer` 在页面加载瞬间会以 `data=[]` 触发一次，
# Gradio 在**预处理阶段**就直接抛
#     ValueError: An event handler (ui_refresh_choices) didn't receive
#     enough input values (needed: 3, got: 0)
# 请求根本进不到回调。因此定时器一律**不带输入组件**，
# 改用「服务端记住最近一次参数」。定时刷新本来也不需要精确到当前帧的参数。
#
# 这是模块级状态、被所有会话共享 —— 可以接受：这两个下拉框的参数本来就是
# 全局的浏览偏好，不是某个用户私有的东西。
_LAST_CHOICES: Dict[str, Tuple[int, int, Optional[str]]] = {
    "evidence": (1, DEFAULT_HOURS, None),
    "generate": (1, 168, None),   # 简报页默认 7 天（与 gen_hours 的默认值保持一致）
}


def ui_refresh_choices(min_orgs: int, hours: int, current: Optional[str],
                       key: str = "") -> Any:
    """刷新事件下拉框，并**保留用户当前选中的事件**。

    这一点很关键：下拉框一旦被重置为第一项，``change`` 就会触发，
    用户正在看的证据链/简报会被无声地换掉。所以只要原选项还在候选里就不动它。
    """
    # 记下本次参数，供无输入的定时刷新复用
    if key:
        _LAST_CHOICES[key] = (int(min_orgs or 1), int(hours or 0), current)
    choices = event_choices(int(min_orgs), 30, int(hours or 0))
    ids = [c[1] for c in choices]
    value = current if current in ids else (ids[0] if ids else None)
    return gr.update(choices=choices, value=value)


def ui_collect_now() -> str:
    """「立即采集一轮」按钮：唤醒后台线程，不阻塞界面。

    数据已经很新鲜时直接拒绝 —— 既省源站流量，也把"按钮被重复触发"
    变成无害的空操作（实测 Gradio 前端在 SSE 流反复中断后会重放 click 事件，
    曾因此在我没点任何按钮的情况下触发过一次真实采集）。
    """
    if _UPDATER is None:
        return ("⚠️ 后台自动采集未启用（启动时带了 `--no-auto-collect`）。"
                "手动更新请运行：`python -m src.cli pipeline`")
    if _UPDATER.running:
        return "🔄 后台正在采集，请等这一轮跑完。"
    st = _UPDATER.status()
    age = st.get("data_age_hours")
    if not _UPDATER.can_trigger() and age is not None:
        return (f"✅ 库内数据 {_fmt_age(age)}，还很新鲜，现在无需采集"
                f"（后台每 {st['interval_minutes']:g} 分钟自动跑一轮）。")
    if not _UPDATER.trigger():
        return "⚠️ 后台采集线程未在运行，或距上次采集过近。"
    return "✅ 已请求后台立即采集一轮；完成后本页列表会自动出现新事件。"


def ui_auto_status() -> str:
    """渲染「数据新鲜度 + 后台采集状态」，供定时器高频刷新。"""
    if _UPDATER is None:
        latest = scheduler.AutoUpdater.latest_data_time()
        age = scheduler.AutoUpdater.data_age_hours(latest)
        return (f"⚠️ **后台自动采集未启用**（`--no-auto-collect`）｜ "
                f"库内最新数据 {_local_time(latest)}（{_fmt_age(age)}）｜ "
                f"手动更新：`python -m src.cli pipeline`")

    st = _UPDATER.status()
    if st["running"]:
        head = "🔄 **正在后台采集…**"
    elif st.get("data_stale"):
        head = "⚠️ **数据已过期**，等待下一轮采集"
    else:
        head = "✅ **数据新鲜**"
    bits = [
        head,
        f"库内最新数据 {_local_time(st.get('latest_data_time'))}"
        f"（{_fmt_age(st.get('data_age_hours'))}）",
        f"采集周期 {st['interval_minutes']:g} 分钟"
        f"（已跑 {st['runs']} 轮，失败 {st['fails']} 次）",
    ]
    if st.get("next_run"):
        bits.append(f"下次采集 {_local_time(st['next_run'])}")
    if st.get("last_finish"):
        bits.append(f"上轮{'成功' if st.get('last_ok') else '失败'}"
                    f" {_local_time(st['last_finish'])}（{st.get('last_elapsed')}s）")
    if st.get("last_error"):
        bits.append(f"错误：`{st['last_error']}`")
    return " ｜ ".join(bits)


def ui_refresh_choices_tick(key: str) -> Any:
    """定时刷新用的**无输入**入口（见 _LAST_CHOICES 的说明）。"""
    mo, hh, cur = _LAST_CHOICES.get(key) or (1, DEFAULT_HOURS, None)
    return ui_refresh_choices(mo, hh, cur, key=key)



def ui_evidence(event_id: Optional[str]) -> str:
    if not event_id:
        return ("_请先在上方选择一个事件。_\n\n"
                "> 下拉框为空时，说明当前「最少印证机构数 + 时间窗口」组合下没有事件。\n"
                "> 多机构印证的事件很稀少（全库 1184 个里只有 9 个），"
                "把机构数调到 1 或把时间窗口放宽到 7 天即可。")
    conn = store.connect()
    store.init_db(conn)
    info = credibility.explain(conn, event_id)
    if not info:
        return "_事件不存在。_"

    f = info["factors"]
    lines = [
        f"### 事件 `{event_id}`",
        "",
        f"**可信度：{info['credibility']} 分** ｜ `{info['label']}`",
        "",
        "#### 评分构成",
        "",
        "| 因子 | 取值 | 含义 |",
        "|---|---|---|",
        f"| `f_tier` | **{f['f_tier']}** | 簇内最高来源等级 tier{f['best_tier']}"
        f"（tier1 官方=1.0 / tier2 主流=0.7） |",
        f"| `f_time` | **{f['f_time']}** | 距最新报道 {f['age_hours']} 小时，"
        f"按 `exp(-age/72)` 衰减 |",
        f"| `f_cross` | **{f['f_cross']}** | {f['n_orgs']} 家**独立机构**印证，"
        f"按 `1-0.4/k` 计算（单一机构封顶 0.60） |",
        "",
        f"> 可信度 = 100 × {f['f_tier']} × {f['f_time']} × {f['f_cross']}"
        f" = **{info['credibility']}**",
        "",
        f"#### 印证机构（{info['n_orgs']} 家）",
        "",
        "、".join(info["orgs"]) or "—",
        "",
        f"#### 全部报道（{info['n_articles']} 篇，{info['n_sources']} 个站点）",
        "",
    ]
    for s in info["sources"]:
        # 有中文译名时以译名为主、原文附在下面：外文标题直接摆给中文用户看没有意义，
        # 但完全丢掉原文又会让人无法核对来源，所以两行都给。
        if (s.get("title_zh") or "").strip():
            head_lines = [f"- **{s['title_zh']}**", f"  - 原文标题：{s['title']}"]
        else:
            head_lines = [f"- {s['title']}"]
        lines += head_lines + [
            f"- 原文：{s['url']}",
            "",
        ]
    return "\n".join(lines)


def _expansion_text(groups: List[List[str]]) -> str:
    """把关联词扩展结果渲染成人读的一行，让用户知道“搜了什么”。"""
    expanded = [g for g in groups if len(g) > 1]
    if not expanded:
        return "（无可用关联词，按字面检索）"
    return "🔎 已自动扩展关联词：" + "；".join(
        "／".join(f"`{w}`" for w in g[:9]) for g in expanded)


def ui_channel(channel: str, kw: str, hours: int, limit: int):
    """板块浏览：点板块看该类的**全部新闻**，板块内还能再搜索。

    - 无关键词：按时间倒序铺信息流（像新闻客户端的频道页），走 store.recent_articles；
    - 有关键词：复用同一套检索（相关词扩展、同稿折叠、译名优先），
      但把分区**下推到 SQL** —— 这就是「板块内搜索」。

    两类数据源的返回结构不同（前者是 sqlite.Row，后者是 dict），
    这里统一成同一套展示列，避免界面上出现两种表格。
    """
    conn = store.connect()
    store.init_db(conn)
    # 界面可能把 Radio 的**标签**（"要闻（全部）"）传进来，而不是它的**值**（""）。
    # 直接拿去查库会静默返回 0 行 —— 首屏就是空的（2026-10-20 实测复现）。
    # 这里统一归一化：标签 -> 值；TOPIC_ANY（"全部分区"）也视作"全部"。
    raw_channel = channel
    if isinstance(raw_channel, (tuple, list)) and raw_channel:
        # Gradio 少数路径会把 (标签, 值) 原样传进来
        raw_channel = raw_channel[1] if len(raw_channel) > 1 else raw_channel[0]
    raw_channel = str(raw_channel or "").strip()
    topic = _CHANNEL_LABEL_TO_VALUE.get(raw_channel, raw_channel)
    unknown = ""
    if topic and topic != TOPIC_ANY:
        known = {name for name, _ in topics.stats(conn)}
        if topic not in known:
            # 认不出的分区名 —— 绝不能静默返回 0 行把首屏搞空（实测踩过）。
            # 退回「全部」，并把这件事写进说明，别让用户以为真没有新闻。
            unknown = topic
            topic = ""
    else:
        topic = ""
    kw = (kw or "").strip()
    label = topic or "要闻（全部）"
    out: List[List[Any]] = []

    if kw:
        hits = search_mod.search_articles(conn, kw, hours=int(hours or 0),
                                          limit=int(limit), topic=topic)
        for r in hits:
            out.append([
                _local_time(r["ts"]),
                channel_title(r.get("title"), r.get("title_zh")),
                r["source_org"],
                r["source_name"],
                r["topic"],
                r["credibility"],
                r["url"],
            ])
        note = [
            f"**{label}** 板块内检索 **{kw}**：命中 **{len(hits)}** 条",
            _expansion_text(search_mod.expand_query(search_mod.parse_query(kw))),
            "排序：标题命中 > 摘要 > 正文，再叠时效加成；同一通稿多站转发已折叠",
        ]
    else:
        raw = store.recent_articles(conn, hours=int(hours or 168),
                                    limit=int(limit), topic=topic or None)
        for r in raw:
            title = channel_title(r["title"], r["title_zh"])
            out.append([
                _local_time(r["published_at"] or r["fetched_at"]),
                title,
                r["source_org"] or r["source_name"],
                r["source_name"],
                r["topic"] or "其他",
                round(float(r["credibility"] or 0), 1),
                r["url"],
            ])
        scope = "全部历史" if int(hours or 0) <= 0 else _window_text(hours)
        note = [f"**{label}** 板块：{scope}内 **{len(out)}** 条新闻，按时间倒序"]
        if unknown:
            note.append(f"（未识别的分区「{unknown}」，已按「要闻（全部）」显示）")

    total = dict(topics.stats(conn))
    conn.close()
    note.append("各板块存量：" + "、".join(
        f"{k} {v}" for k, v in sorted(total.items(), key=lambda kv: -kv[1])))
    return out, "\n\n".join(note)


def ui_search(query: str, hours: int, limit: int, topic: str = TOPIC_ANY):
    """关键词检索。

    注意：这个回调**故意不挂 ``gr.Timer``**。定时器会每 60 秒把全表 LIKE 扫一遍，
    而用户可能根本没在看这一页 —— 纯属白烧 CPU。检索由回车/按钮触发，
    只读不写，代价一次性。
    """
    conn = store.connect()
    store.init_db(conn)
    terms = search_mod.parse_query(query)
    if not terms:
        return [], ("_请在上方输入关键词后按回车。_\n\n"
                    "> 多个关键词用空格分隔，**要求全部命中**（例如「无人机 反制」）；\n"
                    "> 关键词可以命中标题、摘要或正文，命中的位置越靠前排序越靠前。")

    # 同 fetch_event_rows：按分区筛选时先多取一些再过滤，保证结果条数。


    _want = int(limit) * 5 if (topic and topic != TOPIC_ANY) else int(limit)


    rows = search_mod.search_articles(conn, query, hours=int(hours or 0),


                                  limit=_want)


    if topic and topic != TOPIC_ANY:


        rows = [r for r in rows if (r.get("topic") or "其他") == topic]


    rows = rows[:int(limit)]
    cov = search_mod.coverage(conn)
    scope = _window_text(hours) if int(hours or 0) > 0 else "不限时窗"
    note = [
        f"共 **{len(rows)}** 条命中｜时窗：{scope}｜关键词："
        + "、".join(f"**{t}**" for t in terms),
        _expansion_text(search_mod.expand_query(terms)),
        f"排序：标题命中 {search_mod.W_TITLE:g} 分 > 摘要 {search_mod.W_SUMMARY:g} 分 "
        f"> 正文 {search_mod.W_CONTENT:g} 分，再叠时效加成",
        f"库存 {cov['total']} 篇；外文/需翻译 {cov['foreign_like']} 篇，"
        f"其中已有中文译名 **{cov['translated']}** 篇",
        "同一通稿被多家站点转发的结果已**折叠为一行**，"
        "并在「来源站点」列标出「N 站同稿」",
    ]
    if topic and topic != TOPIC_ANY:
        note.append(f"分区筛选：**{topic}**"
                    "（由 `src/topics.py` 自研关键词加权分类器判定）")

    out = []
    for r in rows:
        sites = "、".join(r["dup_names"][:2])
        if r["dup_sites"] > 1:
            sites += f" 等 {r['dup_sites']} 站同稿"
        out.append([

        r["score"],

        r["source_org"],
            f"tier{r['tier']} {sites}",
            r["topic"],
            _local_time(r["ts"]),
            (r["title"] or "")[:58],
            ((r["title_orig"] or "")[:50] if r["translated"] else "—"),
            (r["snippet"] or "")[:72],
            r["credibility"],
            r["url"],
            r["event_id"] or "—",
        ])
    if not rows:
        note.append("> 没有命中。可以少给一个关键词，或把时间窗口放宽到「不限（全部历史）」。")
    return out, "\n\n".join(note)


def _render_brief(result: Dict[str, Any], wall: Optional[float] = None,
                  cached: bool = False) -> Tuple[str, str]:
    """把一次生成结果渲染成（简报 Markdown, 来源 Markdown）。

    单独抽出来是为了让**缓存路径**复用同一套渲染 ——
    否则「读缓存」与「实时生成」两条路的显示会越长越歪。
    """
    ver = result.get("verification") or {}
    # 降级后 result["verification"] 描述的是**抽取式文本**（逐句摘自原文，必然通过），
    # 直接渲染会出现「事实一致性校验：✅ 通过」与「⚠️ 已降级为抽取式摘要」
    # 同时出现、自相矛盾，并且掩盖了模型真实的失败原因。
    # 所以降级时改看降级前那份报告（cli.cmd_daily 已经是这个口径）。
    is_fallback = bool(result.get("_fallback"))
    shown = (result.get("verification_before_fallback") or ver) if is_fallback else ver
    if not is_fallback:
        badge = "✅ 通过" if ver.get("accepted") else "⚠️ 未通过（已降级）"
    elif shown.get("accepted") is False:
        badge = "⚠️ 模型稿未通过校验（已降级为抽取式）"
    else:
        badge = "⚠️ 已降级为抽取式摘要"
    timing = (f"耗时 {result.get('elapsed_ms')} ms（含校验共 {wall:.1f}s）"
              if wall is not None else "**本次直接读取本地缓存，未调用模型**")
    md = [
        f"## {result.get('headline') or '（无标题）'}",
        "",
        f"**价值点**：{result.get('value_point') or '—'}",
        "",
        RULE,
        "",
        result.get("brief") or "（简报为空）",
        "",
        RULE,
        "",
        f"**事实一致性校验：{badge}**",
        "",
        f"- 带依据句：{shown.get('n_supported')}/{shown.get('n_claims')}"
        f"（{shown.get('supported_ratio', 0):.0%}，阈值 {shown.get('min_supported_ratio', 0):.0%}）",
        f"- 简报长度：{shown.get('brief_chars')} 字"
        f"{'（合规）' if shown.get('length_ok') else '（偏离目标）'}"
        + (f"　｜　**字数按素材厚度自适应**：素材事实 {result.get('material_facts')} 字"
           f" → 目标 {result.get('brief_chars_target')} 字"
           f"（上限 {result.get('brief_chars_target_base')}）"
           if result.get("material_facts") else ""),
        f"- 生成后端：`{result.get('backend')}` ｜ 模型：`{result.get('model')}`"
        f" ｜ {timing}",
    ]
    if cached:
        md.insert(0, "> 📌 **这份简报来自本地缓存**（同一事件不重复调用模型）。"
                     "需要一份新的，请点「重新生成（忽略缓存）」。\n")
    if int(result.get("n_orgs") or 0) <= 1:
        md.insert(0, "> ⚠️ **本事件只有 1 家机构报道**，拿不到「多方观点」可比对，"
                     "简报只基于该来源。想看多家媒体／专家口径交叉的事件，"
                     "请到「热点事件」把「最少印证机构数」调到 2（并放宽时间窗口）。\n")
    if result.get("_fallback"):
        md.append(f"- ⚠️ 已降级为抽取式摘要：{result.get('_fallback_reason', '')}")
    if ver.get("speculative_hits"):
        md.append(f"- ⚠️ 检出推测性表述：{'、'.join(ver['speculative_hits'])}")
    if ver.get("uncovered_sentences"):
        md.append(f"- ⚠️ 有 {len(ver['uncovered_sentences'])} 句未被原文支撑")
    if result.get("conflicts"):
        md += ["", "**来源分歧**："] + [f"- {c}" for c in result["conflicts"]]

    # 明确写出「融合了几家独立机构、几篇报道」，并列出机构名。
    # 用户的要求是"融合多家媒体的报道，并在信息来源处标注好" ——
    # 只逐条列来源不足以让人一眼看出融合了几家，这里补一行汇总。
    _srcs = result.get("sources") or []
    _orgs = sorted({(s.get("source_org") or s.get("source_name") or "?") for s in _srcs})
    sources_md = [
        "#### 信息来源",
        "",
        (f"本简报融合 **{len(_orgs)} 家独立机构**的 **{len(_srcs)} 篇报道**"
         + ("：" + "、".join(_orgs) if _orgs else "。")),
        "",
    ]
    for s in result.get("sources") or []:
        # 全部用 .get：缓存里可能是旧版本存下的 payload，字段未必齐全，
        # 渲染不能因为少一个字段就报错。
        sources_md += [
            f"**[{s.get('index', '?')}] {s.get('source_name', '?')}** ｜ "
            f"{s.get('source_org', '?')} ｜ tier{s.get('tier', '?')}",
            "",
            f"{s.get('title', '')}",
            "",
            f"{s.get('url', '')}",
            "",
        ]
    verified = ver.get("claims") or []
    if verified:
        sources_md += ["#### 句级溯源结果", "", "| 句子 | 依据 | 覆盖率 | 判定 |",
                       "|---|---|---|---|"]
        for c in verified[:15]:
            mark = "✅" if c.get("supported") else "❌"
            sources_md.append(
                f"| {(c.get('sentence') or '')[:36]}… | [{c.get('source_index')}] "
                f"{c.get('source_name') or '?'} | {c.get('coverage')} | {mark} |"
            )
    return "\n".join(md), "\n".join(sources_md)


def ui_generate(event_id: Optional[str], backend_kind: str, force: bool = False):
    """生成简报。``force=False`` 时优先返回本地已有的结果。

    为什么要缓存：一次生成要跑模型（即使权重已预热，也要十几到几十秒），
    而演示与复核时经常反复点同一个事件。缓存把「重复查看」变成 0 秒，
    同时用独立的「重新生成」按钮把「我要一份新的」这条路留着 ——
    两者不是二选一，而是把两种意图分开表达。
    """
    if not event_id:
        return "_请先选择一个事件。_", ""
    conn = store.connect()
    store.init_db(conn)

    if not force:
        cached = store.get_brief(conn, event_id)
        if cached and cached.get("brief"):
            return _render_brief(cached, cached=True)

    cfg = llm.load_llm_config()
    kind = backend_kind if backend_kind in ("local", "api") else None
    t0 = time.time()
    # 用 get_backend（按 kind 缓存）而**不是** build_backend：
    # build_backend 每次都会新建 LocalTransformersBackend，从而触发一次完整的
    # 3B 权重加载（本机实测冷启动约 40 秒）—— 这就是「生成很慢」的主因之一。
    backend = llm.get_backend(cfg, kind) if kind else None
    try:
        result = brief_mod.generate_brief(conn, event_id, cfg=cfg, backend=backend)
    except Exception as exc:
        return f"**生成失败**：{type(exc).__name__}: {exc}", ""

    if result.get("error"):
        return f"**生成失败**：{result['error']}", ""

    store.save_brief(conn, result)
    return _render_brief(result, wall=time.time() - t0)


def ui_stats() -> str:
    conn = store.connect()
    store.init_db(conn)
    s = store.stats(conn)
    events = conn.execute(
        "SELECT COUNT(DISTINCT event_id) AS n FROM articles WHERE event_id != ''"
    ).fetchone()["n"]
    multi = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT event_id FROM articles "
        "WHERE event_id != '' GROUP BY event_id "
        "HAVING COUNT(DISTINCT source_org) > 1)"
    ).fetchone()["n"]
    briefs = conn.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()["n"]

    cfg = llm.load_llm_config()
    # 用 get_backend 而不是 build_backend：这样拿到的是界面上真正在用的那个实例，
    # 显示的 load_mode（4bit / full）才是实际状态；且不会重复新建对象。
    backend = llm.get_backend(cfg)
    desc = backend.describe()

    lines = [
        "### 系统状态",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 入库文章 | {s['total']} |",
        f"| 最近 24 小时 | {s['last24']} |",
        f"| 事件簇 | {events} |",
        f"| 多机构印证事件 | {multi} |",
        f"| 已生成简报 | {briefs} |",
        "",
        "### 自动更新",
        "",
        ui_auto_status(),
        "",
        "### 时间口径",
        "",
        f"- **事件发生时间**：簇内**最早**一篇的报道时间。源站给的 `published_at` "
        f"若与采集时间相差超过 {cluster.PUBLISHED_SANITY_DAYS} 天，判为脏数据，"
        f"回退用采集时间（实测全库 868 篇带发布时间的文章里有 263 篇属于这种）。",
        "- **最新跟进**：簇内**最新**一篇的报道时间。",
        "- **可信度**：`100 × f_tier × exp(-age/72h) × (1 - 0.4/k)`，"
        "`age` 按最新跟进计算 —— 所以列表越新鲜，分数越高。",
        "",
        "### 生成后端",
        "",
        f"- 配置后端：`{cfg['backend']}`",
        f"- 模型：`{desc.get('model_id') or desc.get('model')}`",
    ]
    if desc.get("backend") == "local":
        # 只展示项目相对路径：绝对路径会带上本机用户名（如 C:\Users\<姓名>\...），
        # 而匿名版截图不能让这类信息出现（赛题第四十四条 + 自查清单明列此项）。
        wd = str(desc.get("weights_dir") or "")
        try:
            rel = os.path.relpath(wd, PROJECT_ROOT)
            if not rel.startswith(".."):
                wd = rel.replace(os.sep, "/")
        except ValueError:
            pass
        lines.append(f"- 权重目录：`{wd}`")
        lines.append(f"- 权重就绪：{'✅' if desc.get('weights_present') else '❌ 需先下载'}")
    else:
        lines.append(f"- 密钥环境变量：`{desc.get('api_key_env')}`"
                     f"（{'已设置' if desc.get('api_key_present') else '未设置'}）")

    lines += ["", "### 按来源", "", "| 来源 | 地区 | 文章数 | 最近 |", "|---|---|---|---|"]
    for r in s["by_source"]:
        lines.append(f"| {r['source_name']} | {r['region']} | {r['c']} | "
                     f"{_local_time(r['latest'])} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="多源新闻聚合与可溯源简报智能体") as demo:
        gr.Markdown(
            "# 多源新闻聚合与可溯源简报智能体\n"
            "把多源公开新闻聚合成**事件级简报**，每个事实句都可回溯到原文，"
            "每条信息都带可信度评分。\n\n"
            "> 我们不生产新闻，只回答一个问题：**这条信息，有几家独立机构在说？**"
        )

        with gr.Tab("板块新闻", id="channels"):
            gr.Markdown(
                "**按板块浏览新闻**：点上面的板块，看该类的**全部新闻**；"
                "板块内还能直接搜索。\n\n"
                "> 板块由**自研**关键词加权分类器判定（`src/topics.py`）——"
                "全库秒级、同一份数据结果逐字节一致、能说清是哪几个词判进去的，"
                "不依赖任何外部服务。"
            )
            channel = gr.Radio(choices=CHANNEL_CHOICES, value="", label="板块")
            with gr.Row():
                c_kw = gr.Textbox(label="在板块内搜索", scale=3,
                                  placeholder="留空 = 按时间倒序看该板块全部新闻；回车即搜")
                c_hours = gr.Dropdown(choices=HOURS_CHOICES, value=168, label="时间窗口")
                c_limit = gr.Slider(10, 200, value=50, step=10, label="显示条数")
            with gr.Row():
                c_btn = gr.Button("搜索 / 刷新", variant="primary")
            c_note = gr.Markdown("")
            c_list = gr.Dataframe(headers=CHANNEL_HEADERS, value=[],
                                  interactive=False, wrap=True)

            # 四个控件都绑各自的触发方式：下拉/单选绑 change（改完立即生效，
            # 不要等定时器），滑块用 release（松手才跑，避免拖动时连发几十次）。
            c_btn.click(ui_channel, [channel, c_kw, c_hours, c_limit], [c_list, c_note])
            c_kw.submit(ui_channel, [channel, c_kw, c_hours, c_limit], [c_list, c_note])
            channel.change(ui_channel, [channel, c_kw, c_hours, c_limit], [c_list, c_note])
            c_hours.change(ui_channel, [channel, c_kw, c_hours, c_limit], [c_list, c_note])
            c_limit.release(ui_channel, [channel, c_kw, c_hours, c_limit], [c_list, c_note])

        with gr.Tab("热点事件"):
            gr.Markdown(
                f"列表**每 {LIST_REFRESH_SECONDS} 秒自动重载**，数据由后台按周期自动采集，"
                "不需要手动刷新。**信息来源**与**事件发生时间**直接标在表里。"
            )
            with gr.Row():
                min_orgs = gr.Slider(1, 5, value=1, step=1, label="最少印证机构数")
                hours = gr.Dropdown(choices=HOURS_CHOICES, value=DEFAULT_HOURS,
                                    label="时间窗口")
                limit = gr.Slider(5, 50, value=15, step=5, label="显示条数")

                # 分区筛选：数据由自研分类器判定（全库秒级），可以当普通筛选维度用

                ev_topic = gr.Dropdown(choices=topic_choices(), value=TOPIC_ANY,

                                       label="分区")
            auto_line = gr.Markdown(ui_auto_status())
            with gr.Row():
                refresh = gr.Button("手动刷新列表")
                collect_btn = gr.Button("立即采集一轮", variant="primary")
            collect_msg = gr.Markdown("")
            summary = gr.Markdown("")
            table = gr.Dataframe(
                headers=EVENT_HEADERS, value=[], interactive=False, wrap=True
            )

            refresh.click(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                          [table, summary, auto_line])
            collect_btn.click(ui_collect_now, [], [collect_msg])

            # 控件一变就立即重绘，不等下一个定时器周期。
            # 滑块用 release（松手才跑）而不是 input，否则拖动过程中会连发几十次查询。
            hours.change(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                         [table, summary, auto_line])
            # 下拉框必须绑 .change：只绑按钮/定时器的话，用户改完分区要干等一个周期
            # （这个坑在「时间窗口」上踩过一次，见进度交接 §8.11）
            ev_topic.change(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                            [table, summary, auto_line])
            min_orgs.release(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                             [table, summary, auto_line])
            limit.release(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                          [table, summary, auto_line])

            # 定时器：只重读数据库重绘界面，不触发采集
            list_timer = gr.Timer(value=LIST_REFRESH_SECONDS)
            list_timer.tick(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                            [table, summary, auto_line])

        with gr.Tab("搜索"):
            gr.Markdown(
                "输入关键词，检索库内相关新闻。**关键词可命中标题、摘要或正文**，"
                "排序先看命中位置（标题 > 摘要 > 正文），再看时效；"
                "外文标题会自动附上中文译名。\n\n"
                "> 在输入框里直接按回车即可搜索。**本页不做自动刷新** ——"
                "检索是「我主动找东西」的动作，定时刷新只会空耗查询。"
            )
            with gr.Row():
                q_box = gr.Textbox(
                    label="关键词", scale=3,
                    placeholder="例如：诺贝尔经济学奖   ／   无人机 反制   ／   Lockheed",
                )
                s_hours = gr.Dropdown(choices=HOURS_CHOICES, value=0,

                                              label="时间窗口")

                s_topic = gr.Dropdown(choices=topic_choices(), value=TOPIC_ANY,

                                              label="分区")
                s_limit = gr.Slider(10, 100, value=30, step=10, label="最多返回")
                s_btn = gr.Button("搜索", variant="primary")
            s_note = gr.Markdown("_请在上方输入关键词后按回车。_")
            s_table = gr.Dataframe(headers=SEARCH_HEADERS, value=[],
                                   interactive=False, wrap=True)

            s_btn.click(ui_search, [q_box, s_hours, s_limit, s_topic], [s_table, s_note])
            q_box.submit(ui_search, [q_box, s_hours, s_limit, s_topic], [s_table, s_note])
            s_hours.change(ui_search, [q_box, s_hours, s_limit, s_topic], [s_table, s_note])
            s_topic.change(ui_search, [q_box, s_hours, s_limit, s_topic],
                           [s_table, s_note])
            s_limit.release(ui_search, [q_box, s_hours, s_limit, s_topic], [s_table, s_note])

        with gr.Tab("证据链"):
            gr.Markdown("展开一个事件的可信度构成与全部来源。"
                        f"每 {CHOICES_REFRESH_SECONDS} 秒自动同步事件列表，"
                        "**已选中的事件不会被重置**。\n\n"
                        "> ⚠️ **多机构印证的事件很稀少**（实测全库 1184 个事件里只有 9 个，"
                        "近 24 小时内 0 个）——这是数据事实，不是缺陷。默认机构数设为 1 以保证"
                        "下拉框总有内容；想看多机构印证的场景，把它调到 2 并把时间窗口放宽到 7 天。")
            with gr.Row():
                ev_min_orgs = gr.Slider(1, 5, value=1, step=1, label="最少印证机构数")
                ev_hours = gr.Dropdown(choices=HOURS_CHOICES, value=DEFAULT_HOURS,
                                       label="时间窗口")
                ev_refresh = gr.Button("重新加载事件列表")
            event_pick = gr.Dropdown(choices=[], label="选择事件", interactive=True,
                                      # allow_custom_value：事件列表是定时刷新的，
                                      # 当「机构数 + 时间窗口」组合下没有事件时 choices 会变成 []，
                                      # 而前端仍留着上次选中的 event_id —— Gradio 会在**预处理阶段**
                                      # 直接抛 "Value: evt_xxx is not in the list of choices: []"，
                                      # 请求根本进不到回调，表现就是「点生成没反应」。
                                      # 实测服务端日志里能看到这条 Error。
                                      allow_custom_value=True)
            evidence = gr.Markdown("_请先加载并选择一个事件。_")

            ev_refresh.click(pick_event, [ev_min_orgs, ev_hours], [event_pick])
            event_pick.change(ui_evidence, [event_pick], [evidence])
            ev_hours.change(partial(ui_refresh_choices, key="evidence"), [ev_min_orgs, ev_hours, event_pick],
                            [event_pick])
            ev_min_orgs.release(partial(ui_refresh_choices, key="evidence"), [ev_min_orgs, ev_hours, event_pick],
                                [event_pick])

            ev_timer = gr.Timer(value=CHOICES_REFRESH_SECONDS)
            ev_timer.tick(partial(ui_refresh_choices_tick, key="evidence"), [], [event_pick])

        with gr.Tab("简报生成"):
            gr.Markdown("调用**本地开源模型**生成简报，并实时展示事实一致性校验报告。"
                        "事件列表自动同步，**已选中的事件不会被重置**。\n\n"
                        "> 服务启动时已在后台**预热模型权重**，所以第一次生成通常只需几秒；\n"
                        "> **同一个事件不会重复调模型** —— 已有结果会直接读缓存返回，"
                        "要重新生成请点「重新生成（忽略缓存）」。\n"
                        "> 若这个事件含较多**未归一化的外文材料**，会先为前面的几篇做中文归一化"
                        "（每篇一次推理）。先用 `scripts/warm_translations.py --with-summary`\n"
                        "> 离线预热过，这一项开销就不会出现。\n\n"
                        "> ⚠️ 若下拉框为空，说明当前「机构数 + 时间窗口」组合下没有事件——"
                        "把最少印证机构数调到 1、或把时间窗口放宽即可。")
            with gr.Row():
                gen_min_orgs = gr.Slider(1, 5, value=1, step=1, label="最少印证机构数")
                # 默认放宽到 7 天：多机构印证的事件很稀少（实测全库 1457 个事件里
                # 只有 10 个，近 24 小时内 0 个）。默认 24 小时会让用户几乎只能选到
                # 单机构事件，而"融合多家媒体报道"恰恰是本页要展示的能力。
                gen_hours = gr.Dropdown(choices=HOURS_CHOICES, value=168,
                                        label="时间窗口")
                backend = gr.Radio(
                    choices=[("本地开源模型", "local"), ("在线 API", "api")],
                    value="local", label="生成后端",
                )
            with gr.Row():
                gen_pick = gr.Dropdown(choices=[], label="选择事件", interactive=True,
                                      # allow_custom_value：事件列表是定时刷新的，
                                      # 当「机构数 + 时间窗口」组合下没有事件时 choices 会变成 []，
                                      # 而前端仍留着上次选中的 event_id —— Gradio 会在**预处理阶段**
                                      # 直接抛 "Value: evt_xxx is not in the list of choices: []"，
                                      # 请求根本进不到回调，表现就是「点生成没反应」。
                                      # 实测服务端日志里能看到这条 Error。
                                      allow_custom_value=True)
                gen_refresh = gr.Button("重新加载事件列表")
                gen_btn = gr.Button("生成简报", variant="primary")
                gen_again = gr.Button("重新生成（忽略缓存）")
            out_brief = gr.Markdown("")
            out_sources = gr.Markdown("")

            gen_refresh.click(pick_event, [gen_min_orgs, gen_hours], [gen_pick])
            # 生成单独占一个并发组且串行：否则一次生成会把队列堵住，
            # 用户看到的就是「整页卡住」（实测）。读类回调留在默认组里并行。
            gen_btn.click(ui_generate, [gen_pick, backend], [out_brief, out_sources],
                          concurrency_id="llm", concurrency_limit=1)
            gen_again.click(partial(ui_generate, force=True), [gen_pick, backend],
                            [out_brief, out_sources],
                            concurrency_id="llm", concurrency_limit=1)
            gen_hours.change(partial(ui_refresh_choices, key="generate"), [gen_min_orgs, gen_hours, gen_pick],
                             [gen_pick])
            gen_min_orgs.release(partial(ui_refresh_choices, key="generate"), [gen_min_orgs, gen_hours, gen_pick],
                                 [gen_pick])

            gen_timer = gr.Timer(value=CHOICES_REFRESH_SECONDS)
            gen_timer.tick(partial(ui_refresh_choices_tick, key="generate"), [], [gen_pick])

        with gr.Tab("系统状态"):
            gr.Markdown(f"每 {STATS_REFRESH_SECONDS} 秒自动刷新。")
            stats_btn = gr.Button("手动刷新")
            stats_out = gr.Markdown(ui_stats())
            stats_btn.click(ui_stats, [], [stats_out])
            stats_timer = gr.Timer(value=STATS_REFRESH_SECONDS)
            stats_timer.tick(ui_stats, [], [stats_out])

        # 页面加载即预填三个页签的数据，演示时无需先手动点「加载事件列表」
        demo.load(ui_refresh_events, [min_orgs, limit, hours, ev_topic],
                  [table, summary, auto_line])
        # 打开页面就铺好「要闻」信息流（新闻客户端的默认行为）
        demo.load(ui_channel, [channel, c_kw, c_hours, c_limit], [c_list, c_note])
        demo.load(partial(ui_refresh_choices_tick, key="evidence"), [], [event_pick])
        demo.load(partial(ui_refresh_choices_tick, key="generate"), [], [gen_pick])
        # 系统状态也必须在加载时刷一次：stats_out 的初始值是 build_ui() 那一刻算的，
        # 只靠 120 秒的定时器，用户打开页面后可能长时间看到过期统计（实测踩到）
        demo.load(ui_stats, [], [stats_out])

    # 队列并发：默认每类事件一次只跑一个，读类回调（定时刷新、检索）允许并行，
    # 生成类回调由 concurrency_id="llm" 单独串行 —— 这样生成期间其它页签仍可正常用。
    demo.queue(default_concurrency_limit=8)
    return demo


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 Web 界面")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--share", action="store_true")
    parser.add_argument("--no-auto-collect", action="store_true",
                        help="关闭后台定时采集（界面仍会自动刷新，但数据不会变新）")
    parser.add_argument("--interval", type=float,
                        default=scheduler.DEFAULT_INTERVAL_MINUTES,
                        help="后台采集周期，单位分钟（默认 30）")
    parser.add_argument("--stale-hours", type=float,
                        default=scheduler.DEFAULT_STALE_HOURS,
                        help="数据陈旧阈值，单位小时（默认 24）；超过则启动后立即补采一轮")
    parser.add_argument("--no-prewarm", action="store_true",
                        help="不在后台预热模型权重（默认会预热，避免首次生成干等）")
    args = parser.parse_args()

    config.ensure_dirs()
    conn = store.connect()
    store.init_db(conn)
    conn.close()

    global _UPDATER
    if args.no_auto_collect:
        print("[APP] 后台自动采集已关闭（--no-auto-collect）："
              "界面会照常自动刷新，但数据需手动跑 python -m src.cli pipeline")
    else:
        _UPDATER = scheduler.AutoUpdater(interval_minutes=args.interval,
                                         stale_hours=args.stale_hours)
        _UPDATER.start()
        print(f"[APP] 后台自动采集已启动：周期 {_UPDATER.interval_minutes:g} 分钟，"
              f"陈旧阈值 {_UPDATER.stale_hours:g} 小时")

    demo = build_ui()
    # 后台预热模型权重：把「加载 40 秒」和「用户看界面、选事件」在时间上重叠。
    # 不预热的话，用户第一次点「生成简报」会干等四十秒，看起来就像卡死了。
    if not args.no_prewarm:
        try:
            llm.prewarm_async(llm.load_llm_config())
            print("[APP] 已在后台预热模型权重（首次生成无需再等加载）")
        except Exception as exc:                    # noqa: BLE001
            print(f"[APP] 预热未启动：{type(exc).__name__}: {exc}")
    try:
        demo.launch(server_name=args.host, server_port=args.port,
                    share=args.share, show_error=True)
    finally:
        if _UPDATER is not None:
            _UPDATER.stop()
            print("[APP] 后台自动采集已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
