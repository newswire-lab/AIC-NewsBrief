"""事件关键词标签：给「热点事件」提炼能说明"这件事是什么"的短语。

为什么重写（2026-10-22）
------------------------
旧实现直接把标题/摘要按 **2~4 字滑窗**切碎，再把相邻碎片拼回长串。
用户的原话是「事件关键词抽取的关键词不能反映事件的特点或主要内容」，
实测确实是**标题滑窗碎片**：

======================  ==========================================  ==========
标题                     抽出的"关键词"                                病灶
======================  ==========================================  ==========
美国 FCC 将禁止…          ``FCC`` ``业成`` ``将禁止中国测``            12 字滑窗
国务院联防联控机制…      ``务院联`` ``控机``                       残渣
中方希望国际刑庭…        ``余留机`` ``作尽快取得进展``              跨词切断
老年人现在打疫苗晚不晚    ``晚不晚`` ``不能打``                      疑问句碎片
======================  ==========================================  ==========

全库 5195 个标签里，**4% 是纯 ASCII/符号**、**13% 不超过 2 字**（``习近`` 就是
「习近平」被切掉一半）。根因不是参数没调好，而是**没有词边界**这件事本身。

现在的做法
----------
1. :mod:`src.newwords` 从语料里**无词典学出词表**（内聚度 + 自由度 + 最大 n-gram）；
2. 候选 = **词表里的词**（边界对齐）+ **语料里反复出现的相邻词对**（``战略石油储备``、
   ``自杀无人机`` 这类真正的短语）；
3. 打分 = 簇内覆盖度 × IDF × 标题加成 × **长度加成**。

关于长度加成（2026-10-22 改判，旧版是"刻意不加"）
--------------------------------------------------
旧版的理由是：候选是滑窗 n-gram，越长 df 越低是必然，加成会把"随便切的一刀"
捧成最高分。那个理由在旧实现里成立。

现在候选已经对齐在词边界上（词 / 相邻词对），长度不再意味着"切得随意"，
而是意味着**更具体**：``亿美元导弹`` < ``导弹储备`` < ``战略石油储备``。
实测反例：不加长度加成时，``项目``/``计划``/``方式`` 这类泛词靠 IDF 挤掉了
真正描述事件的短语。
所以长度加成在这里是**修正过度切碎**，不是福利。上限 2 倍，避免长串无限膨胀。

于是候选天然落在词的边界上，不再需要 ``_merge_phrase`` 那种"把碎片拼回去"的补丁。
实测残渣类标签（``务院联``/``控机``/``习近``）全部消失。

稳定性
------
候选一律**排序后**参与统计，排序键末尾补上候选本身。原因：字符串哈希每个进程都
不一样（``PYTHONHASHSEED``），同分并列时会按不同的插入序胜出 ——
早期实测「同一份数据两次跑出不同标签」。而"标签稳定不抖"正是本项目
不用大模型做标签的核心理由。

产出去向
--------
算出的事件标签写入 ``event_tags`` 表（见 :func:`tag_all`），
界面只读表、不在刷新时现算 —— 全库 IDF 与词表构建要几秒，
放进 60 秒一次的自动刷新里会明显拖慢界面。
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import newwords

DEFAULT_TOPN = 4            # 每个事件保留几个标签
MIN_CLUSTER_DF = 2          # 候选至少在簇内几篇文章里出现
MAX_DOC_CHARS = 800         # 每篇只取前若干字符，控制计算量
MAX_VOCAB_CHARS = 1200      # 建词表时每篇取多少字符（比候选文本更宽松）
TITLE_BOOST = 2.0           # 出现在事件标题里的候选加成倍数
LENGTH_BONUS = 0.30         # 每多一个字加多少分（上限 2 倍）
LENGTH_BONUS_MAX = 2.0

# 拉丁字母词（型号、机构缩写、公司名之类）
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{2,}")

# 极少数必须硬编码的噪声词：它们在 IDF 上杀不掉。
#
# 为什么这里只收**虚词、版式词与无区分度的新闻套话**，不收「建设」「发展」这类
# 内容词：词表已经把候选限制在"词"上，NOISE 只做**完全相等**过滤，
# 禁掉「中国」并不会阻止「美国战略石油储备」这种更长的短语被选中。
NOISE = {
    # 版式/引用类（本项目语料里几乎每篇都有）
    "中国", "美国", "俄罗斯", "日本", "记者", "报道", "消息", "新闻", "表示",
    "新华社", "中新社", "日电", "简称", "目前", "近日", "日前", "当天",
    "昨天", "今天", "明天", "今年", "去年", "明年", "综合", "报道称",
    "中新网", "中新社北京", "当地时间", "北京时间", "本报", "编者",
    # 虚词、连接词、人称与指示代词
    "以及", "并且", "而且", "因为", "所以", "但是", "如果", "虽然", "由于",
    "因此", "于是", "同时", "此外", "其中", "通过", "对于", "关于", "根据",
    "按照", "随着", "针对", "作为", "成为", "能够", "需要", "进行",
    "已经", "可以", "这个", "那个", "我们", "他们", "是否", "这些", "那些",
    "这样", "那样", "以上", "以下", "之后", "之前", "以来", "左右", "大约",
    # 动词类（新闻体里出现频率极高但无区分度）
    "认为", "指出", "强调", "介绍", "发布", "宣布", "举行", "召开", "出席",
    "参加", "访问", "相关", "有关", "其他", "分别", "先后", "全面",
    "报告称", "报道称", "介绍说", "了解到", "据悉", "消息称", "显示",
    # 数量/时间与泛化名词
    "一个", "两个", "三个", "多个", "一位", "一名", "两名", "三名",
    "第一", "第二", "第三", "部分", "问题", "情况", "工作", "方面",
    "时间", "国家", "重要", "共同", "持续", "开展", "推进", "加强",
    "组织", "中心", "人员", "代表", "政府", "公司", "世界", "时代",
}
# 英文停用/泛词（英文源的事件标签如果不过滤这些，会出现
# "publicly"、"first-ever"、"reports" 这类毫无区分度的词）
EN_NOISE = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had",
    "will", "would", "could", "should", "says", "said", "report", "reports",
    "reported", "according", "publicly", "first-ever", "new", "news", "more",
    "most", "than", "into", "over", "after", "before", "about", "its", "his",
    "her", "their", "they", "you", "your", "can", "may", "not", "but", "all",
    # 公司后缀：这类词单看不出任何事件特征（实测出现过 "inc"/"llc" 当标签）
    "inc", "llc", "corp", "corporation", "ltd", "co", "gmbh", "plc", "sa",
}
NOISE |= EN_NOISE

# 泛动词/副词/连接词的**边界过滤**（与 NOISE 的"完全相等"过滤不同）。
#
# 为什么需要它：候选现在都是词了，但照样会选出 ``没有完成``/``尽快``/
# ``生产导弹`` 这种"语义上什么都没说"的组合，把真正有信息量的短语挤掉。
# 规则：一个候选的**开头或结尾**落在这里就算泛词组合。
#
# 为什么不直接把它们丢进 NOISE 做子串匹配：那样 ``美国防部``（含「美国」）、
# ``中国主权``（含「中国」）也会被误杀 —— 而它们恰恰是有信息量的。
# 边界规则天然避开了这个坑。
_WEAK_EDGE: Set[str] = set((
    "没有 完成 尽快 加速 无法 必要 主要 方式 更新 计划 项目 监督 扩大 生产 "
    "提供 支持 允许 要求 影响 出现 导致 包括 涉及 进行 开展 推进 加强 继续 "
    "努力 关注 呼吁 警告 决定 拒绝 承认 否认 主张 提出 讨论 研究 分析 评估 "
    "检查 确认 获取 使用 利用 采用 建立 提高 保持 维持 参与 帮助 促进 保护 "
    "打击 应对 处理 解决 改善 改变 增加 减少 简称 首次 近年 同时 此外 "
    "将会 将于 已经 可以 能够 需要 必须 以及 并且 或者 但是 而且 因为 所以 "
    "如果 虽然 由于 因此 于是 其中 通过 对于 关于 根据 按照 随着 针对 "
    "作为 成为 我们 他们 这个 那个 这些 那些 这样 那样 以上 以下 之后 "
    "之前 以来 左右 大约 认为 指出 强调 介绍 发布 宣布 举行 召开 出席 "
    "参加 访问 相关 有关 其他 分别 先后 全面 消息 报道 记者 表示 显示 "
    "据悉 本报 编者 各自 正在 仍在 仍将 可能 预计 约为 高达 超过 "
    "不足 至少 多达 将近 曾经 一直 仍然 依然 逐渐 不断 再次 "
    "全都 均已 共计 总计 累计 日前 当天 晚间 上午 下午 目前 近日 "
    # 评价/程度类：几乎任何稿子都能套，不说明"这件事是什么"
    "顺利 一般 基本 明显 严重 大幅 小幅 显著 快速 稳定 良好 积极 "
    "乐观 担忧 期待 相对 较为 有所 尤为 尤其 极其 十分 非常 近乎 "
).split())
_WEAK_EDGE = {w for w in _WEAK_EDGE if len(w) >= 2}
_EDGE_LENS = (2, 3, 4)

# 泛**名词**：只做**整词**过滤，**不能**做边界过滤。
#
# 实测教训：把「总统/主席」放在边界集合里，``美国总统``、``中国国家主席``
# 这两个有信息量的实体会被删掉，一个事件只剩「机场」一个标签。
# 名词出现在短语里是有信息量的，单独出现才是噪声。
_WEAK_NOUN: Set[str] = set((
    "专家 首席 规划 中心 官员 部长 局长 主任 主席 总统 司令 发言人 "
    "负责人 主管 成员 委员会 机构 部门 组织 集团 公司 技术 系统 "
    "能力 水平 情况 方面 内容 阶段 结果 效果 意义 作用 目标 任务 "
    "责任 措施 领域 范围 规模 数量 地点 名称 领导 领导层 状态 行动 "
).split())
NOISE |= _WEAK_NOUN

# 通讯社电头前缀：``中新网南京``、``中新社山西`` 这类是版式，不是事件特征。
_DATELINE = ("中新网", "中新社", "新华社", "本报")

# 纯数字/时间类片段（"2026"、"12月" 之类）不是事件特征
_DIGITY = re.compile(r"^[\d\s年月日时分秒个次美元万亿百千万]+$")

_VOCAB: Optional[newwords.Vocabulary] = None


def set_vocabulary(vocab: Optional[newwords.Vocabulary]) -> None:
    """显式指定词表（测试用；正常运行走 :func:`build_idf` 自动加载）。"""
    global _VOCAB
    _VOCAB = vocab


def vocabulary() -> newwords.Vocabulary:
    """当前词表。缓存里有就读缓存，没有就返回空表（调用方应先跑 build_idf）。"""
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = newwords.load() or newwords.Vocabulary(())
    return _VOCAB


def _doc_text(title: str, summary: str, summary_zh: str, title_zh: str = "") -> str:
    """取用来挖掘关键词的文本。

    **标题放最前面**。早期版本把中文摘要放前面，结果遇到摘要很长的源
    （如中国军网的全篇稿）600 字符全被摘要占满，标题一个字都进不来 ——
    而标题恰恰是「这件事是什么」最准的信号。

    **中文译名优先。** 外文标题一旦被 ``scripts/warm_translations.py`` 译成中文，
    就应该用中文标题做挖掘文本 —— 否则英文事件抽出来的标签永远是英文
    （实测：文章 100% 有 ``title_zh`` 时标签依旧是 ``Advisors``/``Mazars``，
    因为这里当时只读了 ``summary_zh``（正文译名），根本没读 ``title_zh``）。
    """
    parts = [title_zh or title or "", summary_zh or "", summary or ""]
    # 外文文章只要拿到中文译名，就**只用中文文本**做挖掘。
    # 理由：英文正文里的稀有词（实测出现过 "quarter"、"advisors"）在 IDF 上
    # 和中文候选平起平坐地抢标签位，结果一个中文界面上的标签夹着英文单词。
    if title_zh or summary_zh:
        parts = [p for p in (title_zh, summary_zh) if p]
    return newwords.normalize_text(" ".join(p for p in parts if p))[:MAX_DOC_CHARS]


def _vocab_text(title: str, summary: str, summary_zh: str, title_zh: str = "") -> str:
    """建词表用的文本：**四个字段全用**，比候选文本更宽松。

    为什么和 :func:`_doc_text` 不一样：候选文本优先中文（避免中英混杂），
    但词表是**语料级统计**，多一份文本就多一份统计量。
    实测代价很明显：只用中文文本时语料 11.7 万字，词表 3073 条，
    残渣（``防部``/``欧盟资``）明显偏多；放宽后统计量翻倍，残渣大幅减少。
    """
    parts: List[str] = []
    for p in (title_zh, title, summary_zh, summary):
        if p and p not in parts:
            parts.append(p)
    return " ".join(parts)[:MAX_VOCAB_CHARS]


def _is_weak(g: str) -> bool:
    """候选是否本身就是泛词，或以泛动词/副词开头、结尾（见 ``_WEAK_EDGE``）。

    **必须先查相等**：2 字候选（``尽快``/``必要``/``专家``）的边界就是它自己，
    下面那个边界循环对 n=2 一次都不会执行 —— 早期版本就这样把 2 字泛词全放过了。
    """
    if g in _WEAK_EDGE:
        return True
    n = len(g)
    for size in _EDGE_LENS:
        if size > n - 1:
            break
        if g[:size] in _WEAK_EDGE or g[n - size:] in _WEAK_EDGE:
            return True
    return False


def _candidates(text: str) -> List[str]:
    """枚举一篇文本的候选，**按字典序**返回（稳定性，见模块文档）。

    候选来源只有两个，都天然对齐在词边界上：

    1. 词表里的词（长度 2~6 字）；
    2. 语料里反复出现（>=2 篇）的相邻词对。
    """
    if not text:
        return []
    vocab = vocabulary()
    out: Set[str] = set()
    for g in vocab.candidates(text):
        if g in NOISE or _DIGITY.match(g) or _is_weak(g):
            continue
        if g.startswith(_DATELINE):
            continue
        out.add(g)
    # 拉丁/数字词（型号、机构缩写之类）；去掉尾部标点并统一小写比较
    for m in _LATIN_RE.finditer(text):
        g = m.group(0).strip(".-–—").lower()
        # 至少 3 个字母：挡掉 ``c.h``/``u.s`` 这类点分缩写
        if len(g) >= 3 and sum(ch.isalpha() for ch in g) >= 3 and g not in NOISE:
            out.add(g)
    return sorted(out)


def build_idf(docs: Sequence[Tuple[str, int]],
              verbose: bool = False,
              refresh_vocab: bool = False,
              vocab_docs: Optional[Sequence[str]] = None) -> Dict[str, float]:
    """从全库文档算 IDF，并顺带备好词表。

    :param docs: ``[(文本, 所属事件id), ...]``，文本一般是中文摘要
    :param refresh_vocab: 忽略词表缓存强制重建
    :param vocab_docs: 建词表用的语料（可比 ``docs`` 更完整，见 :func:`_vocab_text`）
    :return: ``{候选: idf}``，取 ``ln((N+1)/(df+1))``
    """
    texts = list(vocab_docs) if vocab_docs is not None else [t for t, _eid in docs]
    vocab = newwords.load_or_build(texts, refresh=refresh_vocab, verbose=verbose)
    set_vocabulary(vocab)
    n = len(docs)
    df: Counter = Counter()
    for text, _eid in docs:
        df.update(_candidates(text))
    return {w: math.log((n + 1) / (c + 1))
            for w, c in sorted(df.items(), key=lambda kv: kv[0])}


def _len_factor(g: str) -> float:
    """长度加成：越具体越好，但有上限。"""
    return min(LENGTH_BONUS_MAX, 1.0 + LENGTH_BONUS * max(0, len(g) - 2))


def event_keywords(
    docs: Sequence[str],
    idf: Dict[str, float],
    topn: int = DEFAULT_TOPN,
    min_cluster_df: int = MIN_CLUSTER_DF,
    exclude: Optional[Iterable[str]] = None,
    title: str = "",
) -> List[str]:
    """从一个事件簇的多篇文档里提炼关键词。

    :param docs: 簇内各篇的文本（一篇一个元素），第一篇应是最权威的那篇
    :param idf: :func:`build_idf` 的结果；缺失的候选按 idf=0 处理（即不采纳）
    :param min_cluster_df: 候选至少在几篇文档里出现
    :param exclude: 额外要排除的候选
    :param title: 事件代表标题；出现在标题里的候选给一份加成 ——
        标题是「这件事是什么」最准的信号，而正文里稀缺的词未必是事件特征。
        **单篇事件尤其依赖这一条**（它没有「簇内重复」可用）。
    """
    banned = set(NOISE) | set(exclude or ())
    title_cands = set(_candidates(title)) if title else set()
    texts = [d for d in docs if d and d.strip()]
    if not texts:
        return []
    per_doc = [set(_candidates(d)) for d in texts]

    scored: List[Tuple[str, float]] = []
    if len(per_doc) < min_cluster_df:
        # 单篇文章的事件没有"簇内重复"信号，退化为：稀缺度 + 标题加成
        for g in sorted(per_doc[0]):
            if g in banned:
                continue
            w = idf.get(g, 0.0)
            if w <= 0:
                continue
            scored.append((g, w * _len_factor(g) * (TITLE_BOOST if g in title_cands else 1.0)))
    else:
        df_in_cluster: Counter = Counter()
        for s in per_doc:
            # update(sorted(...))：保证 Counter 的插入顺序稳定
            df_in_cluster.update(sorted(s))
        for g, c in df_in_cluster.items():
            if c < min_cluster_df or g in banned:
                continue
            w = idf.get(g)
            if not w or w <= 0:
                continue
            # 簇内覆盖度为主，IDF 为乘子
            scored.append((g, c * w * _len_factor(g)
                           * (TITLE_BOOST if g in title_cands else 1.0)))

    # 排序键：分数降序 → **长度降序** → 字典序。
    # 长度放在分数之后而不是之前：分数仍是主判据，
    # 只在同分时偏向更长、更具体的那个（``战略石油储备`` 胜过 ``石油储备``）。
    scored.sort(key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return _pick(scored, topn)


def _pick(scored: Sequence[Tuple[str, float]], topn: int) -> List[str]:
    """按分数降序贪心挑选，跳过与已选标签互相包含的候选。

    只做**互相包含**判断就够了：候选现在是词表里的词或相邻词对，
    不会出现旧实现那种"相邻滑窗大幅重叠但不是子串"的错位碎片。
    """
    picked: List[str] = []
    for g, _s in scored:
        if any(g in p or p in g for p in picked):
            continue
        picked.append(g)
        if len(picked) >= topn:
            break
    return picked


def tag_event(conn: sqlite3.Connection, event_id: str,
              idf: Dict[str, float], topn: int = DEFAULT_TOPN) -> List[str]:
    """给单个事件算标签（读库，不写库）。"""
    rows = conn.execute(
        "SELECT title, title_zh, summary, summary_zh FROM articles "
        "WHERE event_id = ? "
        "ORDER BY source_tier ASC, COALESCE(published_at, fetched_at) DESC",
        (event_id,),
    ).fetchall()
    docs = [_doc_text(r["title"], r["summary"], r["summary_zh"], r["title_zh"])
            for r in rows]
    # 代表标题取等级最高的那篇（上面的 ORDER BY 已把它排到第一行）
    head = (rows[0]["title_zh"] or rows[0]["title"]) if rows else ""
    return event_keywords(docs, idf, topn=topn, title=head)


def tag_all(conn: sqlite3.Connection, topn: int = DEFAULT_TOPN,
            days: Optional[int] = None, verbose: bool = True,
            refresh_vocab: bool = False) -> Dict[str, int]:
    """给事件算标签并写入 ``event_tags`` 表。

    :param days: 只处理"最新跟进"在最近 N 天内的事件；None 表示全部
    :param refresh_vocab: 忽略词表缓存强制重建（改了语料统计口径时用）
    """
    sql = ("SELECT event_id, title, title_zh, summary, summary_zh FROM articles "
           "WHERE event_id != ''")
    params: Tuple = ()
    if days is not None and days > 0:
        sql += " AND datetime(COALESCE(published_at, fetched_at)) >= datetime('now', ?)"
        params = (f"-{int(days)} days",)
    # 按等级排序，使每个事件的第一行就是最权威的那篇 —— 它当代表标题
    sql += " ORDER BY source_tier ASC, COALESCE(published_at, fetched_at) DESC"
    rows = conn.execute(sql, params).fetchall()

    by_event: Dict[str, List[str]] = defaultdict(list)
    titles: Dict[str, str] = {}
    all_docs: List[Tuple[str, int]] = []
    vocab_docs: List[str] = []
    for r in rows:
        text = _doc_text(r["title"], r["summary"], r["summary_zh"], r["title_zh"])
        if not text:
            continue
        by_event[r["event_id"]].append(text)
        titles.setdefault(r["event_id"], r["title_zh"] or r["title"] or "")
        all_docs.append((text, 0))
        vocab_docs.append(_vocab_text(r["title"], r["summary"],
                                     r["summary_zh"], r["title_zh"]))

    if not all_docs:
        if verbose:
            print("  [TAG] 库里没有可用文本，跳过")
        return {"events": 0, "tagged": 0}

    idf = build_idf(all_docs, verbose=verbose, refresh_vocab=refresh_vocab,
                    vocab_docs=vocab_docs)
    conn.execute("DELETE FROM event_tags")
    tagged = 0
    payload = []
    for eid, docs in by_event.items():
        tags = event_keywords(docs, idf, topn=topn, title=titles.get(eid, ""))
        if not tags:
            continue
        payload.append((eid, "、".join(tags), len(docs)))
        tagged += 1
    conn.executemany(
        "INSERT OR REPLACE INTO event_tags (event_id, tags, n_docs, updated_at) "
        "VALUES (?, ?, ?, datetime('now'))", payload)
    conn.commit()

    result = {"events": len(by_event), "tagged": tagged,
              "idf_terms": len(idf), "vocab": len(vocabulary())}
    if verbose:
        print(f"  [TAG] 事件 {result['events']} 个，产出标签 {tagged} 个，"
              f"IDF 词表 {len(idf)} 条，分词词表 {result['vocab']} 条")
        for eid, tags, nd in payload[:3]:
            print(f"        {eid[:18]}…  [{nd} 篇]  {tags}")
    return result
