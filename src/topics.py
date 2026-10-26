"""新闻分区：把每篇文章判到「军事 / 政治 / 经济 / 科技 / 民生 / 文化 / 体育 / 国际 / 法治」等分区。

为什么用自研关键词加权，而不是让大模型逐篇分类
--------------------------------------------
1. **规模**：库里有 2000+ 篇，逐篇调模型（就算已预热也要 1 秒/条）就是半小时起，
   而且每新增一轮采集都要重跑一遍；
2. **确定性**：分区是**筛选维度**，同一篇文章今天在「军事」明天跳到「经济」
   会让用户以为筛选坏了 —— 关键词加权是纯函数，同样的输入永远同样的结果；
3. **可解释**：能直接告诉用户「这篇为什么判成军事」——命中了哪些词、各占多少分，
   出了问题能改词表，而不是对着模型的输出猜。

这与 :mod:`src.keywords`（事件关键词）、:mod:`src.credibility`（可信度）
是同一套思路：**能算的用算的，把模型留给它真正擅长的"写句子"**。

判分方式
--------
``score(topic) = Σ 命中词的权重``；取最高分为**主分区**，
若第二名的分数达到主分区的 ``SECONDARY_RATIO`` 且自身不低于 ``MIN_SCORE``，
再给一个**次分区**（一篇讲"人工智能用于军事"的稿子本该同时属于科技与军事，
硬塞进一个分区反而是失真）。最高分低于 ``MIN_SCORE`` 则判为「其他」。

中英双语词表：本项目语料中英混合（外文源占一半以上），只认中文词会漏掉一半文章。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

OTHER = "其他"

# 平分时的优先序：**越靠前越具体，越靠后越泛**。
#
# 原先的写法是 `sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))`，
# 即同分时按分区名的 Unicode 升序 —— 政治（政 U+653F）恒在 民生（民 U+6C11）之前，
# 于是「国务院联防联控机制：老年人应尽快接种疫苗」这种**健康科普稿**
# 因为 政治`国务院:3` 与 民生`疫苗:3` 打平，被稳定判成政治（审计实测一大批）。
# 名字排序是个隐藏的全局偏向，必须换成显式的语义优先序。
TIE_BREAK_ORDER = ("军事", "法治", "体育", "历史", "科技", "民生",
                   "经济", "社会", "文化", "国际", "政治", OTHER)
_TIE_RANK = {name: i for i, name in enumerate(TIE_BREAK_ORDER)}
SECONDARY_RATIO = 0.6      # 次分区至少要达到主分区分数的这个比例
MIN_SCORE = 3.0            # 最高分低于此值就不硬塞分区
MAX_DOC_CHARS = 800        # 只取前若干字符，控制计算量

# 词 -> 权重。3 = 该分区特有；2 = 强指示；1 = 弱指示（容易跨分区）
TOPIC_KEYWORDS: Dict[str, Dict[str, int]] = {
    "军事": {
        "部队": 3, "军演": 3, "演习": 3, "导弹": 3, "航母": 3, "战机": 3,
        "无人机": 3, "潜艇": 3, "防空": 3, "火炮": 3, "坦克": 3, "军舰": 3,
        "战舰": 3, "士兵": 3, "士兵": 3, "作战": 2, "国防": 3, "军费": 3,
        "装备": 2, "炮兵": 3, "兵": 2, "军区": 3, "军事": 3, "武器": 3,
        "拦截": 2, "弹药": 3, "陆军": 3, "海军": 3, "空军": 3, "特种部队": 3,
        "乌军": 3, "俄军": 3, "美军": 3, "以军": 3, "驻军": 3, "军": 2,
        "情报": 1, "战机": 3, "兵士": 3, "前线": 3, "阵地": 3, "防空系统": 3,
        "army": 3, "navy": 3, "missile": 3, "drone": 3, "warship": 3,
        "submarine": 3, "military": 3, "defense": 2, "weapon": 3,
        "artillery": 3, "troops": 3, "interceptor": 3, "hypersonic": 3,
        "fighter jet": 3, "air force": 3, "defence": 2, "warhead": 3,
        "battalion": 3, "soldier": 2, "combat": 2, "airstrike": 3,
        # 补充（2026-10-20）：实测漏进「其他」的高频军事表述
        "五角大楼": 3, "防长": 3, "空降师": 3, "自卫队": 3, "军官": 3,
        "将军": 3, "上将": 3, "中将": 3, "上校": 3, "军事行动": 3,
        "侦察": 2, "军用": 2, "战斗": 2, "驻日美军": 3,
        "pentagon": 3, "airborne": 3, "brigade": 3, "regiment": 3,
        "reconnaissance": 3, "garrison": 3, "helicopter": 3,
        # 增补（第三轮）：单字「军」会被丢弃，所以「全军/集团军」必须显式入表
        "全军": 3, "军方": 2, "集团军": 3, "军工": 3, "军旅": 2, "军种": 3,
        "战区": 3, "军衔": 3, "兵种": 3, "军援": 3, "军售": 3, "军属": 2,
        "潜航器": 3, "无人潜航器": 3, "战斗群": 3, "舰载": 3, "弹道": 3,
        "袭击": 3, "空袭": 3, "交火": 3, "反恐": 3, "维和": 3, "榴弹": 3,
        "巡航": 2, "编队": 3, "武装力量": 3, "装备部": 3,
    },
    "政治": {
        "总统": 3, "议会": 3, "选举": 3, "首相": 3, "外交": 3, "国务院": 2,
        "人大": 3, "政协": 3, "政府": 2, "政策": 2, "立法": 3, "党委": 3,
        "总书记": 3, "主席": 1, "白宫": 3, "克里姆林宫": 3, "制裁": 2,
        "峰会": 2, "声明": 1, "会谈": 2, "外交部": 3, "内政": 2, "执政": 3,
        "president": 2, "parliament": 3, "election": 3, "senate": 3,
        "congress": 3, "diplomat": 3, "prime minister": 3, "kremlin": 3,
        "sanctions": 2, "summit": 2, "policy": 1, "white house": 3,
        "government": 2, "minister": 2, "vote": 2, "legislation": 3,
        # 纪检反腐（按中文新闻惯例归政治，不归法治）：2026-10-20 补
        "双开": 3, "违纪违法": 3, "审查调查": 3, "开除党籍": 3, "落马": 3,
        "副省长": 3, "省长": 3, "副市长": 3, "市长": 3, "副县长": 3,
        "县委书记": 3, "省委书记": 3, "市委书记": 3,
        "受贿": 3, "贪污": 3, "被查": 2, "党内": 3, "从严治党": 3,
    },
    "经济": {
        "经济": 3, "股市": 3, "股票": 3, "股价": 3, "通胀": 3, "财政": 3,
        "央行": 3, "利率": 3, "关税": 3, "贸易": 3, "出口": 2, "进口": 2,
        "投资": 2, "债券": 3, "油价": 3, "亏损": 2, "营收": 3, "市值": 3,
        "并购": 3, "债券": 3, "汇率": 3, "GDP": 3, "减税": 3, "订单": 1,
        "期货": 3, "成交量": 3, "社会融资": 3, "外贸": 3, "贷款": 3, "存款": 3,
        "economy": 3, "stock": 3, "shares": 3, "inflation": 3, "tariff": 3,
        "trade": 2, "invest": 2, "earnings": 3, "revenue": 3, "bond": 3,
        "currency": 2, "market": 1, "acquire": 2, "holdings": 3, "portfolio": 3,
        "dividend": 3, "quarterly": 2, "profit": 2, "gdp": 3,
        # 补充（2026-10-20）：原油/基建/外贸类漏词
        "原油": 3, "布伦特": 3, "每桶": 3, "建筑业": 3, "航运": 3, "港口": 3,
        "财报": 3, "销售额": 3, "产值": 3, "制造业": 3, "招商": 2,
        "crude": 3, "barrel": 2, "shipping": 3, "manufacturing": 3,
        "博览会": 3, "进博会": 3, "广交会": 3, "交易会": 3,
    },
    "科技": {
        "人工智能": 3, "芯片": 3, "半导体": 3, "量子": 3, "卫星": 3, "机器人": 3,
        "算法": 3, "航天": 3, "火箭": 3, "5G": 3, "软件": 2, "数据中心": 3,
        "实验": 1, "研发": 3, "技术": 1, "自动驾驶": 3, "网络安全": 3,
        "artificial intelligence": 3, "semiconductor": 3, "quantum": 3,
        "satellite": 3, "robot": 3, "software": 2, "algorithm": 3,
        "spacecraft": 3, "innovation": 2, "cyber": 3, "drone swarms": 3,
        "prototype": 2, "tech": 2, "data center": 3, "launch vehicle": 3,
        # 补充（2026-10-20）：航天/器件类漏词
        "探测器": 3, "月球": 2, "航行器": 3, "传感器": 3, "算力": 3,
        "lunar": 3, "probe": 2, "sensor": 3,
    },
    "民生": {
        "医疗": 3, "医院": 3, "疫苗": 3, "教育": 3, "学校": 3, "养老": 3,
        "就业": 3, "失业": 3, "住房": 3, "房价": 3, "食品安全": 3, "污染": 3,
        "交通": 2, "地铁": 3, "医保": 3, "社保": 3, "物价": 3, "台风": 3,
        "洪水": 3, "地震": 3, "干旱": 3, "救援": 2, "受灾": 3, "市民": 3,
        "村民": 3, "乡村": 3, "农民": 3, "游客": 2, "患者": 3, "学生": 3,
        "居民": 2, "脱贫": 3, "振兴": 3, "消费": 2, "欺诈": 3, "维权": 3,
        "事故": 3, "消防": 3, "整治": 2, "考试": 3, "招聘": 3, "气温": 3,
        "health": 3, "hospital": 3, "vaccine": 3, "school": 3,
        # 防疫/健康科普（此前因「国务院」命中政治而整批错分到政治）
        "联防联控": 3, "疫情": 2, "就医": 2, "用药": 2, "接种": 2,
        "education": 3, "pension": 3, "housing": 3, "unemploy": 3,
        "pollution": 3, "disaster": 3, "flood": 3, "earthquake": 3,
        "rescue": 2, "patients": 3, "citizens": 2, "weather": 2,
    },
    "文化": {
        "文化": 3, "非遗": 3, "博物馆": 3, "文物": 3, "展览": 3, "电影": 3,
        "电视剧": 3, "音乐": 3, "文学": 3, "书法": 3, "戏曲": 3, "旅游": 3,
        "节日": 3, "春节": 3, "中秋": 3, "遗产": 3, "艺术": 3, "演出": 3,
        "culture": 3, "museum": 3, "film": 3, "movie": 3, "music": 3,
        "literature": 3, "festival": 3, "exhibition": 3, "heritage": 3,
        "art": 2,
        # 体育类词已**移出**文化、单独立区（2026-10-20）：
        # 原先这里塞着 选手/比赛/赛事/冠军/球/奥运/sport/tournament，
        # 导致体育要么被算成文化、要么（因为单字「球」被丢弃）落进「其他」。
    },
    "体育": {
        # 赛事名（强指示，且不会跨到别的分区）
        "体育": 3, "运动员": 3, "球队": 3, "球员": 3, "主教练": 3, "联赛": 3,
        "世界杯": 3, "世锦赛": 3, "锦标赛": 3, "亚运会": 3, "奥运会": 3, "奥运": 3,
        "全运会": 3, "亚运": 3, "大运会": 3, "公开赛": 3, "大师赛": 3,
        # 泛词降权：「冠军/奖牌/比赛」在「技能大赛」「汉语桥比赛」「设计大赛」
        # 里同样出现，权重 3 会让这些稿子被误判成体育（实测踩到）。
        # 降到 1~2 之后，它们不足以单独跨过 MIN_SCORE=3，
        # 而真体育稿仍可靠「亚运会/田径/金牌」等叠加达标。
        "冠军": 1, "夺冠": 2, "决赛": 2, "半决赛": 2, "亚军": 2, "季军": 2,
        "奖牌": 1, "金牌": 2, "银牌": 2, "铜牌": 2, "赛事": 2, "积分榜": 3,
        # 项目名
        "足球": 3, "篮球": 3, "排球": 3, "乒乓球": 3, "羽毛球": 3, "网球": 3,
        "游泳": 3, "田径": 3, "马拉松": 3, "滑雪": 3, "滑冰": 3, "拳击": 3,
        "体操": 3, "举重": 3, "跳水": 3, "射箭": 3, "击剑": 3, "跆拳道": 3,
        "选手": 1, "比赛": 1, "国足": 3, "中超": 3, "CBA": 3, "NBA": 3,
        "友谊赛": 3, "热身赛": 3, "预选赛": 3, "淘汰赛": 3, "四强": 3,
        "八强": 3, "东道主": 3, "进球": 3, "点球": 3, "比分": 3, "球赛": 3,
        "代表队": 2, "国家队": 2, "club": 2, "olympic": 3, "athlete": 3,
        "tournament": 3, "championship": 3,
        "world cup": 3, "league": 2, "playoff": 3, "medal": 3, "sport": 3,
        "wta": 3, "atp": 3, "fifa": 3, "coach": 2,
    },
    "历史": {
        # 党史军史与纪念（「长征」在本库里是高频词，此前全落进「其他」）
        "长征": 3, "红军": 3, "抗战": 3, "抗日": 3, "党史": 3, "先烈": 3,
        "烈士": 3, "抗日战争": 3, "解放战争": 3, "遵义会议": 3, "井冈山": 3,
        "建党": 3, "建军": 3, "建国": 3, "革命": 1, "延安": 2, "纪念": 2,
        "志愿军": 3, "抗美援朝": 3, "抗战胜利": 3, "反法西斯": 3,
        "侵华": 3, "万人坑": 3, "南京大屠杀": 3,
        # 考古与文物（「文物」在文化里也有，权重低于文化，不会抢走文化稿）
        "考古": 3, "出土": 3, "史前": 3, "遗址": 2, "文物": 2, "年代": 1,
        "archaeolog": 3, "dynasty": 3, "excavat": 3,
    },
    "社会": {
        # 地方治理与公共服务。与「民生」的分工：民生偏个人生活（医疗/教育/就业/住房），
        # 社会偏地方政务与社会面事件（印发/条例/营商环境/社区/公益/消费维权）。
        "省委": 3, "市委": 3, "省政府": 3, "市政府": 3, "自治区": 2,
        "印发": 2, "出台": 2, "条例": 3, "办法": 2, "部署": 2, "政务": 3,
        # 收紧为「志愿者/志愿服务」——原先的 `志愿` 会命中「志愿军」，
        # 把抗美援朝纪念稿抢成社会（审计实测）
        "营商环境": 3, "涉台": 3, "宗教": 3, "社区": 3,
        "志愿者": 3, "志愿服务": 3,
        "公益": 3, "慈善": 3, "维权": 3, "纠纷": 3, "投诉": 3,
        "预警": 2, "气象": 2, "民族": 2, "侨": 2, "便民": 3, "服务保障": 2,
    },
    "法治": {
        # 司法/公安/刑事（高精度，和军事、国际的用词基本不重叠）
        "法院": 3, "检察院": 3, "判决": 3, "判刑": 3, "起诉": 3, "涉案": 3,
        "诈骗": 3, "警方": 3, "公安": 3, "逮捕": 3, "犯罪": 3, "刑事": 3,
        "走私": 3, "毒品": 3, "立案": 3, "审理": 3, "诉讼": 3, "庭审": 3,
        "有期徒刑": 3, "死缓": 3, "治安": 3, "拘留": 3, "罚金": 3, "嫌疑": 3,
        "被害": 2, "庭审": 3, "赔偿": 2, "判决书": 3, "查处": 2, "打掉": 2,
        "court": 3, "prosecutor": 3, "verdict": 3, "indicted": 3, "fraud": 3,
        "smuggling": 3, "criminal": 2, "lawsuit": 3, "convicted": 3,
    },
    "国际": {
        "联合国": 3, "北约": 3, "欧盟": 3, "双边": 3, "外交关系": 3, "访问": 1,
        "峰会": 3, "全球": 1, "跨境": 3, "难民": 3, "和平谈判": 3,
        "停火": 3, "nato": 3, "united nations": 3, "eu ": 2, "bilateral": 3,
        "summit": 3, "refugee": 3, "ceasefire": 3, "treaty": 3,
        "foreign minister": 3, "international": 2,
        # 「国际」单独出现太泛（“国际期货市场”这种会把经济稿抢走），权重压到 1
        "国际": 1, "外交": 1,
        "政变": 3, "谴责": 2, "斡旋": 3, "国事访问": 3, "干涉": 2,
        "coup": 3, "condemn": 2, "mediate": 3,
    },
}

_WS_RE = re.compile(r"\s+")
# 图注/版式噪声：这些不是新闻内容，却会直接把稿子带偏。
#
# **实测踩到的坑**：中国新闻社的图片稿正文里写着「（无人机照片）」，
# 于是「河南郑州：秋日“粉黛花海”吸引游人」「三峡水库蓄水…」全被判成**军事**。
# 图注是署名信息，不是事实，判定前必须先剔掉。
_NOISE_RE = re.compile(
    r"[（(][^）)]{0,24}(?:图片|照片|资料图|航拍|摄|合影|供图)[^）)]{0,12}[）)]|"
    r"\([^)]{0,24}(?:photo|picture|image)[^)]{0,12}\)|"
    r"[（(](?:新华社|中新社|本报)[^）)]{0,30}摄[）)]|"
    r"图[为片]\s*[^。，；]{0,30}摄"
)


def strip_noise(text: str) -> str:
    """去掉图注/署名类噪声，剩下的才是可判分区的正文。"""
    return _NOISE_RE.sub("", text or "")


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _build_matcher(word: str):
    """把词表里的一个词编译成匹配器；不合适的词返回 None（直接丢弃）。

    **为什么不能简单用 ``word in text``**：实测发现子串匹配会系统性污染分区 ——
    英文 ``art`` 命中 ``article / start / smart / artillery``，
    ``match`` 命中 ``matches``，中文单字「军」几乎命中任何涉军或非军稿件，
    于是「文化」「军事」这些分区里混进大量无关稿子，而 ``MIN_SCORE`` 挡不住
    多个 2 分单字累加出来的假高分。

    规则：
    - 含汉字的词：至少 2 字（单字一律丢弃），仍按子串匹配（中文没有词边界）；
    - 纯 ASCII 词：用词边界正则，并允许常见的复数后缀
      （``missile`` 也要能命中 ``missiles``，否则英文稿会漏掉一大片）。
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    if _CJK_RE.search(w):
        return re.compile(re.escape(w)) if len(w) >= 2 else None
    if len(w) < 2:
        return None
    return re.compile(r"(?<![a-z0-9])" + re.escape(w) + r"(?:e?s)?(?![a-z0-9])")


