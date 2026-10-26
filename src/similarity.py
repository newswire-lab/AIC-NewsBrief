"""文本相似度：IDF 加权的中文标题向量与余弦相似度。

**动机（由实测驱动，而非拍脑袋）**

标题 SimHash 只能发现"字面转载"（同一稿件被原样重发）。实测 933 篇稿件中，
海明距离 <= 3 的 119 对**全部**是同一机构内部互转；跨机构之间即使把阈值放宽到
<= 10 也仍然是 0 对。原因很直接：不同媒体对同一事件会写不同的标题，例如

    中国军网：习近平抵达华盛顿，开始对美国进行国事访问
    国防部站：习近平抵达华盛顿对美国进行国事访问

字面上差得不多，但 SimHash 对短文本本就敏感；
再如"陆军某旅开展飞行训练"与"陆军某旅组织飞行训练"，
按字面几乎是两篇稿子，按语义显然是同一件事。

因此事件归并需要**语义近似**而非**字面近似**，本模块提供这一层。

**算法**

1. 分词（不依赖 jieba）：拉丁字母/数字连续串 + 中文 2-gram 与 3-gram。
   中文用 2/3-gram 而不分词，好处是能自然覆盖未登录词
   （人名、地名、装备型号如"海南舰""F-16""055"），无需维护词典。
2. 语料内 IDF 加权：某个 gram 出现的标题越少，区分度越高。
   这让"美国""访问""训练"这类高频词自动降权，
   让"海南舰""沃尔沃""萨德"这类专名自动升权。
   —— 这一步是自研的，不依赖任何外部模型。
3. TF-IDF 向量 + 余弦相似度。
4. 倒排索引剪枝：只与"共享关键词最多"的前 K 个标题比较，
   避免 O(n^2) 全量比对。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Tuple
import math
import re

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-_][a-z0-9]+)*")
_ZH_ONLY_RE = re.compile(r"[^\u4e00-\u9fff]+")
_WS_RE = re.compile(r"\s+")

# 中文停用/泛化词：这些词几乎出现在所有新闻标题里，对区分事件没有帮助。
# 交给 IDF 也能降权，但显式剔除更稳定（短语料下 IDF 估计不稳）。
STOP_GRAMS = {
    "中国", "我国", "记者", "报道", "新闻", "消息", "表示", "指出", "认为",
    "进行", "开展", "组织", "举行", "出席", "参加", "有关", "方面", "情况",
    "工作", "问题", "活动", "相关", "以及", "并且", "但是", "因为", "所以",
    "一个", "我们", "他们", "这个", "那个", "可以", "已经", "正在", "今天",
    "昨天", "明天", "上午", "下午", "近日", "日前", "目前", "同时", "此外",
}


def tokenize(text: str) -> List[str]:
    """标题 -> 关键词列表（拉丁/数字串 + 中文 2/3-gram）。"""
    if not text:
        return []
    text = _WS_RE.sub(" ", text.strip()).lower()
    tokens: List[str] = []

    for m in _ASCII_TOKEN_RE.finditer(text):
        tok = m.group(0)
        # 纯数字串区分度低（日期、编号），但装备编号（055、f-16）有区分度，
        # 这里保留长度 2~12 的拉丁/数字串，超长的多为 URL 残留
        if 1 < len(tok) <= 12:
            tokens.append(tok)

    zh = _ZH_ONLY_RE.sub(" ", text)
    for seg in zh.split():
        if len(seg) == 1:
            continue
        for n in (2, 3):
            if len(seg) < n:
                continue
            tokens.extend(seg[i:i + n] for i in range(len(seg) - n + 1))

    return [t for t in tokens if t not in STOP_GRAMS]


class TitleSimilarityIndex:
    """一小组标题的 TF-IDF 向量索引，用于找出"语义近似"的标题对。"""

    def __init__(self, docs: Sequence[Tuple[str, str]]) -> None:
        """docs: [(key, title), ...]，key 用于回指原对象（本项目中是行号字符串）。"""
        self.keys: List[str] = []
        self.counters: List[Counter] = []
        for key, title in docs:
            self.keys.append(key)
            self.counters.append(Counter(tokenize(title)))

        df: Counter = Counter()
        for counter in self.counters:
            for token in counter:
                df[token] += 1

        n = max(1, len(self.counters))
        # 平滑 IDF：语料很小时也不会出现 0 或负值
        self.idf: Dict[str, float] = {
            token: math.log((n + 1.0) / (count + 1.0)) + 1.0
            for token, count in df.items()
        }

        self.vectors: List[Dict[str, float]] = []
        self.norms: List[float] = []
        self.inverted: Dict[str, List[int]] = defaultdict(list)

        for idx, counter in enumerate(self.counters):
            vec: Dict[str, float] = {}
            for token, count in counter.items():
                vec[token] = (1.0 + math.log(count)) * self.idf.get(token, 1.0)
            self.vectors.append(vec)
            self.norms.append(math.sqrt(sum(v * v for v in vec.values())) or 1.0)
            for token in vec:
                self.inverted[token].append(idx)

    def size(self) -> int:
        return len(self.keys)

    def candidates(self, idx: int, topk: int = 25) -> List[int]:
        """与第 idx 条共享关键词最多的前 topk 个候选（倒排索引剪枝）。"""
        shared: Counter = Counter()
        for token in self.vectors[idx]:
            for other in self.inverted[token]:
                if other != idx:
                    shared[other] += 1
        return [j for j, _ in shared.most_common(topk)]

    def similarity(self, i: int, j: int) -> float:
        """余弦相似度，取值 0~1。"""
        vi, vj = self.vectors[i], self.vectors[j]
        if len(vi) > len(vj):
            vi, vj = vj, vi
        dot = 0.0
        for token, weight in vi.items():
            other = vj.get(token)
            if other:
                dot += weight * other
        if dot <= 0.0:
            return 0.0
        return dot / (self.norms[i] * self.norms[j])


def score_pair(a: str, b: str) -> float:
    """一次性比较两条标题（少量比对用，不走索引）。"""
    idx = TitleSimilarityIndex([("a", a), ("b", b)])
    return idx.similarity(0, 1)
