"""简报生成：把事件簇的材料交给开源模型，产出结构化简报。

**结构化输出的意义**

本模块不做"让模型随意写一段话"，而是要求模型输出严格 JSON，且**每一条事实句
都必须标注它在输入材料中的编号**。这是下游 :mod:`src.verify` 做事实一致性
校验的前提 —— 也是本作品相对"通用摘要工具"的实质改进之一。

输出结构：

.. code-block:: json

    {
      "headline":    "（例：某公司宣布新型拦截弹完成首次试射）",
      "value_point": "（例：这是该型拦截弹首次公开试射结果）"
      "brief":       "约 300 字简报正文",
      "claims":      [{"sentence": "简报中的一句", "source_index": 1}, ...],
      "conflicts":   ["不同来源说法不一致之处", ...]
    }

设计上刻意规避的几种输出（对应赛题对"科技伦理"与"AI 输出风险"的要求）：
不做预测、不做意图推测、不表达立场、不补充材料之外的背景知识。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple
import json
import re
import sqlite3

from . import config, credibility, llm, normalize, store

# 生成一次简报最多为多少篇外文材料做「实时归一化」。
#
# 每归一化一篇就要调一次模型（单次上限 600 token）。一个 5 篇的英文事件，
# 会在正式生成**之前**先花掉四五次推理 —— 实测这是「生成简报很慢」的两大主因之一。
# 真正的解法是离线把 ``summary_zh`` 预热好
# （``scripts/warm_translations.py --with-summary``）；这里的上限只是保证
# 「没预热过」时也不至于卡到不可接受。
MAX_NORMALIZE_PER_EVENT = 3

# 单次生成的 max_new_tokens 上限。
# 配置里写死的 1200 是「够用就好」的粗放值：目标 120 字的简报根本用不到，
# 但小模型不会自己停，多出来的预算全是白等的时间。
LOW_MATERIAL_FACTS = 80
# 材料里可核实事实低于这个字数，就认为"抓到的正文太少"。
# 实测（2026-10-15）：某事件 2 篇来源，一篇 content 为空（源站 HTML 抽不到正文），
# 另一篇只有 19 字且与标题重复 → 材料事实字数 = 19，简报就成了"标题重复一遍"。
# 用户看到"原文明明很多、生成的却只有一句话"，而**根因在采集层不在生成层**，
# 所以这种情形必须把原因摆出来，不能静静产出一句话让人以为模型坏了。

# 单次生成的 token 上限。
# 目标字数上限提高后（300 -> 700），1024 会在写到一半时被硬截断：
# 700 字正文 + claims 逐句复述原文 ≈ 1500+ 字 ≈ 2000+ token。
# 代价是生成更慢（本机 3B 4bit 约 20~35 tok/s，满上限约 60~100 秒）。
MAX_NEW_TOKENS_CAP = 2048


def max_tokens_for(brief_chars: int, extra: int = 0) -> int:
    """按目标字数估算单次生成需要的 max_new_tokens。

    JSON 里 ``brief`` 与 ``claims`` 各占一份正文（claims 要求逐句列出简报原文），
    所以按目标字数的 2.2 倍估字符数；中文约 1.5 token/字；再留 200 给
    headline/value_point/conflicts 这些结构化开销。
    """
    need = int(int(brief_chars) * 2.2 * 1.5) + 200 + int(extra)
    return max(320, min(MAX_NEW_TOKENS_CAP, need))

SYSTEM_PROMPT = (
    "你是一名严谨的新闻简报编辑。你的信息来源只有用户提供的、编号为 [1] [2] … 的"
    "多来源报道材料。你必须只使用材料中出现的事实与观点，不得补充任何背景知识。"
    "你可以（而且应该）引用材料里**署名机构与专家**的表态和评论，但必须写清是谁说的；"
    "**绝对不允许编造机构名、专家姓名，或材料里不存在的观点**。"
    "你不得表达自己的立场，也不得做材料之外的推测。"
)

_BRIEF_TEMPLATE = """下面是一个事件的 {n_sources} 个来源共 {n_articles} 篇报道材料，编号 [1] 到 [{n_articles}]。
其中材料语言可能是中文或英文；无论原文语种，你的输出一律使用**简体中文**。

要求你产出一份结构化简报，输出**严格 JSON**（不要输出任何解释性文字、不要用 Markdown 代码围栏）：

{{
  "headline": "（例：某公司宣布新型拦截弹完成首次试射）",
  "value_point": "（例：这是该型拦截弹首次公开试射结果）"
  "brief": "简报正文，**不超过 {brief_max} 字**，分 {n_paragraphs} 段，段与段之间用 \\n 分隔。建议结构：导语（核心事实）→ 关键细节 → **各方反应与观点（须写明“据谁表示”）** → 后续安排或待观察之处。段与段之间要有逻辑承接，读起来是一篇文章，不是条目录",
  "claims": [
    {{"sentence": "简报中的某一句原文", "source_index": 1}},
    {{"sentence": "简报中的另一句原文", "source_index": 2}}
  ],
  "conflicts": ["若不同来源的说法不一致，在此逐条说明；没有则留空数组"]
}}

关于 `headline` 与 `value_point`（**不要照抄上面的「（例：…）」**）：
- `headline`：{headline_guide}；把核心事实说完整。
- `value_point`：{value_point_guide}。