_MATCHERS: Dict[str, List[Tuple[Any, int]]] = {
    topic: [(m, weight) for m, weight in
            ((_build_matcher(w), wt) for w, wt in words.items()) if m is not None]
    for topic, words in TOPIC_KEYWORDS.items()
}


def _doc_text(title: str, summary: str, summary_zh: str,
              title_zh: str = "") -> str:
    """取用来判分区的文本：中文译名优先（与 keywords 的口径一致）。"""
    parts = [title_zh or title or "", summary_zh or "", summary or ""]
    return strip_noise(" ".join(p for p in parts if p))[:MAX_DOC_CHARS]


def score_topics(text: str) -> Dict[str, float]:
    """给文本打各分区的分。返回 ``{分区: 分数}``（只含分数 > 0 的）。"""
    if not text:
        return {}
    haystack = _WS_RE.sub(" ", text).lower()
    scores: Dict[str, float] = {}
    for topic, matchers in _MATCHERS.items():
        hit = 0.0
        for matcher, weight in matchers:
            if matcher.search(haystack):
                hit += weight
        if hit > 0:
            scores[topic] = hit
    return scores


def classify(text: str) -> Tuple[str, str, Dict[str, float]]:
    """返回 ``(主分区, 次分区, 全部分数)``；无法判定时主分区为「其他」。"""
    scores = score_topics(text)
    if not scores:
        return OTHER, "", {}
    ranked = sorted(scores.items(),
                    key=lambda kv: (-kv[1], _TIE_RANK.get(kv[0], 99)))
    top, top_score = ranked[0]
    if top_score < MIN_SCORE:
        return OTHER, "", scores
    second = ""
    if len(ranked) > 1:
        name, sc = ranked[1]
        if sc >= MIN_SCORE and sc >= top_score * SECONDARY_RATIO:
            second = name
    return top, second, scores


