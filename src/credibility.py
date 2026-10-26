"""可信度打分：来源权威性 x 时效性 x 交叉印证度。

这是本作品的第二个核心自研算法。设计目标是回答一个问题：
**「这条信息有多可信？」**

评分模型（满分 100）：

    credibility = 100 * f_tier * f_time * f_cross

- ``f_tier``  权威性因子：取簇内**最高等级**来源的权重。
  tier1（国防部/军网/新华网等官方源）= 1.0，tier2（主流媒体）= 0.7，
  tier3（聚合索引）= 0.4。同一事件只要有一家权威源报道，就按权威源计分。

- ``f_time``  时效因子：``exp(-age_hours / TAU)``，TAU 默认 72 小时。
  0 小时 = 1.00，24 小时 = 0.72，72 小时 = 0.37，168 小时 = 0.10。
  简报产品的价值随时间快速衰减，必须显式建模。

- ``f_cross`` 交叉印证因子：``1 - 0.4 / k``，k = 簇内**不同媒体机构**的数量。
  k=1 -> 0.60（单一机构，标记"待核实"）
  k=2 -> 0.80
  k=3 -> 0.87
  k=4 -> 0.90
  单一来源永远拿不到满分，这是本模型最重要的性质：
  **它把"孤证"和"多源印证"在分数上明确区分开。**

**关键设计：按「机构」而不是「站点」计数。**
实测发现 Army Times / Navy Times / Air Force Times / Military Times /
Defense News / C4ISRNET 同属一家出版集团（Sightline Media Group），
同一篇稿件会在这些站点同时出现。若按站点数计印证度，一个事件会被算成
"5 家源印证"，实际上只是**一家机构的报道被重复计数**，会系统性高估可信度。
因此本模型按机构去重，机构映射见 ``config/orgs.yaml``。

可信度是**事件级**的：同一事件簇内所有文章写入同一个分数，
因为交叉印证是事件属性而非单篇属性。

所有因子与中间量都可通过 :func:`explain` 取出，
供界面做"证据链"展示 —— 可解释性在这里体现。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import math
import sqlite3

from .config import TIER_WEIGHTS, DEFAULT_TIER_WEIGHT
from .store import backfill_source_org

TAU_HOURS = 72.0        # 时效衰减时间常数（小时）
SINGLE_ORG_K = 1

HIGH_THRESHOLD = 70.0   # >= 高可信
MEDIUM_THRESHOLD = 45.0 # >= 中等，其余为时效衰减/待核实


def tier_factor(tier: int) -> float:
    return TIER_WEIGHTS.get(int(tier), DEFAULT_TIER_WEIGHT)


def time_factor(age_hours: float, tau: float = TAU_HOURS) -> float:
    if age_hours <= 0:
        return 1.0
    return math.exp(-age_hours / tau)


def cross_factor(n_orgs: int) -> float:
    """交叉印证因子。k 为不同媒体机构数，单一机构封顶 0.60。"""
    k = max(1, int(n_orgs))
    return 1.0 - 0.4 / k


def _age_hours(ts: Optional[str], now: Optional[datetime] = None) -> float:
    """把 ISO8601(UTC) 字符串转成距今小时数。解析失败返回 0（按最新处理）。"""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)


def score_event(
    n_orgs: int,
    best_tier: int,
    latest_ts: Optional[str],
    tau: float = TAU_HOURS,
    now: Optional[datetime] = None,
) -> Tuple[float, Dict[str, float]]:
    """返回 (可信度 0-100, 因子明细)。"""
    f_tier = tier_factor(best_tier)
    f_cross = cross_factor(n_orgs)
    age = _age_hours(latest_ts, now=now)
    f_time = time_factor(age, tau=tau)
    score = 100.0 * f_tier * f_time * f_cross
    return round(score, 2), {
        "f_tier": f_tier,
        "f_time": round(f_time, 4),
        "f_cross": round(f_cross, 4),
        "age_hours": round(age, 2),
        "n_orgs": int(n_orgs),
        "best_tier": int(best_tier),
    }


def label_of(score: float, n_orgs: int) -> str:
    """给界面用的中文标签。"""
    if n_orgs <= SINGLE_ORG_K:
        return "单一机构·待核实"
    if score >= HIGH_THRESHOLD:
        return "多机构印证·高可信"
    if score >= MEDIUM_THRESHOLD:
        return "多机构印证·中等"
    return "多机构印证·时效衰减"


def score_all(
    conn: sqlite3.Connection,
    tau: float = TAU_HOURS,
    verbose: bool = True,
) -> Dict[str, int]:
    """全量重算可信度，写入 articles.credibility。"""
    backfill_source_org(conn)
    rows = conn.execute(
        "SELECT event_id, source_id, source_org, source_tier, "
        "       COALESCE(published_at, fetched_at) AS ts "
        "FROM articles"
    ).fetchall()
    if not rows:
        if verbose:
            print("  [SCORE] 库里没有文章，跳过")
        return {"articles": 0, "events": 0}

    clusters: Dict[str, Dict] = {}
    for row in rows:
        # event_id 为空说明该文未参与聚类，按独立单篇处理
        key = row["event_id"] or ("solo:" + str(row["source_id"]))
        info = clusters.setdefault(
            key, {"orgs": set(), "best_tier": 99, "latest": None}
        )
        info["orgs"].add(row["source_org"] or row["source_id"])
        info["best_tier"] = min(info["best_tier"], int(row["source_tier"]))
        ts = row["ts"]
        if ts and (info["latest"] is None or ts > info["latest"]):
            info["latest"] = ts

    event_scores: Dict[str, float] = {}
    for key, info in clusters.items():
        score, _ = score_event(
            n_orgs=len(info["orgs"]),
            best_tier=info["best_tier"],
            latest_ts=info["latest"],
            tau=tau,
        )
        event_scores[key] = score

    # 以 event_id 批量更新；未聚类的文章在下面单独兜底
    conn.executemany(
        "UPDATE articles SET credibility = ? WHERE event_id = ? AND event_id != ''",
        [(score, key) for key, score in event_scores.items() if not key.startswith("solo:")],
    )
    # 未聚类的文章（event_id 为空）按各自来源单独打分
    solo_rows = conn.execute(
        "SELECT DISTINCT source_id, source_tier FROM articles WHERE event_id = ''"
    ).fetchall()
    for row in solo_rows:
        score, _ = score_event(
            n_orgs=1, best_tier=int(row["source_tier"]), latest_ts=None, tau=tau
        )
        conn.execute(
            "UPDATE articles SET credibility = ? WHERE event_id = '' AND source_id = ?",
            (score, row["source_id"]),
        )
    conn.commit()

    result = {"articles": len(rows), "events": len(event_scores)}
    if verbose:
        top = conn.execute(
            "SELECT MAX(credibility) AS m, MIN(credibility) AS lo FROM articles"
        ).fetchone()
        multi = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT event_id FROM articles WHERE event_id != '' "
            "  GROUP BY event_id HAVING COUNT(DISTINCT source_org) > 1)"
        ).fetchone()["n"]
        print(f"  [SCORE] 事件 {result['events']} 个，文章 {result['articles']} 篇，"
              f"分数区间 {top['lo']} ~ {top['m']}（TAU={tau:g}h）")
        print(f"          按机构去重后，多机构印证的事件 {multi} 个"
              f"（此前按站点计数会更多，属高估）")
    return result


def explain(conn: sqlite3.Connection, event_id: str, tau: float = TAU_HOURS) -> Dict:
    """给出一个事件的完整评分依据，供界面做证据链展示。"""
    rows = conn.execute(
        "SELECT source_id, source_name, source_org, source_tier, region, title, "
        "       title_zh, url, "
        "       COALESCE(published_at, fetched_at) AS ts, credibility "
        "FROM articles WHERE event_id = ? "
        "ORDER BY source_tier ASC, ts DESC",
        (event_id,),
    ).fetchall()
    if not rows:
        return {}

    orgs = {r["source_org"] or r["source_id"] for r in rows}
    sources = {r["source_id"] for r in rows}
    best_tier = min(int(r["source_tier"]) for r in rows)
    latest = max((r["ts"] for r in rows if r["ts"]), default=None)
    score, factors = score_event(len(orgs), best_tier, latest, tau=tau)

    return {
        "event_id": event_id,
        "credibility": score,
        "label": label_of(score, len(orgs)),
        "factors": factors,
        "n_articles": len(rows),
        "n_orgs": len(orgs),
        "n_sources": len(sources),
        "orgs": sorted(orgs),
        "sources": [
            {
                "source_id": r["source_id"],
                "source_name": r["source_name"],
                "source_org": r["source_org"],
                "tier": int(r["source_tier"]),
                "region": r["region"],
                "title": r["title"],
                "title_zh": r["title_zh"],
                "url": r["url"],
                "time": r["ts"],
            }
            for r in rows
        ],
    }