硬性约束（违反任一条，整份输出都会被判为不合格）：
1. **brief 的长度上限是 {brief_max} 字，但字数不是目标。**
   材料里有多少可核实的事实就写多少，写完为止；**比上限短是正常的，不会被判不合格**。
   材料的事实不足以支撑长文时，写短才是正确做法。
2. **严禁为了凑字数而重复。** 不得换词重说已经说过的同一件事，
   不得用同义反复、排比、总结句把长度填上去。
   「把每个事实都说一遍」是目标，「把同一件事说两遍」直接判为不合格。
3. **引用观点必须署名。** 材料里若有署名机构的表态、专家评论、各方反应，
   要写进简报并标明「据XX（机构名或专家姓名）表示／分析认为」。
   **材料里没有的观点一律不得写入，绝对不得编造机构名或专家姓名。**
   材料里确实没有多方观点时，就只写事实，不要硬凑观点。
4. claims 必须覆盖 brief 中的**每一个句子**，且句子文本要与 brief 里**逐字一致**。
3. 只使用材料中出现的事实，**不得**加入材料之外的任何知识或背景。
4. **禁止无署名地推测**：不要写「或将」「可能会」「有望」这类**没有出处**的判断。
   若材料里确有署名人这么判断，就**按引用写**（「XX预计…」），不要写成你自己的结论。
5. 若材料之间存在矛盾，必须在 conflicts 中指出，不要擅自选择其中一个。
6. 不要提及"材料""来源编号"这类元信息，简报本身要读起来像正式新闻简报。

---

以下是材料：

{material}
"""

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")
# 用于从被截断的输出里抢救字段（小模型容易在 max_new_tokens 处被截断）
_STR_FIELD_RE = re.compile(r'"([A-Za-z_]+)"\s*:\s*"((?:[^"\\]|\\.)*)"')
_CLAIM_OBJ_RE = re.compile(
    r'\{\s*"sentence"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*'
    r'"source_index"\s*:\s*(-?\d+)\s*\}'
)


def _unescape(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except (ValueError, json.JSONDecodeError):
        return raw


def salvage_fields(text: str) -> Optional[Dict[str, Any]]:
    """从被截断或格式不严格的输出中抢救字段。

    小模型经常在 max_new_tokens 处被截断，导致 JSON 缺少收尾括号而整体解析失败。
    此时逐字段把已有内容捞出来，避免整条结果作废 —— 拿到残缺但可校验的内容，
    比直接放弃更好（拿不到的部分会由校验层判定为未声明依据）。
    """
    if not text:
        return None
    fields = {k: _unescape(v) for k, v in _STR_FIELD_RE.findall(text)}
    claims = [
        {"sentence": _unescape(s), "source_index": int(idx)}
        for s, idx in _CLAIM_OBJ_RE.findall(text)
    ]
    headline = fields.get("headline", "").strip()
    brief_text = fields.get("brief", "").strip()
    if not headline and not brief_text:
        return None
    return {
        "headline": headline,
        "value_point": fields.get("value_point", "").strip(),
        "brief": brief_text,
        "claims": claims,
        "conflicts": [],
        "_salvaged": True,
    }


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出中尽力抠出 JSON 对象（模型常会加代码围栏或前后缀）。"""
    if not text:
        return None
    candidates: List[str] = []
    for m in _JSON_BLOCK_RE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            # 常见问题：尾随逗号
            try:
                fixed = re.sub(r",\s*([}\]])", r"\1", cand)
                data = json.loads(fixed)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    # 到最后一步：尝试从被截断的输出里抢救字段
    return salvage_fields(text)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _as_text(value: Any) -> str:
    """把模型可能给出的各种类型统一成字符串。

    实测小模型常把 `brief` 写成**字符串数组**而不是字符串（按段分更自然），
    直接 `str()` 会得到 Python 列表字面量（形如 `['第一段', '第二段']`），
    既不是给人读的，也会让长度校验失真。这里显式拼接。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        parts = [_as_text(v) for v in value]
        joined = "".join(p for p in parts if p)
        if any(p and not p.endswith(("。", "！", "？", ".", "!", "?")) for p in parts):
            joined = " ".join(p for p in parts if p)
        return joined.strip()
    if isinstance(value, dict):
        return " ".join(_as_text(v) for v in value.values()).strip()
    return str(value).strip()


# --------------------------------------------------------------------------
# 标题与价值点：**不设硬字数上限**
#
# 实测踩到的坑：旧配置把 headline_chars 设为 20，正常新闻标题会被砍成半句 ——
# 用户看到的正是「标题说一半就停了」。库内 14 份已保存简报里的实例：
#     四川省华蓥市人大常委会原党组书记、主任刘      ← 人名被截断
#     习近平和彭丽媛出席特朗普总统夫妇举行的欢        ← 截断
#     US Army transfers 60                      ← 截断
#     两名美国经济学家获得 2026 年诺贝尔经济        ← 截断
# 20 字对中文新闻标题实在太短（正常 15~35 字），而其中 12 份走的是
# **抽取式兜底**路径，说明硬截断同时存在于模型路径和兜底路径。
#
# 现在：提示词里不再声明字数上限；代码只保留一个「防跑飞护栏」——
# 小模型偶尔会把整段话塞进 title，靠 _clip 在标点处断开兜住。
# 这**不是写作目标**，正常标题永远不会碰到它。
HEADLINE_SOFT_MAX = 48        # 护栏，不是上限目标
VALUE_POINT_SOFT_MAX = 60     # 同上

HEADLINE_GUIDE = (
    "客观、完整、通顺的标题：把核心事实说完整，能独立看懂；"
    "不要为了短而截断句子，也不要写成一段话"
)
VALUE_POINT_GUIDE = (
    "一句话说明这条信息为什么值得关注（只陈述事实层面的原因，"
    "不做价值判断与预测）"
)


# 模型把**提示词原文/示例**照抄进字段时的兜底。
#
# 实测（2026-10-15）：把"标题要完整通顺"这类**要求**写进 JSON 的**值**位置后，
# 3B 直接把那句话当标题输出了：
#     headline = "客观、完整、通顺的标题：把核心事实说完整……也不要写成一段话"
# 小模型分不清"值"与"对值的要求"。两道防线：
#   ① schema 里改成明确标出的「（例：…）」，要求移到约束区；
#   ② 这里再兜一次：命中说明性词句或例子就判无效，由调用方派生。
_ECHO_MARKERS = (
    "客观、完整、通顺", "把核心事实说完整", "不要为了短而截断", "不要写成一段话",
    "一句话说明这条信息为什么值得关注", "（例：", "(例：", "不超过 20 字",
    "某公司宣布新型拦截弹",
)


def _clean_field(text: str, limit: int) -> str:
    """清掉模型回显的说明文字/示例；无效时返回空串（调用方负责回退）。"""
    t = _clip(text, limit)
    if not t:
        return ""
    if any(m in t for m in _ECHO_MARKERS):
        return ""
    if t[-1] in "：:，,、；;":      # 显然没写完
        return ""
    return t


def _clip(text: str, limit: int) -> str:
    """按字数上限裁剪，尽量在标点处断开，避免截断到一半的词。"""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("，", "、", "；", "：", "（", ",", ";", " "):
        pos = head.rfind(sep)
        if pos >= limit * 0.55:
            return head[:pos]
    return head


def _normalize_claims(claims: Any) -> List[Dict[str, Any]]:
    """把 claims 规范成 [{"sentence": str, "source_index": int}, ...]。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(claims, (list, tuple)):
        return out
    for item in claims:
        if isinstance(item, dict):
            sentence = _as_text(item.get("sentence"))
            try:
                idx = int(item.get("source_index"))
            except (TypeError, ValueError):
                idx = -1
            if sentence:
                out.append({"sentence": sentence, "source_index": idx})
        elif isinstance(item, str) and item.strip():
            out.append({"sentence": item.strip(), "source_index": -1})
    return out


