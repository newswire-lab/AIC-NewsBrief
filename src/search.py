"""关键词检索：输入关键词，检索近期相关新闻。

**为什么不用 FTS5。** 库里文章是千级，`LIKE` 全表扫描实测 < 100 ms，完全够用；
而 SQLite 的 FTS5 对中文默认用 `unicode61` 分词器，会把一整句中文切成**一个** token，
想搜「诺贝尔」必须写成 `诺贝尔*` 前缀查询才命中 —— 对普通用户是更差的选择。
所以在当前数据规模下，`LIKE` 是**更准**的方案，不是妥协。

**为什么排序在 Python 里做。** 命中位置（标题 / 摘要 / 正文）决定这条结果有多大价值。
把权重写成常量，既可解释、又可单元测试，也方便写进技术报告。
顺带把「命中片段」截出来 —— 用户扫一眼就知道这条为什么被搜到。

**语义**：多个关键词之间是 **AND**（都要出现），因为新闻检索里
「关键词越多越精确」符合直觉；命中位置决定排序，而不是决定是否返回。

**同一通稿多发要折叠。** 实测搜 `hypersonic` 时，Sightline Media Group 旗下的
Defense News / Army Times / Military Times / Navy Times 四条结果**标题完全一样**，
各自占一行。同一篇通稿在不同站点重复上架是国际新闻媒体的常态，
不折叠的话搜索结果页会被同一件事刷屏。所以这里按 `title_hash` 折叠，
保留分最高的一条，并记下「共 N 个站点同稿」。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import cluster
from .models import utcnow

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_TERMS = 6            # 关键词过多时只取前若干个，避免 AND 条件爆炸
SNIPPET_PAD = 46         # 命中片段左右各截多少字符

# 命中权重：标题 > 摘要 > 正文。数量级差异要够大，否则一条"正文里蹭到关键词"
# 的长文会盖过一条标题正中的短讯。
W_TITLE = 5.0
W_SUMMARY = 2.5
W_CONTENT = 1.0
# 只命中来源名（如「新华社」）：说明用户在做**来源过滤**而不是找内容，
# 权重给得很低，保证它排在真内容命中之后。
W_SOURCE = 0.5

# 时效加成：exp(-age/72h) 与本项目其余部分（可信度的 f_time）保持同一衰减尺度，
# 免得同一个界面里出现两套"新鲜"标准。
RECENCY_BONUS = 1.5
RECENCY_TAU_HOURS = 72.0

_SPLIT_RE = re.compile(r"[\s,，、;；|/]+")
_QUOTES = "\"'“”‘’"


def parse_query(query: str) -> List[str]:
    """把用户输入拆成关键词列表（去重、限量、去掉包裹的引号）。"""
    terms: List[str] = []
    seen = set()
    for raw in _SPLIT_RE.split((query or "").strip()):
        t = raw.strip().strip(_QUOTES).strip()
        if not t:
            continue
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        terms.append(t)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def _age_hours(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (utcnow() - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)


def _snippet(text: str, terms: List[str], pad: int = SNIPPET_PAD) -> str:
    """截出命中关键词的那一小段，让用户知道这条为什么被搜到。"""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    low = text.lower()
    pos = -1
    for t in terms:
        p = low.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return text[:pad * 2] + ("…" if len(text) > pad * 2 else "")
    start = max(0, pos - pad)
    end = min(len(text), pos + pad)
    frag = text[start:end].strip()
    return ("…" if start > 0 else "") + frag + ("…" if end < len(text) else "")


# 关联词表：把一个词扩展成**一组相关词**。
#
# 为什么必须这么做：之前是完全的**字面子串匹配** —— 用户搜「俄乌」一条都命中不了，
# 因为**没有哪篇报道真的写着“俄乌”两个字**；搜「俄乌冲突」也不行。
# 而用户要的是“跟俄乌有关的所有新闻”。所以把查询扩展成词组，
# 组内是 OR（命中任一即算相关），组间是 AND（多个关键词仍要求都覆盖）。
RELATED: Dict[str, List[str]] = {
    "俄乌": ["乌克兰", "俄罗斯", "乌军", "俄军", "基辅", "莫斯科",
           "泽连斯基", "普京", "顿涅茨克", "哈尔科夫", "顿巴斯"],
    "巴以": ["以色列", "加沙", "哈马斯", "巴勒斯坦", "内塔尼亚胡", "耶路撒冷"],
    "中东": ["伊朗", "以色列", "加沙", "胡塞", "也门", "黎巴嫩", "叙利亚"],
    "胡塞": ["也门", "红海", "萨那", "亚丁"],
    "台海": ["台湾", "台军", "解放军", "台北"],
    "朝鲜": ["平壤", "金正恩", "朝核", "朝鲜半岛"],
    "伊朗": ["德黑兰", "哈梅内伊", "伊朗革命卫队"],
    "无人机": ["无人作战", "uav", "fpv", "巡飞弹", "无人艇", "usv", "drone"],
    "高超音速": ["hypersonic", "高超声速", "乘波体", "滑翔"],
    "反无人机": ["counter-drone", "反制无人机", "防空", "拦截", "激光武器"],
    "航母": ["航空母舰", "carrier", "舰载机", "航母编队"],
    "核": ["核武器", "核弹头", "核潜艇", "战略核", "nuclear"],
    "拉美": ["巴西", "阿根廷", "墨西哥", "委内瑞拉", "哥伦比亚"],
    "非洲": ["尼日利亚", "南非", "埃及", "埃塞俄比亚", "萨赫勒"],
    "芯片": ["半导体", "晶圆", "光刻", "chip", "semiconductor"],
    "关税": ["贸易战", "tariff", "制裁", "加征"],
}
# 词组最长保留多少个词（含原词），避免 LIKE 条件爆炸
MAX_GROUP = 8


def expand_query(terms: Sequence[str]) -> List[List[str]]:
    """把用户输入的关键词扩展成**词组**（每组是 OR 关系）。

    命中规则：
      1. 词的直接匹配（``"俄乌" in RELATED``）；
      2. 否则看**哪个键是它的子串** —— 这样「俄乌冲突」「俄乌局势」
         都能映射到「俄乌」那组词，而不是只能搜到字面词；
      3. 都不命中则原词单独成组（行为与以前一致）。
    """
    groups: List[List[str]] = []
    for t in terms:
        group = [t]
        key = t if t in RELATED else None
        if key is None:
            for k in RELATED:
                if k in t:
                    key = k
                    break
        if key:
            for w in RELATED[key]:
                if w.lower() not in {x.lower() for x in group}:
                    group.append(w)
                if len(group) >= MAX_GROUP:
                    break
        groups.append(group)
    return groups


def _score_row(row: Any, groups: Sequence[Sequence[str]]):
    """按命中位置打分。**以「词组」为单位**计分。

    组内取命中的**最高权重**、组间相加：这样「命中越多相关词排得越前」，
    又不会因为某个词组词多就把分数堆高（否则长词表天然占便宜）。
    """
    fields = [
        (W_TITLE, ((row["title"] or "") + " " + (row["title_zh"] or "")).lower()),
        (W_SUMMARY, ((row["summary"] or "") + " " + (row["summary_zh"] or "")).lower()),
        (W_CONTENT, (row["content"] or "").lower()),
        (W_SOURCE, ((row["source_name"] or "") + " " + (row["source_org"] or "")).lower()),
    ]
    score = 0.0
    hit_title = hit_summary = hit_source = 0
    for group in groups:
        best = 0.0
        where = ""
        for t in group:
            tl = t.lower()
            for weight, text in fields:
                if weight > best and tl in text:
                    best = weight
                    where = ("title" if weight == W_TITLE else
                             "summary" if weight == W_SUMMARY else
                             "content" if weight == W_CONTENT else "source")
        score += best
        if where == "title":
            hit_title += 1
        elif where == "summary":
            hit_summary += 1
        elif where == "source":
            hit_source += 1
    return score, hit_title, hit_summary, hit_source


def search_articles(
    conn: Any,
    query: str,
    hours: int = 0,
    limit: int = DEFAULT_LIMIT,
    topic: str = "",
) -> List[Dict[str, Any]]:
    """检索文章。``hours<=0`` 表示不限时窗。

    时窗按**文章自身**的时间戳判定，且与「热点事件」用同一套脏数据防护
    （``cluster.SANE_TS_SQL``）—— 同一个界面里不能有两套时间口径。
    """
    terms = parse_query(query)
    if not terms:
        return []
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    ts_expr = f"({cluster.SANE_TS_SQL})"
    where: List[str] = []
    params: List[Any] = []
    if int(hours or 0) > 0:
        where.append(f"datetime({ts_expr}) >= datetime('now', ?)")
        params.append(f"-{int(hours)} hours")
    # 限定分区（板块内搜索）：下推到 SQL，而不是取回后再过滤 ——
    # 否则 limit 会被别的分区的结果占满，用户看到"没几条"。
    if topic and topic not in ("", "全部分区"):
        where.append("topic = ?")
        params.append(topic)
    groups = expand_query(terms)
    for group in groups:
        clauses: List[str] = []
        group_params: List[Any] = []
        for t in group:
            # 转义 LIKE 的通配符。
            #
            # **实测踩到的坑**：不转义时搜 `_` 会变成模式 `%_%`（匹配任意非空串），
            # 在 2108 篇的库上直接返回满额结果；搜 `%` 更会返回全库。
            # 用户输一个下划线只是想搜含下划线的标题，不该得到整个文库。
            like = "%" + (t.replace("\\", "\\\\")
                          .replace("%", "\\%")
                          .replace("_", "\\_")) + "%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR title_zh LIKE ? ESCAPE '\\' "
                "OR summary LIKE ? ESCAPE '\\' OR summary_zh LIKE ? ESCAPE '\\' "
                "OR content LIKE ? ESCAPE '\\' "
                "OR source_name LIKE ? ESCAPE '\\' OR source_org LIKE ? ESCAPE '\\')"
            )
            group_params += [like] * 7
        # 组内 OR：命中同组的任一个词都算“这件事相关”
        where.append("(" + " OR ".join(clauses) + ")")
        params += group_params

    sql = (
        "SELECT id, title, title_zh, summary, summary_zh, content, source_name, "
        "       source_org, source_tier, url, event_id, credibility, title_hash, topic, "
        f"       {ts_expr} AS ts "
        "FROM articles WHERE " + " AND ".join(where)
    )
    rows = conn.execute(sql, params).fetchall()

    out: List[Dict[str, Any]] = []
    for row in rows:
        score, hit_title, hit_summary, hit_source = _score_row(row, groups)
        age = _age_hours(row["ts"])
        bonus = RECENCY_BONUS * math.exp(-age / RECENCY_TAU_HOURS) if age is not None else 0.0
        body = ((row["summary_zh"] or "").strip() or (row["summary"] or "").strip()
                or (row["content"] or "").strip())
        title_orig = row["title"] or ""
        title_zh = (row["title_zh"] or "").strip()
        out.append({
            "score": round(score + bonus, 3),
            "hit_title": hit_title,
            "hit_summary": hit_summary,
            "hit_source": hit_source,
            "matched_all": (hit_title + hit_summary + hit_source) >= len(groups),
            # 展示用标题：有中文译名就优先用它（这是「翻译」功能对搜索的增益）
            "title": title_zh or title_orig,
            "title_orig": title_orig,
            "translated": bool(title_zh),
            "snippet": _snippet(body, terms),
            "source_name": row["source_name"],
            "source_org": row["source_org"] or row["source_name"],
            # 新闻分区（src/topics.py 自研分类器判定，见 topics.stats）
            "topic": (row["topic"] or "") or "其他",
            "tier": int(row["source_tier"] or 2),
            "ts": row["ts"],
            "url": row["url"],
            "event_id": row["event_id"] or "",
            "credibility": round(float(row["credibility"] or 0.0), 1),
            "age_hours": age,
            "title_hash": row["title_hash"] or "",
        })

    out.sort(key=lambda r: (-r["score"], -r["credibility"]))
    return _collapse_same_story(out)[:limit]


def _collapse_same_story(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把「同一通稿被多家站点转发」折成一行。

    折叠键优先用 `title_hash`（入库时就按标题算好的）；它为空的极少数文章
    回退用截断后的小写标题，保证不会把不同的事误折在一起。
    """
    out: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = r["title_hash"] or (r["title"] or "")[:60].lower()
        first = seen.get(key)
        if first is not None:
            first["dup_sites"] += 1
            if r["source_name"] not in first["dup_names"]:
                first["dup_names"].append(r["source_name"])
            continue
        r["dup_sites"] = 1
        r["dup_names"] = [r["source_name"]]
        seen[key] = r
        out.append(r)
    return out


def coverage(conn: Any) -> Dict[str, int]:
    """检索相关的库存统计，供界面提示「有多少可搜内容」。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN lang != 'zh' OR length(title_zh) > 0 THEN 1 ELSE 0 END) AS foreign_like, "
        "       SUM(CASE WHEN length(title_zh) > 0 THEN 1 ELSE 0 END) AS translated "
        "FROM articles"
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "foreign_like": int(row["foreign_like"] or 0),
        "translated": int(row["translated"] or 0),
    }
