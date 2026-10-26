"""命令行入口。

用法（在项目根目录执行）：
    python -m src.cli init                      初始化数据库
    python -m src.cli health                    源连通性体检（不写库）
    python -m src.cli health --all              含 enabled=false 的源
    python -m src.cli collect                   采集全部启用的源（增量去重）
    python -m src.cli collect -s cn81_rss
    python -m src.cli cluster                   事件聚类（回填 event_id）
    python -m src.cli score                     可信度打分
    python -m src.cli pipeline                  采集 -> 聚类 -> 打分（每日任务）
    python -m src.cli events --min-sources 2    查看多源印证的热点事件
    python -m src.cli explain <event_id>        查看某个事件的评分依据
    python -m src.cli list --hours 24 --limit 30
    python -m src.cli stats
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional, Sequence

from . import (__version__, brief, cluster, collect, config, credibility, keywords,
               llm, normalize, parsers, parsers_html, search, store, topics, verify)
from .config import Source
from .fetch import build_session, fetch

RULE = "=" * 78
SUB = "-" * 78


def _truncate(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _local(ts: Optional[str]) -> str:
    """把库里的 UTC ISO 时间转成本地时间展示。"""
    if not ts:
        return "无时间"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%m-%d %H:%M")


def _select_sources(args) -> List[Source]:
    sources = config.load_sources(only_enabled=not getattr(args, "all", False))
    wanted = getattr(args, "source", None)
    if wanted:
        want = set(wanted)
        sources = [s for s in sources if s.id in want]
        missing = want - {s.id for s in sources}
        for m in sorted(missing):
            print(f"[WARN] 未匹配到数据源 id: {m}")
    return sources


def cmd_init(args) -> int:
    config.ensure_dirs()
    conn = store.connect()
    store.init_db(conn)
    all_sources = config.load_sources(only_enabled=False)
    enabled = [s for s in all_sources if s.enabled]
    rss = [s for s in enabled if s.kind == "rss"]
    html = [s for s in enabled if s.kind == "html"]
    print(f"数据库     : {config.DB_FILE}")
    print(f"源配置      : {config.SOURCES_FILE}")
    print(f"源定义总数  : {len(all_sources)}  (启用 {len(enabled)}: rss {len(rss)} / html {len(html)})")
    return 0


def cmd_health(args) -> int:
    config.ensure_dirs()
    sources = _select_sources(args)
    if not sources:
        print("[ERROR] 没有匹配的数据源")
        return 2

    session = build_session()
    results = []
    for i, src in enumerate(sources):
        res = fetch(src.url, session=session)
        if not res.ok:
            results.append((src, "FAIL", res.status, 0, res.error, res.elapsed_ms))
        else:
            if src.kind == "html":
                page = parsers.decode_bytes(res.content, res.encoding)
                items = parsers_html.parse_list_page(
                    page, src.url, max_items=max(1, src.max_items)
                )
                note = "html 列表页抽取"
            else:
                items, feed_title = parsers.parse_feed(res.content, res.encoding)
                note = f"feed={feed_title}" if feed_title else "no feed title"
            status = "OK" if items else "EMPTY"
            results.append((src, status, res.status, len(items), note, res.elapsed_ms))
        if i < len(sources) - 1:
            time.sleep(0.4)

    print(RULE)
    print(f"源健康体检  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(RULE)
    print(f"{'状态':<6} {'条目':>5} {'HTTP':>5} {'耗时ms':>7}  {'kind':<5} {'id':<16} 源名称")
    print(SUB)
    for src, status, http, count, note, ms in results:
        print(f"{status:<6} {count:>5} {str(http or '-'):>5} {ms:>7}  "
              f"{src.kind:<5} {src.id:<16} {src.name}")
    print(SUB)

    ok = [r for r in results if r[1] == "OK"]
    empty = [r for r in results if r[1] == "EMPTY"]
    fail = [r for r in results if r[1] == "FAIL"]
    print(f"可用 {len(ok)} / 空 {len(empty)} / 失败 {len(fail)} / 合计 {len(results)}")
    print(f"可用源产出条目合计: {sum(r[3] for r in ok)}")

    if fail:
        print("\n[建议] 以下源不可达，把 sources.yaml 中对应项的 enabled 改为 false：")
        for src, *_ in fail:
            print(f"  - {src.id}")
    if empty:
        print("\n[注意] 以下源可达但解析不出条目（格式变更或非列表页）：")
        for src, *_ in empty:
            print(f"  - {src.id}  {src.url}")
    return 0


def cmd_collect(args) -> int:
    config.ensure_dirs()
    conn = store.connect()
    store.init_db(conn)

    sources = _select_sources(args)
    if not sources:
        print("[ERROR] 没有匹配的数据源")
        return 2

    print(RULE)
    print(f"开始采集  {len(sources)} 个源  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(RULE)
    runs = collect.collect_all(conn, sources, delay=args.delay, verbose=True)
    agg = collect.summarize(runs)

    print(SUB)
    print(
        f"完成: 源 {agg['sources']} / 条目 {agg['items']} / 新增 {agg['new']} / "
        f"URL重复 {agg['dup']} / 失败 {agg['failed']} / 空 {agg['empty']} / "
        f"未更新 {agg['not_modified']}"
    )
    return 0


def cmd_cluster(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    print(RULE)
    print("事件聚类（阶段 B：自研算法一）")
    print(RULE)
    cluster.cluster_articles(
        conn, days=args.days, threshold=args.threshold,
        sim_threshold=args.sim_threshold, verbose=True,
    )
    return 0


def cmd_score(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    print(RULE)
    print("可信度打分（阶段 B：自研算法二）")
    print(RULE)
    credibility.score_all(conn, tau=args.tau, verbose=True)
    return 0


def cmd_tag(args) -> int:
    """提炼事件关键词标签（自研算法，无需模型）。"""
    conn = store.connect()
    store.init_db(conn)
    print(RULE)
    print("事件关键词标签（自研频繁短语挖掘 + IDF）")
    print(RULE)
    keywords.tag_all(conn, topn=getattr(args, "topn", 4),
                     days=getattr(args, "days", None), verbose=True)
    return 0


def cmd_topics(args) -> int:
    """判定新闻分区（自研关键词加权分类，无需模型）。"""
    conn = store.connect()
    store.init_db(conn)
    print(RULE)
    print("新闻分区（政治 / 军事 / 经济 / 科技 / 文化 / 体育 / 民生 / 国际 / 法治 / 社会 / 历史）")
    print(RULE)
    only_missing = not getattr(args, "refresh", False)
    topics.classify_all(conn, verbose=True, only_missing=only_missing)
    return 0


def cmd_pipeline(args) -> int:
    """每日任务：采集 -> 聚类 -> 打分。"""
    rc = cmd_collect(args)
    if rc != 0:
        return rc
    args.days = getattr(args, "days", 14)
    args.threshold = getattr(args, "threshold", 3)
    args.sim_threshold = getattr(args, "sim_threshold", 0.5)
    args.tau = getattr(args, "tau", 72.0)
    rc = cmd_cluster(args)
    if rc != 0:
        return rc
    rc = cmd_score(args)
    if rc != 0:
        return rc
    # ⚠️ tag 必须拿「不限时窗」的 args。
    #
    # 上面的 ``args.days = 14`` 是给**聚类**用的（只重算近 14 天的簇）。
    # 若把这个 args 直接传给 cmd_tag，``tag_all(days=14)`` 会只给 14 天内的事件
    # 重建标签 —— 而 tag_all 的第一件事是 ``DELETE FROM event_tags`` 清空整表。
    # 于是窗口外的历史事件标签被删掉却不再生成。
    # 实测（2026-10-22 录制彩排）：3219 篇文章里 1147 篇落进"有 event_id 但无标签"
    # 的状态，界面上「事件关键词」列整列变空，且新词发现语料从 2698 篇缩到 1592 篇、
    # 词表从 3215 条掉到 1720 条。
    tag_args = argparse.Namespace(**vars(args))
    tag_args.days = None
    rc = cmd_tag(tag_args)
    if rc != 0:
        return rc
    # 分区必须跟在 tag 之后：新采的文章默认 topic=''，
    # 不重算的话界面上「分区」筛选会对新稿子完全失效。
    return cmd_topics(args)


def cmd_events(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    rows = cluster.top_events(conn, limit=args.limit, min_orgs=args.min_orgs,
                             hours=getattr(args, "hours", None))
    if not rows:
        print("[空] 还没有聚类结果，先跑 python -m src.cli cluster")
        return 0

    print(RULE)
    print(f"热点事件（按印证机构数排序，共 {len(rows)} 个）")
    print(RULE)
    for i, row in enumerate(rows, 1):
        detail = credibility.explain(conn, row["event_id"])
        score = detail.get("credibility", 0.0)
        label = detail.get("label", "")
        orgs = "、".join(detail.get("orgs", []))
        print(f"{i:>3}. [{score:>5.1f}分 {label}] "
              f"{row['n_orgs']} 机构印证（{row['n_sources']} 站点 / {row['n_articles']} 篇）")
        print(f"     {_truncate(row['display_title'] or row['sample_title'], 62)}")
        if row["keywords"]:
            print(f"     关键词: {_truncate(row['keywords'], 62)}")
        print(f"     首报 {_local(row['earliest'])}"
              f" ｜ 最新 {_local(row['latest'])}")
        print(f"     机构: {_truncate(orgs, 62)}")
    return 0


def cmd_search(args) -> int:
    """关键词检索近期新闻（纯本地 SQL + 自研排序，不调模型）。"""
    conn = store.connect()
    store.init_db(conn)
    terms = search.parse_query(args.query)
    rows = search.search_articles(conn, args.query, hours=args.hours, limit=args.limit)
    if not terms:
        print("[提示] 请给一个关键词，例如：python -m src.cli search 诺贝尔")
        return 2

    cov = search.coverage(conn)
    window = "全部历史" if not args.hours else f"最近 {args.hours} 小时"
    print(RULE)
    print(f"检索「{' '.join(terms)}」  ({window}，命中 {len(rows)} 条，最多展示 {args.limit})"
          f"    库存 {cov['total']} 篇，其中外文/需翻译 {cov['foreign_like']} 篇"
          f"、已有中文译名 {cov['translated']} 篇")
    print(RULE)
    if not rows:
        print("[空] 没有命中。可试着减少关键词，或把 --hours 改成 0（不限时窗）。")
        return 0
    for i, r in enumerate(rows, 1):
        mark = "译" if r["translated"] else "原"
        same = f"  [{r['dup_sites']} 站同稿]" if r["dup_sites"] > 1 else ""
        print(f"{i:>3}. [{r['score']:>5.2f}分] [{mark}] [{r['source_org']}] "
              f"{_local(r['ts'])}  {r['credibility']:.0f}分{same}")
        print(f"     {_truncate(r['title'], 66)}")
        if r["title_orig"] and r["translated"]:
            print(f"     原文: {_truncate(r['title_orig'], 60)}")
        if r["snippet"]:
            print(f"     片段: {_truncate(r['snippet'], 62)}")
        print(f"     {r['url']}")
    return 0


def cmd_explain(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    info = credibility.explain(conn, args.event_id)
    if not info:
        print(f"[空] 找不到事件 {args.event_id}")
        return 1

    print(RULE)
    print(f"事件 {info['event_id']}  可信度 {info['credibility']} 分  {info['label']}")
    print(RULE)
    f = info["factors"]
    print(f"评分模型: 100 x f_tier({f['f_tier']}) x f_time({f['f_time']}) x f_cross({f['f_cross']})")
    print(f"  来源等级最高 tier={f['best_tier']}   印证机构数={f['n_orgs']}   "
          f"距最新报道 {f['age_hours']} 小时")
    print(f"  参与印证的机构: {'、'.join(info['orgs'])}")
    print(SUB)
    print(f"同事件报道 {info['n_articles']} 篇（{info['n_sources']} 个站点）：")
    for s in info["sources"]:
        print(f"  tier{s['tier']} [{_local(s['time'])}] {s['source_name']} / {s['source_org']}")
        print(f"      {_truncate(s['title'], 60)}")
        print(f"      {s['url']}")
    return 0


def cmd_list(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    rows = store.recent_articles(conn, hours=args.hours, limit=args.limit, region=args.region)
    if not rows:
        print(f"[空] 最近 {args.hours} 小时内没有入库文章")
        return 0

    print(RULE)
    print(f"最近 {args.hours} 小时文章  (共 {len(rows)} 条)")
    print(RULE)
    for i, r in enumerate(rows, 1):
        tag = f"{r['credibility']:.0f}分" if r["credibility"] else "--"
        print(f"{i:>3}. [{_local(r['published_at'] or r['fetched_at'])}] "
              f"[{r['source_name']}] [{tag}]")
        print(f"     {_truncate(r['title'], 64)}")
        print(f"     {r['url']}")
    return 0


def cmd_stats(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    s = store.stats(conn)
    print(RULE)
    print("入库统计")
    print(RULE)
    print(f"文章总数   : {s['total']}")
    print(f"最近 24h   : {s['last24']}")
    print(f"最近 7d    : {s['last7']}")

    events = conn.execute(
        "SELECT COUNT(DISTINCT event_id) AS n FROM articles WHERE event_id != ''"
    ).fetchone()["n"]
    multi = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT event_id FROM articles WHERE event_id != '' "
        "  GROUP BY event_id HAVING COUNT(DISTINCT source_org) > 1)"
    ).fetchone()["n"]
    scored = conn.execute(
        "SELECT COUNT(*) AS n FROM articles WHERE credibility > 0"
    ).fetchone()["n"]
    print(f"事件簇数   : {events}  (其中多机构印证 {multi})")
    print(f"已打分文章 : {scored}")
    print(SUB)
    print("按地区：")
    for r in s["by_region"]:
        print(f"  {r['region']:<6} {r['c']:>6}")
    print(SUB)
    print("按来源：")
    for r in s["by_source"]:
        print(f"  {r['source_name']:<20} {r['region']:<5} {r['c']:>6}   最近 {_local(r['latest'])}")
    return 0


def _print_brief(result) -> None:
    print(f"来源     : {result.get('n_orgs')} 个机构 / {result.get('n_sources')} 个站点 / "
          f"{result.get('n_articles')} 篇")
    print(f"生成后端 : {result.get('backend')}   模型: {result.get('model')}   "
          f"耗时 {result.get('elapsed_ms')} ms")
    if result.get("_fallback"):
        print(f"[降级] 已改为抽取式兜底：{result.get('_fallback_reason', '')}")
        before = result.get("verification_before_fallback")
        if before:
            print("降级前的校验报告（定位失败原因）：")
            for line in verify.format_report(before).splitlines():
                print("      " + line)
    if result.get("_expanded"):
        print(f"[扩写] 首次生成 {result.get('brief_chars_before_expand')} 字偏短，"
              f"已定向扩写至 {len(result.get('brief') or '')} 字"
              f"（追加 {result.get('_expand_ms')} ms）")
    print(SUB)
    print(f"标题   : {result.get('headline')}")
    print(f"价值点 : {result.get('value_point')}")
    print(SUB)
    print(result.get("brief") or "（简报为空）")
    print(SUB)
    print("信息来源：")
    for src in result.get("sources") or []:
        print(f"  [{src['index']}] {src['source_name']}（{src['source_org']}，tier{src['tier']}）")
        print(f"      {_truncate(src['title'], 58)}")
        print(f"      {src['url']}")
    print(SUB)
    print(verify.format_report(result.get("verification") or {}))
    if result.get("conflicts"):
        print("来源分歧：")
        for item in result["conflicts"]:
            print(f"  - {item}")


def cmd_normalize(args) -> int:
    """把外文材料归一化为中文事实要点（生成与校验的前置步骤）。"""
    conn = store.connect()
    store.init_db(conn)
    rows = cluster.top_events(conn, limit=args.limit, min_orgs=args.min_orgs)
    if not rows:
        print("[空] 没有可用事件，先跑 python -m src.cli pipeline")
        return 0

    cfg = llm.load_llm_config()
    backend = (llm.build_backend(cfg, kind=args.backend) if args.backend
               else llm.get_backend(cfg))
    print(RULE)
    print(f"跨语言归一化  {len(rows)} 个事件   后端={backend.name}")
    print(RULE)
    total = 0
    for i, row in enumerate(rows, 1):
        print(f"  {i:>3}. {_truncate(row['sample_title'], 44)}")
        n = normalize.normalize_event(conn, row["event_id"], backend=backend, cfg=cfg)
        total += n
        if n == 0:
            print("        （无需归一化，或已缓存）")
    print(SUB)
    print(f"完成：共归一化 {total} 篇外文材料（已缓存，后续生成不再重复调用）")
    return 0


def cmd_brief(args) -> int:
    conn = store.connect()
    store.init_db(conn)

    event_id = args.event_id
    if not event_id:
        rows = cluster.top_events(conn, limit=1, min_orgs=args.min_orgs)
        if not rows:
            print("[空] 没有可用事件，先跑 python -m src.cli pipeline")
            return 0
        event_id = rows[0]["event_id"]

    cfg = llm.load_llm_config()
    backend = llm.build_backend(cfg, kind=args.backend) if args.backend else None

    print(RULE)
    print(f"生成简报  event={event_id}")
    print(RULE)
    result = brief.generate_brief(conn, event_id, backend=backend, cfg=cfg)
    if result.get("error"):
        print(f"[ERROR] {result['error']}")
        return 1

    _print_brief(result)
    if not args.no_save:
        store.save_brief(conn, result)
        print(SUB)
        print("已保存到数据库（briefs 表）")
    return 0


def cmd_briefs(args) -> int:
    conn = store.connect()
    store.init_db(conn)
    rows = store.recent_briefs(conn, limit=args.limit)
    if not rows:
        print("[空] 还没有已保存的简报，先跑 python -m src.cli brief")
        return 0
    print(RULE)
    print(f"已保存简报（{len(rows)} 条，按时间倒序）")
    print(RULE)
    for i, row in enumerate(rows, 1):
        # 降级过的简报，其 backend 会被置为 extractive；据此把两类结果分开显示，
        # 避免把「模型直出失败后换成了抽取式摘要」也念成「校验通过」。
        degraded = str(row["backend"]) == "extractive"
        flag = "降级为抽取式" if degraded else ("通过" if row["accepted"] else "未通过")
        print(f"{i:>3}. [{row['credibility']:>5.1f}分][{flag}] {row['headline']}")
        print(f"     {_truncate(row['value_point'], 56)}")
        print(f"     {row['backend']} / {row['model']}   {_local(row['created_at'])}")
        print(f"     {row['event_id']}")
    return 0


def cmd_daily(args) -> int:
    """为当前排名靠前的事件批量生成简报。"""
    conn = store.connect()
    store.init_db(conn)
    rows = cluster.top_events(conn, limit=args.limit, min_orgs=args.min_orgs)
    if not rows:
        print("[空] 没有可用事件，先跑 python -m src.cli pipeline")
        return 0

    cfg = llm.load_llm_config()
    backend = llm.build_backend(cfg, kind=args.backend) if args.backend else None
    print(RULE)
    print(f"批量生成简报  {len(rows)} 个事件   后端={backend.name if backend else cfg['backend']}")
    print(RULE)

    ok = 0
    fallback = 0
    for i, row in enumerate(rows, 1):
        result = brief.generate_brief(conn, row["event_id"], backend=backend, cfg=cfg)
        if result.get("error"):
            print(f"  {i:>3}. [跳过] {result['error']}")
            continue
        ver = result.get("verification") or {}
        # 降级时 verification 描述的是**抽取式产物**（摘自原文，必然通过），
        # 模型自身的真实表现存在 verification_before_fallback。
        # 不区分这两者就会把降级行显示成「通过」，掩盖模型的失败。
        pre = result.get("verification_before_fallback") or ver
        is_fallback = bool(result.get("_fallback"))
        model_ok = bool(ver.get("accepted")) and not is_fallback
        flag = "一次成型" if model_ok else "降级为抽取式"
        store.save_brief(conn, result)
        ok += 1 if model_ok else 0
        fallback += 1 if is_fallback else 0
        print(f"  {i:>3}. [{flag}] {result.get('headline')}  "
              f"（{result.get('n_orgs')} 机构，{result.get('elapsed_ms')}ms，"
              f"依据句 {pre.get('n_supported')}/{pre.get('n_claims')}"
              f"（{pre.get('supported_ratio', 0):.0%}））")
    print(SUB)
    print(f"完成：一次成型 {ok} / 共 {len(rows)}；降级为抽取式 {fallback} 条")
    return 0


def cmd_ready(args) -> int:
    """列出「素材充足、适合录演示」的事件。

    **为什么需要这个命令**（2026-10-15 决定）：模型稿要过事实校验（默认支撑率 80%），
    材料太薄的事件必然写不出达标简报、只能降级成抽取式摘要 —— 演示时很难看。
    与其下调校验阈值（那会牺牲可信度，而可信度正是本项目最该守住的东西），
    不如把「哪些事件材料够」做成可查询的清单，演示时从清单里挑。

    口径与真正生成简报**完全一致**：直接调用 :func:`brief.build_material`，
    所以这里报的"材料字数"就是模型实际会看到的字数，
    "预估字数"就是 :func:`brief.adaptive_brief_chars` 给出的目标字数。
    """
    conn = store.connect()
    store.init_db(conn)
    cfg = llm.load_llm_config()
    base = int(cfg["task"].get("brief_chars", 700))
    max_src = int(cfg["task"].get("max_source_chars", 6000))

    events = cluster.top_events(
        conn, limit=max(1, args.scan), min_orgs=args.min_orgs,
        hours=(args.hours if args.hours and args.hours > 0 else None),
    )
    if not events:
        print("[!] 没有事件。先跑 python -m src.cli pipeline")
        return 0

    rows = []
    for ev in events:
        eid = ev["event_id"]
        material, sources = brief.build_material(
            conn, eid, max_total_chars=max_src, backend=None, cfg=cfg
        )
        facts = brief.fact_chars(material)
        target = brief.adaptive_brief_chars(material, base)
        no_body = conn.execute(
            "SELECT COUNT(*) AS c FROM articles WHERE event_id = ? "
            "AND length(COALESCE(content, '')) < 120", (eid,)
        ).fetchone()["c"]
        rows.append({
            "event_id": eid,
            "title": ev["display_title"] or ev["sample_title"] or eid,
            "topic": ev["topic"] or "",
            "n_orgs": ev["n_orgs"],
            "n_articles": ev["n_articles"],
            "n_sources": ev["n_sources"],
            "n_sources_used": len(sources),
            "facts": facts,
            "target": target,
            "no_body": no_body,
            "orgs": ev["orgs"] or "",
            "latest": ev["latest"],
            "credibility": ev["credibility"],
        })

    rows.sort(key=lambda r: (-r["facts"], -r["n_orgs"], -r["n_articles"]))
    shown = rows[: max(1, args.top)]

    print(RULE)
    print("演示备选事件（按材料充足度排序）"
          f"  ｜ 扫描 {len(rows)} 个 · 展示 {len(shown)} 个")
    print(RULE)
    print(f"{'#':>3}  {'机构':>4} {'文章':>4} {'材料字数':>8} {'预估字数':>8} "
          f"{'缺正文':>6}  {'评价':<8} 事件标题")
    print(SUB)
    good = []
    for i, r in enumerate(shown, 1):
        if r["facts"] >= 900:
            mark = "充足"
            good.append(r)
        elif r["facts"] >= 300:
            mark = "偏薄"
        else:
            mark = "不足"
        print(f"{i:>3}  {r['n_orgs']:>4} {r['n_articles']:>4} {r['facts']:>8} "
              f"{r['target']:>8} {r['no_body']:>6}  {mark:<4} "
              f"{_truncate(r['title'], 34)}")

    n_good = sum(1 for r in shown if r["facts"] >= 900)
    n_mid = sum(1 for r in shown if 300 <= r["facts"] < 900)
    n_bad = sum(1 for r in shown if r["facts"] < 300)
    print(SUB)
    print(f"充足 {n_good} 个 ｜ 偏薄 {n_mid} 个 ｜ 不足 {n_bad} 个 ｜ "
          f"参考下限 {brief.LOW_MATERIAL_FACTS} 字（低于此走抽取式兜底）")
    if n_bad:
        print("提示：'不足'的事件多半是正文没抓到 → 跑 "
              "python scripts/backfill_bodies.py 补正文后会改善")

    if good:
        print()
        print("推荐直接拿这几个录演示：")
        for r in good[:5]:
            print(f"  {_truncate(r['title'], 40)}")
            print(f"    {r['n_orgs']} 家独立机构 · 材料 {r['facts']} 字 · "
                  f"预估 {r['target']} 字")
            print(f"    python -m src.cli brief {r['event_id']} --no-save")

    if args.json:
        import json
        print()
        print(json.dumps(shown, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="AIC-NewsBrief: 多源公开新闻聚合与可溯源简报生成",
    )
    p.add_argument("--version", action="version", version=f"AIC-NewsBrief {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="初始化数据库与目录")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("health", help="源连通性与可解析性体检（不写库）")
    sp.add_argument("--all", action="store_true", help="包含 enabled=false 的源")
    sp.add_argument("-s", "--source", action="append", help="只体检指定源 id，可重复")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("collect", help="采集入库（增量去重）")
    sp.add_argument("--all", action="store_true", help="包含 enabled=false 的源")
    sp.add_argument("-s", "--source", action="append", help="只采集指定源 id，可重复")
    sp.add_argument("--delay", type=float, default=0.6, help="源之间的间隔秒数，默认 0.6")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("cluster", help="事件聚类（回填 event_id / cluster_size）")
    sp.add_argument("--days", type=int, default=14, help="参与聚类的回溯天数，默认 14")
    sp.add_argument("--threshold", type=int, default=3, help="标题 SimHash 海明距离阈值，默认 3")
    sp.add_argument("--sim-threshold", type=float, default=0.5,
                    help="语义相似度阈值（IDF 加权余弦），默认 0.5")
    sp.set_defaults(func=cmd_cluster)

    sp = sub.add_parser("score", help="可信度打分（写入 credibility）")
    sp.add_argument("--tau", type=float, default=72.0, help="时效衰减时间常数（小时），默认 72")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("pipeline", help="每日任务：采集 -> 聚类 -> 打分")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("-s", "--source", action="append")
    sp.add_argument("--delay", type=float, default=0.6)
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--threshold", type=int, default=3)
    sp.add_argument("--sim-threshold", type=float, default=0.5)
    sp.add_argument("--tau", type=float, default=72.0)
    sp.set_defaults(func=cmd_pipeline)

    sp = sub.add_parser("events", help="查看热点事件簇")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--min-orgs", type=int, default=2, help="最少印证机构数，默认 2")
    sp.add_argument("--hours", type=float, default=24,
                    help="只显示最近 N 小时内仍有新报道的事件；0 表示不限")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("explain", help="查看某个事件的评分依据与来源清单")
    sp.add_argument("event_id")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("normalize", help="把外文材料归一化为中文事实要点")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--min-orgs", type=int, default=1)
    sp.add_argument("--backend", choices=["local", "api"], default=None)
    sp.set_defaults(func=cmd_normalize)

    sp = sub.add_parser("brief", help="为一个事件生成可溯源简报")
    sp.add_argument("event_id", nargs="?", help="事件 id；省略则取排名第一的事件")
    sp.add_argument("--backend", choices=["local", "api"], default=None,
                    help="覆盖 config/llm.yaml 的后端设置")
    sp.add_argument("--min-orgs", type=int, default=1)
    sp.add_argument("--no-save", action="store_true", help="只打印，不写入数据库")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("daily", help="为排名靠前的多个事件批量生成简报")
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--min-orgs", type=int, default=1)
    sp.add_argument("--backend", choices=["local", "api"], default=None)
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("briefs", help="查看已保存的简报")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_briefs)

    sp = sub.add_parser("list", help="查看最近入库文章")
    sp.add_argument("--hours", type=int, default=24)
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--region", choices=["cn", "intl"], default=None)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("stats", help="入库统计")
    # 注意：每个子命令必须紧跟自己的 set_defaults(func=...)。
    # 曾经 stats 忘了设置、而末尾又有一个多余的 set_defaults(func=cmd_stats)，
    # 结果 ①`stats` 子命令因为没有 func 属性直接崩；
    #      ②那个多余的 set_defaults 落在后注册的 `tag` 上，把它覆盖成了 cmd_stats。
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("search", help="关键词检索近期新闻")
    sp.add_argument("query", help="关键词，多个词用空格分隔（全部命中才算命中）")
    sp.add_argument("--hours", type=int, default=0, help="时间窗口小时数，0=不限（默认）")
    sp.add_argument("--limit", type=int, default=20, help="最多展示条数")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("tag", help="提炼事件关键词标签（纯自研，无需模型）")
    sp.add_argument("--topn", type=int, default=4, help="每个事件保留几个标签")
    sp.add_argument("--days", type=int, default=None, help="只处理最近 N 天的事件")
    sp.set_defaults(func=cmd_tag)

    sp = sub.add_parser("topics", help="判定新闻分区（纯自研，无需模型）")
    sp.add_argument("--refresh", action="store_true",
                    help="全量重算（默认只补 topic 为空的新文章）")
    sp.set_defaults(func=cmd_topics)

    sp = sub.add_parser("ready", help="列出素材充足、适合录演示的事件")
    sp.add_argument("--scan", type=int, default=40, help="先按热度扫描多少个事件")
    sp.add_argument("--top", type=int, default=12, help="展示前 N 个")
    sp.add_argument("--min-orgs", type=int, default=2,
                    help="独立机构数下限（默认 2，演示要件多源印证）")
    sp.add_argument("--hours", type=float, default=0, help="只看最近 N 小时，0=不限")
    sp.add_argument("--json", action="store_true", help="额外输出 JSON")
    sp.set_defaults(func=cmd_ready)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