# 追加在 prompt 末尾的可核验性硬约束。
# 实测：不加这条时，3B 会为了语感大幅同义改写，句级词面覆盖率只有 25%
# （阈值 80%），而且声明的 source_index 两个全错 → 每次生成都被校验拦下、降级。
# 加这条并不是"放学校验"，而是把"以原文措辞为主"这个前提明确告诉模型 ——
# 可溯源是硬指标，不靠事后宽松放过。
TRACEABILITY_HINT = (
    "\n\n【可核验性硬约束（违反则整份作废）】\n"
    "1. 简报每句话都必须能在材料里找到对应的**原文措辞**：以材料用词为主，\n"
    "   不要为了语感而换成同义词。确实需要概括时，保留材料中的关键词与专有名词。\n"
    "2. `claims` 里的 `source_index` 必须是该句**真实取材**的材料编号，不得猜测或全填 1。\n"
    "3. 材料里没写到的事实一律不写——宁缺勿编。\n"
    "4. 数字、人名、机构名、奖项名必须与材料逐字一致。\n"
)


def adaptive_brief_chars(material: str, base: int) -> int:
    """按素材厚度自适应目标简报字数。

    为什么需要（实测发现的系统性缺陷）：``brief_chars`` 是**固定值**（默认 300），
    但素材厚度差异极大 —— 同一批数据里，中国新闻社的 RSS 只给短摘要
    （某个事件 3 篇合计仅 **267 字**），而英文军事站点的事件可达 **12000+ 字**。
    用 267 字素材要求模型写 300 字简报，物理上不可能完成，
    结果必然是「长度偏离目标 + 依据覆盖率不足」→ 校验不过 → 降级为抽取式摘要。
    表面上像"生成层不可用"，根因却是目标字数没跟着素材走。

    规则：``max(MIN_BRIEF_CHARS, min(上限, 事实字数 * BRIEF_RATIO))``
    （当前取值 60 / 0.34 / 上限 700，三个常量见本函数下方与 config/llm.yaml）。

    **``base`` 的语义是「上限」而不是「目标」**：素材厚就写长，素材薄就精炼。
    实际效果（事实字数 -> 目标字数）：300 -> 102、1000 -> 340、2000 -> 680、3000+ -> 700。
    早期 ``brief_chars`` 被当成固定目标（300），于是再厚的素材也只能写 300 字，
    与「原文内容丰富、简报就多写」的诉求相反 —— 这是 2026-10-15 修正的。

    由于 prompt、扩写、校验三处都从 ``cfg["task"]["brief_chars"]`` 取值，
    :func:`generate_brief` 只需覆盖这一处，全链路口径自动一致。
    """
    facts = fact_chars(material)
    return max(MIN_BRIEF_CHARS, min(int(base), int(facts * BRIEF_RATIO)))