def classify_row(row: Any) -> Tuple[str, str, Dict[str, float]]:
    text = _doc_text(row["title"], row["summary"], row["summary_zh"],
                     row["title_zh"] if "title_zh" in row.keys() else "")
    return classify(text)


def classify_all(conn: sqlite3.Connection, verbose: bool = True,
                 only_missing: bool = False) -> Dict[str, Any]:
    """给全库文章判分区，写入 ``articles.topic`` / ``articles.topic2``。

    :param only_missing: 只处理还没判过的（新采集的文章）
    """
    sql = ("SELECT id, title, title_zh, summary, summary_zh FROM articles"
           + (" WHERE topic = ''" if only_missing else ""))
    rows = conn.execute(sql).fetchall()
    if not rows:
        if verbose:
            print("  [TOPIC] 没有待判分区的文章")
        return {"articles": 0, "by_topic": {}}

    payload: List[Tuple[str, str, int]] = []
    counter: Dict[str, int] = {}
    for r in rows:
        topic, topic2, _ = classify_row(r)
        payload.append((topic, topic2, r["id"]))
        counter[topic] = counter.get(topic, 0) + 1

    conn.executemany("UPDATE articles SET topic = ?, topic2 = ? WHERE id = ?",
                     payload)
    conn.commit()

    result = {"articles": len(rows), "by_topic": counter}
    if verbose:
        total = len(rows)
        print(f"  [TOPIC] 已判分区 {total} 篇")
        for name, n in sorted(counter.items(), key=lambda kv: -kv[1]):
            print(f"          {name:<4} {n:>5} 篇（{n * 100.0 / total:.0f}%）")
    return result


def stats(conn: sqlite3.Connection) -> List[Tuple[str, int]]:
    """各分区的文章数（供界面筛选下拉框）。"""
    return [(r["topic"] or OTHER, r["n"]) for r in conn.execute(
        "SELECT topic, COUNT(*) n FROM articles GROUP BY topic ORDER BY n DESC")]
