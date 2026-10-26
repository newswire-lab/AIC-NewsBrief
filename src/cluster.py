"""事件聚类：把不同源报道的同一事件归到一组。

这是本作品的核心自研算法之一。它解决问题的前提是：
**同一事件会被多家媒体以几乎相同的标题报道**，而这些报道的 URL 各不相同，
URL 去重抓不到它们 —— 但它们恰恰是"多源交叉印证"的证据。

算法分两阶段：

1. **精确阶段**：正文指纹（content_fp）完全相同的文章直接合并。
   跨源整篇转载时正文一字不差，是最强的判据，O(n)。

2. **近似阶段**：标题 SimHash（64 位）海明距离 <= threshold 的连边用并查集合并。
   朴素做法是两两比较 O(n^2)，这里用**分段桶**剪枝：
   把 64 位切成 4 段各 16 位，若两个哈希海明距离 <= 3，
   则 4 段中至少有一段**完全相同**（抽屉原理：3 个不同比特最多破坏 3 段）。
   因此只需在同一「段号 + 段值」的桶内精确比较，
   结果与全量两两比较**等价**，但候选集大幅缩小。

事件 ID 由簇内成员 URL 指纹排序后哈希生成，因此：
- 成员集合不变时，多次运行结果稳定（可幂等重跑）；
- 成员变化时会重新生成，这是可接受的（全量重算语义）。

注意：本模块**不会丢弃**任何一篇文章。跨源同题文章必须保留，
因为"被 N 家源同时报道"正是可信度打分与简报交叉印证的基础。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from hashlib import sha1
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import sqlite3

from .dedup import hamming64
from .models import iso, utcnow
from .similarity import TitleSimilarityIndex
from .store import backfill_source_org

BANDS = 4            # 64 位切成 4 段
BAND_BITS = 16       # 每段 16 位
BAND_MASK = (1 << BAND_BITS) - 1
DEFAULT_THRESHOLD = 3
DEFAULT_WINDOW_DAYS = 14
# published_at 脏数据防护：早于 fetched_at 超过该天数就不采信（详见 top_events）
PUBLISHED_SANITY_DAYS = 30
SANE_TS_SQL = (
    "CASE WHEN datetime(published_at) >= datetime(fetched_at, '-%d days') "
    "THEN published_at ELSE fetched_at END" % PUBLISHED_SANITY_DAYS
)
MAX_BUCKET_SIZE = 3000   # 单桶过大时跳过，防止极端退化
# 语义相似度阈值：实测标定，详见 scripts/evaluate_similarity.py
# 同事件样例得分 0.46~0.80，不同事件样例 0.00~0.23，取 0.50 偏保守
DEFAULT_SIM_THRESHOLD = 0.50
DEFAULT_CANDIDATES = 25   # 每条标题参与语义比较的候选数（倒排索引剪枝）


class UnionFind:
    """并查集（带路径压缩 + 按秩合并）。"""

    __slots__ = ("parent", "rank", "count")

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n
        self.count = n

    def find(self, x: int) -> int:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        # 路径压缩
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        self.count -= 1
        return True


def band_keys(value: int) -> List[Tuple[int, int]]:
    """返回 64 位哈希的 4 个 (段号, 段值)。"""
    return [(i, (value >> (i * BAND_BITS)) & BAND_MASK) for i in range(BANDS)]


def load_candidates(
    conn: sqlite3.Connection,
    days: int = DEFAULT_WINDOW_DAYS,
) -> List[sqlite3.Row]:
    """取窗口内参与聚类的文章。"""
    return conn.execute(
        "SELECT id, source_id, source_name, source_org, region, url_hash, title, "
        "       title_hash, content_fp, published_at, fetched_at "
        "FROM articles "
        "WHERE datetime(COALESCE(published_at, fetched_at)) >= datetime('now', ?) "
        "ORDER BY COALESCE(published_at, fetched_at) DESC",
        (f"-{int(days)} days",),
    ).fetchall()


def build_clusters(
    rows: Sequence[sqlite3.Row],
    threshold: int = DEFAULT_THRESHOLD,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    candidates: int = DEFAULT_CANDIDATES,
    max_bucket: int = MAX_BUCKET_SIZE,
) -> Tuple[UnionFind, Dict[str, int]]:
    """返回 (并查集, 统计信息)。rows 的索引即并查集下标。"""
    # 分段桶剪枝的前置条件。
    #
    # 阶段 2 用「抽屉原理」剪枝：把 64 位 SimHash 切成 BANDS=4 段，
    # 只要海明距离 ≤ BANDS-1 = 3，就必有一段完全相同 → 一定分到同一个桶。
    # 但 threshold 是命令行可调参数（cli.py 暴露了 --threshold）。
    # 一旦传入 ≥4，距离 4~5 的重复对可能四段全不同 → 永不进桶 → **静默漏合并**，
    # 而模块文档仍宣称「结果与全量两两比较等价」。这里直接拦住。
    if int(threshold) > BANDS - 1:
        raise ValueError(
            f"threshold={threshold} 超过分段桶剪枝的保证范围（最大 {BANDS - 1}）。"
            f"请增大 BANDS 或调小 threshold；否则会静默漏掉重复事件。"
        )

    uf = UnionFind(len(rows))
    stats = {
        "articles": len(rows),
        "exact_merges": 0,
        "approx_merges": 0,
        "semantic_merges": 0,
        "candidate_pairs": 0,
        "semantic_pairs": 0,
        "skipped_buckets": 0,
    }

    # ---- 阶段 1：正文指纹精确合并 ----
    by_fp: Dict[str, List[int]] = defaultdict(list)
    for idx, row in enumerate(rows):
        fp = row["content_fp"]
        if fp:
            by_fp[fp].append(idx)
    for fp, idxs in by_fp.items():
        if len(idxs) < 2:
            continue
        head = idxs[0]
        for other in idxs[1:]:
            if uf.union(head, other):
                stats["exact_merges"] += 1

    # ---- 阶段 2：标题 SimHash 分段桶近似合并 ----
    hashes: List[Optional[int]] = []
    for row in rows:
        raw = row["title_hash"] or ""
        try:
            value = int(raw, 16)
        except (TypeError, ValueError):
            value = 0
        hashes.append(value if value else None)

    buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, value in enumerate(hashes):
        if value is None:
            continue
        for key in band_keys(value):
            buckets[key].append(idx)

    seen_pairs: set = set()
    for key, idxs in buckets.items():
        if len(idxs) < 2:
            continue
        if len(idxs) > max_bucket:
            stats["skipped_buckets"] += 1
            continue
        for i in range(len(idxs)):
            ai = idxs[i]
            ha = hashes[ai]
            if ha is None:
                continue
            for j in range(i + 1, len(idxs)):
                bj = idxs[j]
                pair_key = (ai, bj)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                hb = hashes[bj]
                if hb is None:
                    continue
                stats["candidate_pairs"] += 1
                if hamming64(ha, hb) <= threshold and uf.union(ai, bj):
                    stats["approx_merges"] += 1

    # ---- 阶段 3：语义近似（IDF 加权余弦）----
    # 抓"同一事件的不同措辞"：不同媒体对同一事件的标题写法不同，
    # SimHash 抓不到，但关键词重叠会很高。
    index = TitleSimilarityIndex(
        [(str(i), row["title"] or "") for i, row in enumerate(rows)]
    )
    for i in range(len(rows)):
        for j in index.candidates(i, topk=candidates):
            if j <= i:
                continue
            stats["semantic_pairs"] += 1
            if index.similarity(i, j) >= sim_threshold and uf.union(i, j):
                stats["semantic_merges"] += 1

    return uf, stats


def make_event_id(url_hashes: Iterable[str]) -> str:
    """事件 ID：簇内成员 URL 指纹排序后哈希，成员不变则 ID 稳定。"""
    joined = "|".join(sorted(h for h in url_hashes if h))
    if not joined:
        return ""
    return "evt_" + sha1(joined.encode("utf-8")).hexdigest()[:14]


def cluster_articles(
    conn: sqlite3.Connection,
    days: int = DEFAULT_WINDOW_DAYS,
    threshold: int = DEFAULT_THRESHOLD,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    verbose: bool = True,
) -> Dict[str, int]:
    """全量重算事件簇，回填 articles.event_id 与 cluster_size。"""
    backfilled = backfill_source_org(conn)
    if verbose and backfilled:
        print(f"  [CLUSTER] 回填机构字段 {backfilled} 行")
    rows = load_candidates(conn, days=days)
    if not rows:
        if verbose:
            print("  [CLUSTER] 窗口内没有文章，跳过")
        return {"articles": 0, "clusters": 0, "multi_source_clusters": 0}

    uf, stats = build_clusters(rows, threshold=threshold, sim_threshold=sim_threshold)

    groups: Dict[int, List[int]] = defaultdict(list)
    for idx in range(len(rows)):
        groups[uf.find(idx)].append(idx)

    updates: List[Tuple[str, int, int]] = []
    multi_source = 0
    multi_org = 0
    max_cluster = 0
    for members in groups.values():
        url_hashes = [rows[i]["url_hash"] for i in members]
        event_id = make_event_id(url_hashes)
        size = len(members)
        max_cluster = max(max_cluster, size)
        sources = {rows[i]["source_id"] for i in members}
        if len(sources) > 1:
            multi_source += 1
        orgs = {(rows[i]["source_org"] or rows[i]["source_id"]) for i in members}
        if len(orgs) > 1:
            multi_org += 1
        for i in members:
            updates.append((event_id, size, rows[i]["id"]))

    conn.executemany(
        "UPDATE articles SET event_id = ?, cluster_size = ? WHERE id = ?", updates
    )
    conn.commit()

    result = {
        "articles": len(rows),
        "clusters": len(groups),
        "multi_source_clusters": multi_source,
        "multi_org_clusters": multi_org,
        "max_cluster": max_cluster,
        "exact_merges": stats["exact_merges"],
        "approx_merges": stats["approx_merges"],
        "semantic_merges": stats["semantic_merges"],
        "candidate_pairs": stats["candidate_pairs"],
        "semantic_pairs": stats["semantic_pairs"],
        "skipped_buckets": stats["skipped_buckets"],
    }
    if verbose:
        naive = len(rows) * (len(rows) - 1) // 2
        print(
            f"  [CLUSTER] 文章 {result['articles']} -> 事件簇 {result['clusters']} "
            f"(多站点簇 {multi_source}，**跨机构簇 {multi_org}**，最大簇 {max_cluster})"
        )
        print(
            f"            合并: 精确 {result['exact_merges']} + 字面近似 "
            f"{result['approx_merges']} + 语义近似 {result['semantic_merges']} "
            f"(语义阈值 {sim_threshold:g})"
        )
        print(
            f"            剪枝: SimHash 候选对 {result['candidate_pairs']}、"
            f"语义候选对 {result['semantic_pairs']} / 朴素两两 {naive} "
            f"(合计剪到 {100.0 * (result['candidate_pairs'] + result['semantic_pairs']) / max(1, naive):.2f}%)"
        )
    return result


def cluster_sources(conn: sqlite3.Connection, event_id: str) -> List[sqlite3.Row]:
    """取一个事件簇下的全部文章，用于交叉印证展示与简报生成。"""
    return conn.execute(
        "SELECT * FROM articles WHERE event_id = ? "
        "ORDER BY COALESCE(published_at, fetched_at) DESC",
        (event_id,),
    ).fetchall()


def top_events(conn: sqlite3.Connection, limit: int = 20, min_orgs: int = 1,
               hours: Optional[float] = None) -> List[sqlite3.Row]:
    """按（印证机构数, 报道规模, 时间）倒序取事件列表。

    印证度按**机构**去重（见 config/orgs.yaml）—— 同一传媒集团下的
    多站点互转不算"多源印证"。sample_title 取标题字典序最小者，保证可复现。

    :param hours: 只保留「最新报道时间」落在最近 N 小时内的事件。
                  None 或 <=0 表示不限时窗。
                  判据用**最新**而非最早报道时间：一个持续多日的事件，
                  只要今天仍有新报道，它就是今天的热点，不该在第二天突然消失。

    返回行在原有列之外新增两列，供界面展示"信息来源"与"事件发生时间"：

    - ``earliest``：事件**首报时间**。
    - ``orgs``：参与印证的机构名（GROUP_CONCAT 拼接）。

    ``earliest`` 对脏数据做了防护。实测 868 篇带 ``published_at`` 的文章里，
    263 篇的该字段与采集时间相差 30 天以上（中国军网的 RSS 会把 2019 年的
    日期带出来）。若直接 ``MIN(published_at)``，这些事件在界面上会显示成
    "2019 年发生"。因此只有落在 ``fetched_at`` 之前 :data:`PUBLISHED_SANITY_DAYS`
    天内的 ``published_at`` 才被采信，否则回退到 ``fetched_at``。

    ``latest`` 沿用未做防护的 ``MAX(COALESCE(...))``：偏早的脏数据不会赢得 MAX，
    实测 609 个事件中 0 个受影响，且该口径与 :mod:`src.credibility` 的时间因子一致。
    """
    cutoff: Optional[str] = None
    if hours is not None and float(hours) > 0:
        cutoff = iso(utcnow() - timedelta(hours=float(hours)))
    return conn.execute(
        "SELECT articles.event_id, COUNT(*) AS n_articles, "
        "       COUNT(DISTINCT source_id) AS n_sources, "
        "       COUNT(DISTINCT source_org) AS n_orgs, "
        "       MIN(source_tier) AS best_tier, "
        "       MIN(title) AS sample_title, "
        "       MIN(COALESCE(NULLIF(title_zh, ''), title)) AS display_title, "
        "       t.tags AS keywords, "
        "       MAX(COALESCE(published_at, fetched_at)) AS latest, "
        "       MIN(" + SANE_TS_SQL + ") AS earliest, "
        "       GROUP_CONCAT(DISTINCT COALESCE(source_org, source_id)) AS orgs, "
        "       MAX(credibility) AS credibility, "
        # 事件级分区：取该事件下**出现次数最多**的主分区（众数）。
        # 一个事件可能跨分区（如"AI 用于军事"同时算科技与军事），
        # 但界面筛选需要一个单值，所以取众数而非"任一"。
        "       (SELECT a2.topic FROM articles a2 "
        "         WHERE a2.event_id = articles.event_id AND a2.topic <> '' "
        "         GROUP BY a2.topic ORDER BY COUNT(*) DESC, a2.topic LIMIT 1) AS topic "
        "FROM articles LEFT JOIN event_tags t ON t.event_id = articles.event_id "
        "WHERE articles.event_id != '' "
        "GROUP BY articles.event_id "
        "HAVING n_orgs >= ? "
        "  AND (? IS NULL OR MAX(COALESCE(published_at, fetched_at)) >= ?) "
        "ORDER BY n_orgs DESC, n_articles DESC, latest DESC LIMIT ?",
        (int(min_orgs), cutoff, cutoff, int(limit)),
    ).fetchall()