# —— 简报长度：按素材里的事实量定，而不是按材料总长 ——
MIN_BRIEF_CHARS = 60      # 再薄的素材也至少写这么多（低于此就没必要出简报）
BRIEF_RATIO = 0.34        # 简报字数 / 事实字数。实测 0.45 会明显逼出凑字数的长文
_BOILER_RE = re.compile(
    r"^\[\d+\] 来源：.*$|^时间：.*$|^标题：.*$|^正文：|"
    r"（已由开源模型归一化为中文事实要点）|（外文材料[^）]*）|"
    r"（该源未提供正文，仅有标题）", re.M)


# 事实量去重的相似度阈值。
# 取得比 :func:`dedup_sentences`（0.55，用于收拾模型的"换词重说"）**更高**：
# 这里只想去掉"同一篇稿子被多家转发"这种近乎逐字相同的句子，
# 绝不能把"同结构但不同事实"的两句（如"提供100套发射装置" vs "提供100套通信设备"）
# 当成重复删掉 —— 那会把真实信息量算少，反而让简报写得更短。
FACT_DEDUP_THRESHOLD = 0.80


def _dedup_fact_text(text: str, threshold: float = FACT_DEDUP_THRESHOLD) -> str:
    """去掉**近重复**的句子，返回用于计算事实量的文本。

    判定：4-gram 集合的交集占较短句的比例 >= ``threshold``。
    同时丢掉长度 < 6 的碎片（图注、"编辑：××"、"来源：××" 之类），
    它们不是事实，却会被计进字数。
    """
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？；;!?\n])", text)
    kept = []
    grams = []
    for raw in parts:
        s = raw.strip()
        if len(s) < 6:
            continue
        g = {s[i:i + 4] for i in range(max(0, len(s) - 3))}
        if g and any(o and len(g & o) / min(len(g), len(o)) >= threshold
                     for o in grams):
            continue
        kept.append(s)
        grams.append(g)
    return "".join(kept)


def fact_chars(material: str) -> int:
    """材料里**真正的事实文本**有多少字（去掉版式与标注）。

    为什么要单独算：``build_material`` 给每篇文章都拼了
    「[1] 来源：…（机构：…，等级 tier2，地区 cn）／时间：…／标题：…／正文：」，
    实测这些版式占材料的三分之一以上。之前直接用**材料总长**推目标字数，
    等于「版式越长、要求写越多」，薄素材被硬逼出凑字数的长文 ——
    用户反馈的「大部分是重复内容」正是这么来的。
    """
    text = _BOILER_RE.sub("", material or "")
    # **跨来源近重复句只算一次**（2026-10-20 修的）。
    #
    # 实测证据：事件「习近平：以更高标准、更实举措推进全面从严治党」有 2 篇材料，
    # 国防部与中新社发的是**同一篇稿子**。正文回填把第二篇补齐后，
    # 事实字数从 1512 涨到 **2988**（翻倍），而本地 3B 的简报长度
    # **一点没变（322 → 322）** —— 因为新增的全是重复内容，模型没有更多事实可写。
    # 于是 ``BRIEF_RATIO × 事实字数`` 把目标顶到 700（上限），
    # 结构性地不可能达到，校验必然以"长度不达标"失败并降级。
    #
    # 这与项目立意也是一致的：多源的价值是**独立印证**（由 credibility 的
    # f_cross 按机构计数衡量），不是把同一篇稿子的字数叠加成"信息量"。
    text = _dedup_fact_text(text)
    return len(re.sub(r"\s+", "", text))





def dedup_sentences(text: str, threshold: float = 0.55) -> Tuple[str, int]:
    """去掉**近义重复**的句子（模型的「换词重说」就靠这一步收拾）。

    判定用 4-gram 的包含度（短句被已保留句子覆盖多少），而不是字符相等。
    阈值取得比想象中保守（0.55）：
    「该合同还包括提供100套PrSM导弹发射装置」与「该合同还包括提供100套通信设备」
    这种同结构句包含度很高，但它们其实是**不同的事实**，不能当重复删掉。
    返回 (去重后的文本, 丢弃的句数)。
    """
    parts = re.split(r"(?<=[。！？；;])", text or "")
    kept: List[str] = []
    grams: List[set] = []
    dropped = 0
    for raw in parts:
        s = raw.strip()
        if not s:
            continue
        g = {s[i:i + 4] for i in range(max(0, len(s) - 3))}
        if g and any(o and len(g & o) / min(len(g), len(o)) >= threshold
                     for o in grams):
            dropped += 1
            continue
        kept.append(s)
        grams.append(g)
    return "".join(kept), dropped


