"""无词典新词发现：给事件关键词提供「候选必须是词」的约束。

为什么需要它
------------
旧实现直接在**纯中文连续段**上滑 2~4 字窗口，再把相邻碎片拼回长串。
没有词典、没有边界，结果必然是"掐断的半截"或"拼接的残渣"——
实测过的事件标签：``务院联`` / ``控机`` / ``将禁止中国测`` / ``习近``（习近平被切一半）。
用户原话是"关键词不能反映事件的特点或主要内容"，根因就在这里。

本模块用**无词典分词的标准三件套**从语料里自己学出词表：

1. **内聚度**（互信息）：``log(p(g) * N / (p(左) * p(右)))``，
   取所有切分点的最小值 —— 一个字串内部各段都"黏得紧"才算词；
2. **自由度**（左右邻接熵）：一个词应该能自由搭配。
3. **最大 n-gram**：若存在更长的串覆盖了本串 >=90% 的出现次数，本串就是碎片。

判据里有两个反直觉的点，都是实测踩出来的（别改回去）：

- 自由度用 **max(左熵, 右熵)** 而不是 min。
  ``国务院联防联控机制`` 的左邻恒为「务」（熵 0），用 min 会把真专名误杀；
  而真碎片 ``砖国家领导人第十`` 两侧都固定，用 max 才拦得住。
- 虚词要拆成 **NO_START / NO_END 两个集合**。
  曾经把「地」也禁在词尾，于是真词 ``军事基地`` 被删、残渣 ``军事基`` 留下。

标定记录（2887 篇语料，28 个已知好词 vs 21 个手工坏词）
------------------------------------------------------
内聚度：好词 min 2.32 / 中位 5.42；坏词 max 4.73 / 中位 0.36。
注意**子串的内聚度可能高于整词**（``务院`` 4.73 > ``国务院`` 4.26），
所以内聚度单用必然切出碎片，必须配合"最大 n-gram"规则。
当前成果：好词命中 33/34、坏词残留 0/23，词表约 5600 条。

产出去向
--------
词表是**派生数据**（由语料统计得出），缓存在 ``data/lexicon/newwords.txt``；
``data/`` 不入版本库，所以克隆仓库后首次运行会自动重建。
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 数字 + 年月日：必须当**分隔符**处理。
#
# 不处理的后果（实测）：``2026年全国食品安全宣传周`` 里的数字不在中文段内，
# 切出来的残段变成 ``年全国``，而它在语料里总是跟在某个数字后面，
# 统计上显得“很常见”，于是成了一个假词。
_DATE_NOISE = re.compile(r"[0-9０-９]+\s*[年月日号]|[0-9０-９]+")

MAX_GRAM = 8            # n-gram 枚举上界（要比 WORD_MAX 大，才能判出"总被更长串包住"）
WORD_MIN, WORD_MAX = 2, 6
PHRASE_MAX = 12         # 相邻词对拼成的短语最长多少字
MIN_COHESION = 2.0      # 内聚度下限（自然对数）
MIN_MAX_ENTROPY = 1.0   # 左右**至少有一侧**的自由度下限（自然对数）
CONTAIN_RATIO = 0.9     # 更长串覆盖本串多少比例即判本串为碎片
MIN_PHRASE_DF = 2       # 词对至少在几篇文档里出现才算短语
CACHE_VERSION = 4       # 换算法/换语料就加一，避免读到旧格式缓存

# 不能出现在词**开头**的字
NO_START: Set[str] = set(
    "的了和与及或而则就都是将在被对为以于从向给由使让把等之其此该这那很不都已还又再才只但且并"
    "着过吗呢吧啊么个我你他她它们所地得过来去"
)
# 不能出现在词**结尾**的字。比 NO_START 少：``基地``/``阵地``/``经过``/``未来``
# 都是合法词尾，禁掉就会把真词删掉、只留残渣（实测过 ``军事基`` 取代 ``军事基地``）。
NO_END: Set[str] = set(
    "的了和与及或而则就都是将在被对为以于从向给由使让把等之其此该这那很都已还又再才只但且并"
    "着吗呢吧啊么所我你他她它们"
)


def _min_df(n: int) -> int:
    """频次下限按词长递减：长词罕见，但恰恰信息量最大。"""
    return 4 if n == 2 else 3 if n == 3 else 2


def normalize_text(text: str) -> str:
    """把数字/日期换成空格（变成词边界），供分词与词表统计使用。"""
    return _DATE_NOISE.sub(" ", text or "")


def _entropy(counter: Counter) -> float:
    total = sum(counter.values())
    if total <= 1:
        return 0.0
    return -sum((v / total) * math.log(v / total) for v in counter.values())


class Vocabulary:
    """分词词表 + 高频相邻词对。

    :param words: 词表（长度 2~6 字）
    :param phrases: 语料里出现过 >=2 次的相邻词对（长度 4~12 字）
    """

    def __init__(self, words: Sequence[str], phrases: Iterable[str] = ()):
        ws = tuple(sorted(set(w for w in words if w)))
        self.words = ws
        self.phrases = frozenset(phrases)
        self._set = frozenset(ws)
        self.maxlen = max((len(w) for w in ws), default=1)

    def __len__(self) -> int:
        return len(self._set)

    def __contains__(self, w: str) -> bool:
        return w in self._set

    def segment(self, text: str) -> List[str]:
        """最大正向匹配。切不出来的单字原样保留（调用方会按长度过滤）。"""
        out: List[str] = []
        i, n, ml = 0, len(text), self.maxlen
        while i < n:
            for size in range(min(ml, n - i), 0, -1):
                w = text[i:i + size]
                if size == 1 or w in self._set:
                    out.append(w)
                    i += size
                    break
        return out

    def tokens(self, text: str) -> List[str]:
        """只对纯中文连续段分词（标点/数字/英文原样交出，由调用方处理）。"""
        toks: List[str] = []
        for run in CJK_RUN.findall(normalize_text(text)):
            toks.extend(self.segment(run))
        return toks
        return toks

    def phrases_of(self, toks: Sequence[str]) -> List[str]:
        """把相邻的两个词拼成短语，只保留语料里反复出现的那些。

        要求两个词都 >=2 字：否则 ``中波音``/``俄将``/``机合同`` 这类
        "单字 + 词"的垃圾短语会大量出现（实测过）。
        """
        out: List[str] = []
        for k in range(len(toks) - 1):
            a, b = toks[k], toks[k + 1]
            if len(a) < WORD_MIN or len(b) < WORD_MIN:
                continue
            p = a + b
            if len(p) <= PHRASE_MAX and p in self.phrases:
                out.append(p)
        return out

    def candidates(self, text: str) -> List[str]:
        """候选 = 词表里的词 + 语料里常见且边界对齐的相邻词对。

        **词对必须逐段内生成**。早期版本先把所有中文段的分词结果拼成一个长列表，
        于是「标题末尾的 ``预期``」和「摘要开头的 ``中新社``」被当成相邻词，
        抽出了 ``预期中新社``/``启动中新社`` 这种跨段垃圾（用户看到的就是这种）。

        返回**排序后**的列表：候选顺序必须与进程无关（PYTHONHASHSEED），
        否则同分候选的胜负会抖动，"标签稳定"这个卖点就没了。
        """
        out: Set[str] = set()
        for run in CJK_RUN.findall(normalize_text(text)):
            toks = self.segment(run)
            out.update(t for t in toks if len(t) >= WORD_MIN)
            out.update(self.phrases_of(toks))
        return sorted(out)


def build(docs: Sequence[str], verbose: bool = False) -> Vocabulary:
    """从语料统计出词表与高频词对。"""
    runs_per_doc = [CJK_RUN.findall(normalize_text(d)) for d in docs]
    doc_df: Counter = Counter()
    gram: Counter = Counter()
    chars: Counter = Counter()
    left: Dict[str, Counter] = defaultdict(Counter)
    right: Dict[str, Counter] = defaultdict(Counter)
    total = 0

    for runs in runs_per_doc:
        seen = set()
        for run in runs:
            n = len(run)
            total += n
            for ch in run:
                chars[ch] += 1
            for size in range(2, min(MAX_GRAM, n) + 1):
                for i in range(n - size + 1):
                    g = run[i:i + size]
                    gram[g] += 1
                    seen.add(g)
                    left[g][run[i - 1] if i > 0 else "^"] += 1
                    right[g][run[i + size] if i + size < n else "$"] += 1
        for g in seen:
            doc_df[g] += 1

    if not total or not gram:
        return Vocabulary(())

    def count(s: str) -> int:
        return chars[s] if len(s) == 1 else gram.get(s, 0)

    def cohesion(g: str) -> float:
        pg = count(g)
        if pg == 0 or len(g) < 2:
            return 0.0
        worst = float("inf")
        for i in range(1, len(g)):
            pl, pr = count(g[:i]), count(g[i:])
            if pl == 0 or pr == 0:
                return 0.0
            worst = min(worst, math.log(pg * total / (pl * pr)))
        return worst if worst != float("inf") else 0.0

    # 1) 频次 + 内聚度
    stage1 = {g for g, df in doc_df.items()
              if df >= _min_df(len(g)) and cohesion(g) >= MIN_COHESION}
    # 2) 虚词不能在词边界上
    stage2 = {g for g in stage1 if g[0] not in NO_START and g[-1] not in NO_END}
    # 3) 至少一侧自由
    stage3 = {g for g in stage2
              if max(_entropy(left[g]), _entropy(right[g])) >= MIN_MAX_ENTROPY}
    # 4) 碎片消解：只在 <=WORD_MAX 的串里找"更长的覆盖者"，
    #    保证词表自洽（v4 的坑：6 字串把 4 字真词判成碎片，自己又被长度上限砍掉，
    #    两个都不在词表里，分词退化成单字）
    pool = {g for g in stage3 if len(g) <= WORD_MAX}
    covered: Dict[str, int] = defaultdict(int)
    for g2 in pool:
        n = len(g2)
        if n < 3:
            continue
        c2 = gram[g2]
        for i in range(n):
            for j in range(i + WORD_MIN, n):
                s = g2[i:j + 1]
                if len(s) == n:
                    continue
                if s in pool and c2 > covered[s]:
                    covered[s] = c2
    words = {g for g in pool if covered.get(g, 0) < CONTAIN_RATIO * gram[g]}

    vocab = Vocabulary(words)
    if verbose:
        print(f"  [NEWWORDS] 语料 {len(docs)} 篇 / {total} 字，"
              f"候选 {len(gram)} → 词表 {len(words)}")

    # 5) 高频相邻词对（短语），用刚得到的词表先分词再统计。
    #    **逐段统计**：跨段拼出来的词对是垃圾（见 :meth:`Vocabulary.candidates`）。
    pair_df: Counter = Counter()
    for runs in runs_per_doc:
        seen = set()
        for run in runs:
            toks = vocab.segment(run)
            for k in range(len(toks) - 1):
                a, b = toks[k], toks[k + 1]
                if len(a) < WORD_MIN or len(b) < WORD_MIN:
                    continue
                p = a + b
                if len(p) <= PHRASE_MAX:
                    seen.add(p)
        for p in seen:
            pair_df[p] += 1
    phrases = {p for p, c in pair_df.items() if c >= MIN_PHRASE_DF}

    vocab = Vocabulary(words, phrases)
    if verbose:
        print(f"  [NEWWORDS] 高频词对 {len(phrases)} 条")
    return vocab


# ---------------------------------------------------------------- 缓存

def default_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "lexicon", "newwords.txt")


def save(vocab: Vocabulary, path: Optional[str] = None, n_docs: int = 0) -> str:
    path = path or default_path()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    lines = ["#newwords v%d words=%d phrases=%d docs=%d"
             % (CACHE_VERSION, len(vocab.words), len(vocab.phrases), int(n_docs)),
             "#WORDS"]
    lines.extend(vocab.words)
    lines.append("#PHRASES")
    lines.extend(sorted(vocab.phrases))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def load(path: Optional[str] = None) -> Optional[Vocabulary]:
    """读缓存；格式不匹配或文件坏掉一律返回 None（调用方会重建）。"""
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().splitlines()
    except OSError:
        return None
    if not raw or not raw[0].startswith("#newwords v%d" % CACHE_VERSION):
        return None
    words: List[str] = []
    phrases: List[str] = []
    section = ""
    for line in raw[1:]:
        if line == "#WORDS":
            section = "w"
            continue
        if line == "#PHRASES":
            section = "p"
            continue
        if not line:
            continue
        if section == "w":
            words.append(line)
        elif section == "p":
            phrases.append(line)
    if not words:
        return None
    return Vocabulary(words, phrases)


def load_or_build(docs: Sequence[str], path: Optional[str] = None,
                  refresh: bool = False, verbose: bool = False,
                  tolerate: float = 0.05) -> Vocabulary:
    """读缓存，必要时重建。

    :param tolerate: 语料规模变化超过这个比例就重建 ——
        新文章持续入库，词表不能永远冻在第一次的状态；
        但也没必要每轮都重建，否则标签会随每轮采集轻微抖动。
    """
    if not refresh:
        cached = load(path)
        if cached is not None:
            header = _cached_docs(path)
            if header and abs(len(docs) - header) <= max(1, int(header * tolerate)):
                if verbose:
                    print(f"  [NEWWORDS] 命中缓存：词 {len(cached.words)} 条、"
                          f"短语 {len(cached.phrases)} 条")
                return cached
            if verbose:
                print(f"  [NEWWORDS] 语料规模 {header} → {len(docs)}，重建词表")
    vocab = build(docs, verbose=verbose)
    if len(vocab):
        try:
            save(vocab, path, n_docs=len(docs))
        except OSError as exc:      # 缓存写不进去不影响主流程
            if verbose:
                print(f"  [NEWWORDS] 写缓存失败（忽略）：{exc}")
    return vocab


def _cached_docs(path: Optional[str]) -> int:
    """缓存里记录的**语料篇数**（不是词数）。"""
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.readline()
    except OSError:
        return 0
    m = re.search(r"docs=(\d+)", head)
    return int(m.group(1)) if m else 0
