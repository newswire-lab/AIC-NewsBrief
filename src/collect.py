"""采集编排：抓取 -> 解析 -> 归一化 -> 去重 -> 入库 -> 记录源健康。

去重策略（D1-D2 范围）：
- URL 归一化后完全重复  -> 丢弃（同一篇稿件被重复抓取）
- 标题 SimHash 近似重复 -> **保留**，因为"同一事件被几家源报道"是
  本作品多源交叉印证的基础数据，事件聚类留到 D3-D4 做。

礼貌抓取：串行、每源之间 sleep、支持 ETag/Last-Modified 条件请求。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple
import time

import requests

from . import dedup, parsers, parsers_html, store
from .config import Source
from .fetch import build_session, fetch
from .models import Article, SourceRun, iso, utcnow

DEFAULT_DELAY = 0.6        # 源之间间隔秒数
NEAR_DUP_WINDOW_DAYS = 7   # 近似重复比对的回溯窗口


def build_article(src: Source, item: parsers.RawItem) -> Optional[Article]:
    """把解析出的 RawItem 归一化成 Article。返回 None 表示该条不可用。"""
    url = (item.link or item.guid or "").strip()
    if not url.startswith("http"):
        return None
    title = (item.title or "").strip()
    if not title:
        return None

    return Article(
        source_id=src.id,
        source_name=src.name,
        source_org=src.org or src.id,
        source_tier=src.tier,
        region=src.region,
        lang=src.lang,
        url=url,
        url_hash=dedup.url_hash(url),
        title=title,
        norm_title=dedup.normalize_title(title),
        summary=(item.summary or "")[:2000],
        content=(item.content or "")[:20000],
        published_at=iso(item.published),
        fetched_at=iso(utcnow()),
        title_hash=dedup.simhash_hex(dedup.title_simhash(title)),
        content_fp=dedup.text_fingerprint(item.content or item.summary),
    )



# --------------------------------------------------------------------------
# 补正文：列表页 / RSS 只给标题时，抓一次详情页
#
# **为什么需要**（2026-10-15 实测）：全库 2471 篇里 175 篇正文完全为空、
# 84 篇不足 40 字，且高度集中在两类源：
#   - ``kind: html`` 的源（国防部两个栏目共 88 篇，100% 为空）——
#     它们只从列表页拿到标题与链接，parse_list_page 不返回正文；
#   - 部分 RSS（Global Times 41 篇，100% 为空）—— RSS 本身不带 description。
# 用户看到的现象是"简报只有一句话"，根因却在采集层：
# 送给模型的材料本身就只有标题。
#
# 用"正文不足才补抓"而不是"每条都抓"，是为了控成本：
# 多数源的 RSS 已带正文，补抓只发生在真正缺的地方，且每源每轮有上限。
# --------------------------------------------------------------------------
MIN_BODY_CHARS = 120            # 低于此认为"没有正文"，需要补抓
MAX_BODY_FETCH_PER_SOURCE = 8   # 每源每轮最多补抓几条（礼貌 + 控制时长）
BODY_FETCH_DELAY_S = 0.3        # 补抓之间的间隔


def _enrich_bodies(items, sess, src, limit: int = MAX_BODY_FETCH_PER_SOURCE):
    """对正文过短的条目抓详情页补正文（原地修改并返回 items）。"""
    if not items:
        return items
    done = 0
    for it in items:
        if done >= limit:
            break
        cur = str(getattr(it, "content", "") or "").strip()
        url = str(getattr(it, "url", "") or "").strip()
        if len(cur) >= MIN_BODY_CHARS or not url:
            continue
        try:
            res = fetch(url, session=sess)
            raw = getattr(res, "content", None)
            if not raw:
                continue
            body = parsers_html.extract_body(
                parsers.decode_bytes(raw, getattr(res, "encoding", None))
            )
        except Exception:
            continue
        if len(body) <= len(cur):
            continue
        try:
            it.content = body
        except Exception:           # 万一 RawItem 不可变，也不要让采集挂掉
            continue
        done += 1
        time.sleep(BODY_FETCH_DELAY_S)
    return items


def collect_source(
    conn,
    src: Source,
    session: Optional[requests.Session] = None,
    near_dup_pool: Optional[List[Tuple[str, int]]] = None,
    verbose: bool = True,
) -> SourceRun:
    """采集单个源。返回运行记录。"""
    sess = session or build_session()
    run = SourceRun(source_id=src.id, status="ok", run_at=iso(utcnow()) or "")

    state = store.get_state(conn, src.id)
    validators: Optional[Dict[str, str]] = None
    if state is not None and (state["etag"] or state["last_modified"]):
        validators = {"etag": state["etag"], "last_modified": state["last_modified"]}

    res = fetch(src.url, session=sess, validators=validators)
    run.http_status = res.status
    run.elapsed_ms = res.elapsed_ms

    if res.not_modified:
        run.status = "not_modified"
        store.record_run(conn, run)
        store.update_state(conn, src.id, status=run.status, ok=True)
        if verbose:
            print(f"  [304] {src.label} 未更新 ({res.elapsed_ms}ms)")
        return run

    if not res.ok:
        run.status = "network_error" if res.status is None else "http_error"
        run.error = res.error[:300]
        store.record_run(conn, run)
        store.update_state(conn, src.id, status=run.status, ok=False)
        if verbose:
            print(f"  [FAIL] {src.label} {run.status}: {run.error}")
        return run

    if src.kind == "html":
        page_text = parsers.decode_bytes(res.content, res.encoding)
        items = parsers_html.parse_list_page(
            page_text, src.url, max_items=max(1, src.max_items)
        )
        feed_title = src.name
    else:
        items, feed_title = parsers.parse_feed(res.content, res.encoding)

    # 列表页/RSS 只给标题时补抓详情页正文（见 _enrich_bodies 的说明）
    items = _enrich_bodies(items, sess, src)
    run.item_count = len(items)

    if not items:
        run.status = "empty"
        run.error = "解析出 0 条：源格式变更，或该页面无可识别的列表结构"
        store.record_run(conn, run)
        store.update_state(conn, src.id, status=run.status, ok=False,
                           etag=res.etag, last_modified=res.last_modified)
        if verbose:
            print(f"  [EMPTY] {src.label} 解析出 0 条 ({res.elapsed_ms}ms)")
        return run

    near_dup_hits = 0
    for item in items[: max(1, src.max_items)]:
        art = build_article(src, item)
        if art is None:
            continue
        if store.exists_url_hash(conn, art.url_hash):
            run.dup_count += 1
            continue

        # 近似重复检测：命中不丢弃，只统计（D3 聚类用）
        if near_dup_pool is not None and art.title_hash:
            _, dist = dedup.find_near_dup(int(art.title_hash, 16), near_dup_pool)
            if dist >= 0:
                near_dup_hits += 1

        if store.insert_article(conn, art):
            run.new_count += 1
            if near_dup_pool is not None and art.title_hash:
                near_dup_pool.append((art.url_hash, int(art.title_hash, 16)))

    conn.commit()
    store.record_run(conn, run)
    store.update_state(
        conn, src.id, status=run.status, ok=True,
        etag=res.etag, last_modified=res.last_modified,
        item_delta=run.item_count, new_delta=run.new_count,
    )
    if verbose:
        tip = f" 近重复 {near_dup_hits}" if near_dup_hits else ""
        print(
            f"  [OK] {src.label} 条目 {run.item_count} 新增 {run.new_count} "
            f"URL重复 {run.dup_count}{tip} ({res.elapsed_ms}ms)"
        )
    return run


def collect_all(
    conn,
    sources: Sequence[Source],
    delay: float = DEFAULT_DELAY,
    verbose: bool = True,
) -> List[SourceRun]:
    """顺序采集多个源。"""
    session = build_session()
    # 近似重复池：只加载一次，采集过程中增量追加
    pool = store.recent_title_hashes(conn, days=NEAR_DUP_WINDOW_DAYS)
    runs: List[SourceRun] = []
    for i, src in enumerate(sources):
        if src.kind not in ("rss", "html"):
            if verbose:
                print(f"  [SKIP] {src.label} 未知 kind={src.kind}")
            continue
        runs.append(collect_source(conn, src, session=session,
                                   near_dup_pool=pool, verbose=verbose))
        if delay and i < len(sources) - 1:
            time.sleep(delay)
    return runs


def summarize(runs: Sequence[SourceRun]) -> Dict[str, int]:
    agg = {"sources": len(runs), "items": 0, "new": 0, "dup": 0,
           "failed": 0, "empty": 0, "not_modified": 0}
    for r in runs:
        agg["items"] += r.item_count
        agg["new"] += r.new_count
        agg["dup"] += r.dup_count
        if r.status in ("http_error", "network_error"):
            agg["failed"] += 1
        elif r.status == "empty":
            agg["empty"] += 1
        elif r.status == "not_modified":
            agg["not_modified"] += 1
    return agg