def build_material(
    conn: sqlite3.Connection,
    event_id: str,
    max_total_chars: int = 6000,
    per_article_chars: int = 1500,
    backend: Optional[llm.BaseBackend] = None,
    cfg: Optional[Dict[str, Any]] = None,
    max_normalize: int = MAX_NORMALIZE_PER_EVENT,
) -> Tuple[str, List[Dict[str, Any]]]:
    """把事件簇的文章拼成编号材料。返回 (材料文本, 来源列表)。

    排序原则：来源等级高、时间新的排在前面，保证优先进入上下文的材料最可靠。

    **非中文材料会先归一化为中文事实要点**（见 :mod:`src.normalize`），
    否则下游的词面校验会因语种不同而把正确的翻译型简报误判为无依据。
    """
    rows = conn.execute(
        "SELECT id, source_name, source_org, source_tier, region, lang, title, "
        "       summary, summary_zh, content, "
        "       url, COALESCE(published_at, fetched_at) AS ts "
        "FROM articles WHERE event_id = ? "
        "ORDER BY source_tier ASC, ts DESC",
        (event_id,),
    ).fetchall()

    blocks: List[str] = []
    sources: List[Dict[str, Any]] = []
    used = 0
    normalized = 0
    for idx, row in enumerate(rows, 1):
        raw_body = (row["content"] or "").strip() or (row["summary"] or "").strip()
        zh_facts = (row["summary_zh"] or "").strip()
        note = ""
        if not zh_facts and normalize.needs_normalize(
            row["lang"], f"{row['title']}\n{raw_body}"
        ):
            if backend is not None and normalized < max_normalize:
                zh_facts = normalize.ensure_article_chinese(
                    conn, row, backend=backend, cfg=cfg
                )
                normalized += 1
                note = "（已由开源模型归一化为中文事实要点）"
            elif backend is None:
                note = "（外文材料，本次未归一化）"
            else:
                note = (f"（外文材料，本轮已达实时归一化上限 {max_normalize} 篇；"
                        f"离线预热 summary_zh 可消除这个开销）")
        body = zh_facts or raw_body
        body = _truncate(body, per_article_chars)
        block = (
            f"[{idx}] 来源：{row['source_name']}（机构：{row['source_org']}，"
            f"等级 tier{row['source_tier']}，地区 {row['region']}）{note}\n"
            f"时间：{row['ts']}\n"
            f"标题：{row['title']}\n"
            f"正文：{body or '（该源未提供正文，仅有标题）'}"
        )
        if used + len(block) > max_total_chars and blocks:
            break
        blocks.append(block)
        used += len(block)
        sources.append({
            "index": idx,
            "source_name": row["source_name"],
            "source_org": row["source_org"],
            "tier": int(row["source_tier"]),
            "region": row["region"],
            "title": row["title"],
            "url": row["url"],
            "time": row["ts"],
            "text": body,
            "normalized": bool(zh_facts) and bool(note),
        })

    return "\n\n".join(blocks), sources


# 署名观点句的识别：<署名> + <观点动词>
#
# 为什么要单独检索：RSS 摘要里的观点句往往淹在事实句里，小模型会直接忽略
# 它们、只写事实。把观点句**单独列出来**喂给模型，引用率明显提高；
# 而且正因为是「从材料里抠出来的」，所以不可能凭空出现材料外的专家。
_VIEW_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9·（）()]{2,18}"
    r"(表示|指出|认为|强调|警告|透露|声称|分析认为|预计|呼吁|称)"
)
_SENT_SPLIT = re.compile(r"(?<=[。！？；;])\s*|(?<=\.)\s+")


def extract_viewpoints(sources: Sequence[Dict[str, Any]],
                       limit: int = 8) -> List[str]:
    """从材料里检索**带署名的观点句**，带上来源编号返回。

    返回形如 ``["[1] 洛克希德公司高管表示，…", …]``；
    没有署名观点的材料返回空列表（此时简报只写事实，不硬凑观点）。
    """
    out: List[str] = []
    for s in sources:
        for sent in _SENT_SPLIT.split(str(s.get("text") or "")):
            sent = sent.strip()
            if len(sent) >= 8 and _VIEW_RE.search(sent):
                out.append(f"[{s.get('index', '?')}] {sent}")
                if len(out) >= limit:
                    return out
    return out


def build_prompt(material: str, n_articles: int, n_sources: int,
                 cfg: Optional[Dict[str, Any]] = None) -> str:
    task = (cfg or llm.load_llm_config())["task"]
    return _BRIEF_TEMPLATE.format(
        material=material,
        n_articles=n_articles,
        n_sources=n_sources,
        headline_guide=HEADLINE_GUIDE,
        value_point_guide=VALUE_POINT_GUIDE,
        brief_chars=int(task.get("brief_chars", 300)),
        brief_min=int(int(task.get("brief_chars", 300)) * 0.85),
        brief_max=int(int(task.get("brief_chars", 300)) * 1.15),
        n_paragraphs=int(task.get("brief_paragraphs", 3)),
    )


_EXPAND_TEMPLATE = """下面是一个事件的多来源材料，以及一份**字数不足**的简报草稿。
草稿当前 {cur} 字，要求 {lo}~{hi} 字。

请把草稿**扩写**为 {lo}~{hi} 字的正式简报，分 {n_paragraphs} 段。
输出**严格 JSON**，结构与草稿相同：

{{
  "headline": "（例：某公司宣布新型拦截弹完成首次试射）",
  "value_point": "（例：这是该型拦截弹首次公开试射结果）"
  "brief": "{lo}~{hi} 字的完整简报，分 {n_paragraphs} 段",
  "claims": [{{"sentence": "简报中的某一句", "source_index": 1}}],
  "conflicts": []
}}

硬性要求（违反任一条即不合格）：
1. **只能使用材料中出现的事实**，绝对不得添加材料之外的任何信息；
2. 扩写方式是：把材料里**被草稿漏掉的可核实事实**补进来
   （金额、装备型号、时间、地点、此前的进展、后续安排、各方表态等），
   而不是加形容词、评论或背景知识；
3. 不得使用"或将""预计""分析认为"等推测性表述；
4. claims 要覆盖扩写后 brief 的**每一句**，句子文本与 brief 逐字一致。

---

材料：

{material}

---

草稿：

{draft}
"""


def expand_brief(
    backend: llm.BaseBackend,
    material: str,
    draft: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """把过短的草稿定向扩写到目标字数。

    1.5B 级小模型一次很难写满 300 字（实测常见 100~180 字）。
    与其降级放弃，不如拿同一份材料再要求它把漏掉的事实补进来 ——
    这比“重新生成一遍”更有针对性，也不会引入材料之外的信息。
    """
    task = cfg.get("task", {})
    brief_chars = int(task.get("brief_chars", 300))
    lo, hi = int(brief_chars * 0.85), int(brief_chars * 1.15)
    prompt = _EXPAND_TEMPLATE.format(
        cur=len(str(draft.get("brief") or "")),
        lo=lo, hi=hi,
        n_paragraphs=int(task.get("brief_paragraphs", 3)),
        headline_guide=HEADLINE_GUIDE,
        value_point_guide=VALUE_POINT_GUIDE,
        material=material,
        draft=(str(draft.get("brief") or "")),
    )
    gen = backend.generate(prompt, system=SYSTEM_PROMPT,
                           max_new_tokens=max_tokens_for(brief_chars))
    parsed = extract_json(gen.text)
    if not parsed:
        return None
    expanded_text = _as_text(parsed.get("brief"))
    draft_len = len(str(draft.get("brief") or ""))
    exp_len = len(expanded_text)
    tol = float(task.get("brief_chars_tolerance", 0.25))
    # 采纳条件不能只看「更长」：
    #   1) 必须比草稿**更接近目标字数**；
    #   2) 必须落进长度校验的容差带内 —— 否则会出现「扩写结果被采纳、
    #      随后又因长度不合规而被降级」的自相矛盾。
    # 实测 1.5B 的草稿不足 255 字时会被扩写到 380~390 字（目标 300），
    # 旧规则（只比长度）会照单全收，导致简报明显偏长却仍显示「长度合规」。
    if exp_len <= draft_len:
        return None
    if abs(exp_len - brief_chars) >= abs(draft_len - brief_chars):
        return None
    if abs(exp_len - brief_chars) > brief_chars * tol:
        return None
    return {
        "headline": _clean_field(_as_text(parsed.get("headline")), HEADLINE_SOFT_MAX)
                    or _clip(str(draft.get("headline") or ""), HEADLINE_SOFT_MAX),
        "value_point": _clip(_as_text(parsed.get("value_point"))
                             or str(draft.get("value_point") or ""),
                             VALUE_POINT_SOFT_MAX),
        "brief": expanded_text,
        "claims": _normalize_claims(parsed.get("claims")),
        "conflicts": [_as_text(c) for c in (parsed.get("conflicts") or [])],
        "_expand_ms": gen.elapsed_ms,
    }


def _fallback_extractive(sources: Sequence[Dict[str, Any]], reason: str,
                        target_chars: Optional[int] = None) -> Dict[str, Any]:
    """抽取式兜底：模型不可用或校验不过时，直接用最高等级来源的原文句子组简报。

    宁可给出一段"不好看但完全忠实于原文"的简报，也不输出可能失真的生成内容。
    """
    if not sources:
        return {}
    best = min(sources, key=lambda s: (s["tier"], s["index"]))
    # 中英文句子切分：中文句末标点，以及英文的 ". "（点号后跟空白）
    sentences = re.split(r"(?<=[。！？；!?;])\s*|(?<=\.)\s+", best.get("text") or "")
    # 目标：兜底稿也要落在长度判据的下限之上，否则"降级后仍判不通过"。
    # **曾有的 bug**：这里写死 280 字，而目标由 adaptive_brief_chars 给出
    # （材料 1512 事实字 → 目标 514）。实测兜底稿卡在 308 字，
    # 于是即使降级也永远显示"简报长度偏离目标"，界面像是一直在报错。
    goal = 280
    if target_chars:
        goal = max(280, int(int(target_chars) * 0.85))
    picked: List[str] = []
    total = 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        picked.append(s)
        total += len(s)
        if total >= goal:
            break
    if not picked:
        picked = [(best.get("text") or "")[:300]]
    # 有些源只提供标题、没有正文（实测个别栏目如此），上面会兜出一个**空简报**。
    # 诊断时实测到「降级=True 且 brief 长度=0」就是这条路径。
    # 宁可退到标题，也不能交白卷。
    if not "".join(picked).strip():
        fallback_text = str(best.get("title") or "").strip()
        picked = [fallback_text[:300]] if fallback_text \
            else ["（该事件暂无可用正文，仅有来源标题）"]
    # —— 素材不足：只抓到标题、没有正文 ——
    # 这时再输出"抽取式摘要"就是标题重复一遍，会被误读成"生成功能坏了"。
    # 直接说明原因，并列出各来源标题（谁是标题级、谁有正文，一眼看清）。
    if total < LOW_MATERIAL_FACTS:
        thin = [s for s in sources
                if len(str(s.get("text") or "").strip()) < 40]
        # 注意用"全来源里最长的一篇"，不要用 total ——
        # total 只累计了 best（最高等级）那一篇，若它恰好没正文就会显示"0 字"，
        # 而另一篇其实有几十字的摘要，报出来的数会让人以为系统算错了。
        _lens = [len(str(s.get("text") or "").strip()) for s in sources] or [0]
        lines = [
            f"**本条只提供标题级摘要：全来源里最长的一篇正文只有 {max(_lens)} 字"
            f"（{len(sources)} 篇合计 {sum(_lens)} 字）。**",
            "",
            f"原因：{len(sources)} 篇来源里有 **{len(thin)} 篇没抓到正文**"
            "（源站页面结构变化、或需要 JS 渲染）。"
            "这不是生成模型的问题 —— 送给模型的材料本身就只有标题。",
            "",
            "各来源标题：",
        ]
        for s in sorted(sources, key=lambda x: x.get("index") or 0):
            lines.append(
                f"- **[{s.get('index')}] {s.get('source_org') or s.get('source_name')}**："
                f"{str(s.get('title') or '').strip()}"
                f"{'' if len(str(s.get('text') or '').strip()) >= 40 else '（未抓到正文）'}"
            )
        lines += ["", "> 想看完整内容请点上方链接；正文抓取可对具体源做适配（见 `src/parsers_html.py`）。"]
        brief = "\n".join(lines)
    else:
        # 中文直接拼接，英文用空格拼接，避免单词粘连
        joiner = "" if re.search(r"[\u4e00-\u9fff]", "".join(picked)) else " "
        brief = joiner.join(picked)
    # 抽取式也削一遍重复：同一篇正文里复现过的同义句不必出现两次
    # （阈值放宽到 0.8，避免把同结构但不同事实的句子误删）
    brief, _dropped = dedup_sentences(brief, threshold=0.8)

    # 兜底标题同样不再硬截到 20 字 —— 这是用户看到「说一半就停」的主要来源
    # （库内 14 份简报里有 12 份走的是这条路径）。直接沿用来源标题全文，
    # 只有明显过长时才在标点/空格处断开。
    headline = _clip(str(best["title"] or ""), HEADLINE_SOFT_MAX)

    # **曾有的 bug**：这里把**整篇简报**当成一条 claim 声明
    #     "claims": [{"sentence": brief, "source_index": best["index"]}]
    # 校验器要把它 match 回**单句**，308 字的整篇跟任何一句的 Jaccard 都到不了
    # 0.45 → 落进 unmatched_claims → 界面报「有 1 条声明与简报正文对不上」。
    # 实测抽取式产物**每句覆盖率都是 1.00**（它就是原文），却因这条假警报
    # 看起来像"内容没有依据"。改成逐句声明。
    from .verify import split_sentences          # 局部导入避免循环依赖
    _claims = [{"sentence": s, "source_index": best["index"]}
               for s in split_sentences(brief)]
    return {
        "headline": headline,
        "value_point": "（抽取式兜底：未使用生成模型）",
        "brief": brief,
        "claims": _claims,
        "conflicts": [],
        "_fallback": True,
        "_fallback_reason": reason,
    }


def _drop_unsupported(brief_text, verification):
    """剔除校验报告里 ``supported=False`` 的句子，返回 (新正文, 被剔除的句子列表)。

    按句切分后**用原串替换**删除，而不是"重新拼接" ——
    英文句子是用 ``(?<=\.)\s+`` 切的，重新拼接会丢掉词间空格；
    用替换则天然保住原有空白与标点。
    """
    bad = [c.get("sentence") for c in (verification.get("claims") or [])
           if not c.get("supported") and c.get("sentence")]
    if not bad or not brief_text:
        return brief_text, []
    text = brief_text
    removed = []
    for s in bad:
        if s and s in text:
            text = text.replace(s, "")
            removed.append(s)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text, removed


def generate_brief(
    conn: sqlite3.Connection,
    event_id: str,
    backend: Optional[llm.BaseBackend] = None,
    cfg: Optional[Dict[str, Any]] = None,
    allow_fallback: bool = True,
) -> Dict[str, Any]:
    """对单个事件生成结构化简报（含事实校验）。"""
    cfg = cfg or llm.load_llm_config()
    material, sources = build_material(
        conn, event_id,
        max_total_chars=int(cfg["task"].get("max_source_chars", 6000)),
        backend=backend,
        cfg=cfg,
    )
    if not sources:
        return {"error": "事件不存在或没有可用材料", "event_id": event_id}

    # 目标字数随素材厚度自适应（详见 adaptive_brief_chars 的说明）。
    # 必须在这里覆盖一次，因为 prompt / 扩写 / 校验 都从 cfg["task"] 读它。
    _task = dict(cfg["task"])
    _base = int(_task.get("brief_chars", 700))
    _facts = fact_chars(material)
    _task["brief_chars"] = adaptive_brief_chars(material, _base)
    cfg = dict(cfg, task=_task)

    orgs = {s["source_org"] for s in sources}
    result: Dict[str, Any] = {
        "event_id": event_id,
        "n_articles": len(sources),
        "n_sources": len({(s["source_name"], s["source_org"]) for s in sources}),
        "n_orgs": len(orgs),
        "brief_chars_target": _task["brief_chars"],
        "brief_chars_target_base": _base,
        "material_facts": _facts,          # 素材里的事实字数（决定目标字数的那个量）
        "material_chars": len(material),   # 材料总字数（含 [N] 来源：… 等样板）
        "sources": [
            {k: v for k, v in s.items() if k != "text"} for s in sources
        ],
    }

    prompt = build_prompt(material, len(sources), len(orgs), cfg) + TRACEABILITY_HINT
    # 先把材料里的署名观点句检索出来，单独成块 —— 这是「借鉴多家媒体与专家
    # 观点」的**检索**那一步：不搜出来，小模型只会写事实。
    views = extract_viewpoints(sources)
    if views:
        prompt += ("\n\n---\n\n材料里出现的**署名观点句**（可引用，但务必写明 "
                   "是谁说的；不要编造材料之外的机构与专家）：\n"
                   + "\n".join(views))
    result["viewpoints_found"] = len(views)
    result["prompt_chars"] = len(prompt)

    active: Optional[llm.BaseBackend] = None
    try:
        active = backend or llm.get_backend(cfg)
        gen = active.generate(prompt, system=SYSTEM_PROMPT,
                              max_new_tokens=max_tokens_for(_task["brief_chars"]))
        result["backend"] = gen.backend
        result["model"] = gen.model
        result["elapsed_ms"] = gen.elapsed_ms
        result["generation_meta"] = gen.extra
        parsed = extract_json(gen.text)
        if parsed is None:
            raise llm.BackendError("模型输出无法解析为 JSON")
        result.update({
            # 回显兜底：清掉说明文字后若为空，退回用简报首句当标题
            "headline": (_clean_field(_as_text(parsed.get("headline")), HEADLINE_SOFT_MAX)
                         or _clip(_as_text(parsed.get("brief")).strip().split("。")[0],
                                  HEADLINE_SOFT_MAX)),
            "value_point": _clean_field(_as_text(parsed.get("value_point")),
                                        VALUE_POINT_SOFT_MAX),
            "brief": _as_text(parsed.get("brief")),
            "claims": _normalize_claims(parsed.get("claims")),
            "conflicts": [_as_text(c) for c in (parsed.get("conflicts") or [])],
            "_salvaged": bool(parsed.get("_salvaged")),
            "_raw_model_output": gen.text[:4000],
        })
        # 模型很容易「换词重说」把长度凑上来，先削一遍重复句再交给校验。
        # 这也是「字数按事实量定」的另一半：写多了就收回去，而不是靠调上限。
        deduped, dropped = dedup_sentences(str(result.get("brief") or ""))
        if dropped:
            result["brief"] = deduped
            result["_dropped_duplicates"] = dropped
    except Exception as exc:
        if not allow_fallback:
            raise
        fallback = _fallback_extractive(sources, f"{type(exc).__name__}: {exc}",
                                      target_chars=_task["brief_chars"])
        result.update(fallback)
        result["backend"] = "extractive"
        result["model"] = "(none)"
        result["elapsed_ms"] = 0

    # 一次生成明显偏短时，做一轮定向扩写。
    # **默认关闭**：用户明确要求「根据原文篇幅写简报，不要硬凑字数」，
    # 而「把短的写长」与这个诉求直接冲突 —— 小模型在事实已经写完时
    # 只能靠重复同一个事实来交差，正是之前的重复来源。
    # 需要时在 config/llm.yaml 里设 task.allow_expand: true 打开。
    min_chars = int(int(cfg["task"].get("brief_chars", 300)) * 0.85)
    if (bool(cfg["task"].get("allow_expand", False))
            and active is not None and not result.get("_fallback")
            and 0 < len(str(result.get("brief") or "")) < min_chars):
        result["brief_chars_before_expand"] = len(str(result.get("brief") or ""))
        try:
            expanded = expand_brief(active, material, result, cfg)
        except Exception:
            expanded = None
        if expanded:
            result.update(expanded)
            result["_expanded"] = True

    # 事实一致性校验
    from . import verify                                  # 局部导入避免循环
    result["verification"] = verify.verify_claims(result, sources, cfg=cfg)

    # ---- 通过之后：剔除"未被原文支撑"的句子 ----
    #
    # 通过与否是**比例**判据（支撑率 ≥ 80%），但发布出去的是**全文**。
    # 实测（2026-10-20，浏览器）：事件「习近平：以更高标准…」支撑率 4/5 = 80%
    # 恰好通过，正文里却留着一句覆盖率仅 0.643 的
    # 「中国共产党在'十四五'时期…」—— 原文是**"十五五"**，属于事实性错误，
    # 只因为占 1/5 就照样发布了。对「每个事实句都可回溯」的承诺来说这是硬伤。
    #
    # 比例闸门管"整体是否可信"，剔除管"这句话能不能发"，两者互补。
    # 剔除只能是改进：若剔除后反而不通过（比如剔除后字数低于下限），
    # 就保持原样，交给下面的降级逻辑处理。
    ver = result["verification"]
    if ver.get("accepted") and not result.get("_fallback"):
        new_text, removed = _drop_unsupported(str(result.get("brief") or ""), ver)
        if removed and new_text != str(result.get("brief") or ""):
            trial = dict(result)
            trial["brief"] = new_text
            trial["claims"] = []          # 旧声明对应旧正文，留着只会制造噪声
            trial_ver = verify.verify_claims(trial, sources, cfg=cfg)
            if trial_ver.get("accepted"):
                result = trial
                result["_dropped_unsupported"] = removed
                result["verification_before_drop"] = ver
                ver = trial_ver
                result["verification"] = trial_ver

    # 校验不过 -> 自动降级为抽取式
    if allow_fallback and not ver.get("accepted", True) and not result.get("_fallback"):
        fallback = _fallback_extractive(
            sources, "事实一致性校验未通过，已自动降级为抽取式摘要",
            target_chars=_task["brief_chars"],
        )
        if fallback:
            # 保留降级前的校验报告，便于定位失败原因
            result["verification_before_fallback"] = ver
            result.update(fallback)
            result["backend"] = "extractive"
            result["model"] = "(none)"
            result["verification"] = verify.verify_claims(result, sources, cfg=cfg)

    return result
