"""事实一致性校验：把简报的每一句回溯到输入材料，核对是否有原文支撑。

**这是本作品对应赛题技术报告第七节「AI 输出、知识产权与内容安全」的核心机制。**
赛题要求说明"错误输出、偏见、有害内容、提示注入、模型幻觉或误用风险，
以及采取的审核、限制和提示措施"。本模块把这件事落成可执行、可量化的检查。

---

### 校验对象：简报的每一句，而不是模型的"声明"

本模块经历了一次重要修正，值得记录：

初版是**校验模型声明的 claims 句子**（要求模型为每条事实句标注材料编号）。
但实测发现小模型会**改写**它声明的句子 —— 简报里写

    这一合同标志着交付PrSM增量2给作战人员的一个重要步骤。

声明的 claim 却写成

    这标志着向作战人员交付PrSM增量2的重要一步。

于是词面覆盖率对不上，**简报本身明明有原文支撑，却被判为无依据**，触发错误降级。

修正后的设计：

1. **逐句校验简报正文的每一句**（这才是真正要给用户看的内容）；
2. 模型声明的 `claims` 只当作**来源提示**：
   若能对应到简报中的某句，就优先用声明的来源去核对；
3. 若按声明来源核对不通过，**再到全部材料里找最佳匹配来源** ——
   既不因为模型标错编号而误杀，也会把"声明不实"作为质量信号单独记录；
4. 模型完全没给 `claims` 时，直接逐句对全部材料求最佳匹配。

这样既保住了"句级可溯源"的产品承诺，也不会因模型的措辞小差异而误判。

### 其余三类检查

- **推测性表述检测**：命中"或将/可能会/预计/分析认为/意味着"等词即标记
  （这是本作品明令禁止的输出形态）；
- **长度合规**；
- **声明准确度**：模型声明的来源是否确实支撑该句（单独统计，不计入通过与否）。

校验不通过时，:mod:`src.brief` 会自动降级为**抽取式摘要** ——
"宁可给出不好看但完全忠实原文的简报，也不输出可能失真的生成内容"。

方法完全自研、不引入外部模型：覆盖率用字符 n-gram 与拉丁词元，
按 token 长度加权（长 n-gram 信息量更大），避免短词反复命中造成虚高。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple
import re

from . import llm
from .similarity import tokenize

# 明令禁止的**无主**推测性表述（模型自己的臆测）。
#
# 注意：「预计」「分析认为」曾在这张表里，但它们也是**有署名引用**的常见措辞
# （「据XX专家分析认为」「俄国防部预计」）。产品要求简报可以借鉴多家媒体与
# 专家的观点并标明来源，若继续一刀切禁用，**正确的引用句会被判为不合格并
# 触发降级** —— 这是实测出来的冲突，所以把它们移出本表，改由提示词约束
# 「必须署名、材料里没有的观点一律不许写」。
SPECULATIVE_WORDS = (
    "或将", "可能会", "有望", "恐将", "势必", "无疑", "显然", "据推测",
    "大概会", "should", "likely", "may ", "might ",
)
# 引用类措辞：出现这些词时句子**应当**带署名（据…／援引…／XX表示）。
# 本版只用提示词约束，不做机械判定 —— 机械判定会把「专家表示」也误杀。
ATTRIBUTION_WORDS = ("分析认为", "预计", "援引", "引述", "表示", "指出", "称")
# 覆盖率计算时要忽略的**框式词**：它们不是事实，不该拉低可溯源比例。
# 「据新华报道，X发生于Y」里只有 X/Y 是可溯源的事实，「据/报道」是行文架子。
FRAMING_TOKENS = frozenset({
    "据", "援引", "引述", "报道", "消息", "人士", "表示", "指出", "称",
    "认为", "强调", "警告", "透露", "分析", "评论", "观点", "专家", "媒体",
})
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*|(?<=\.)\s+")
_WS_RE = re.compile(r"\s+")

DEFAULT_MIN_COVERAGE = 0.75          # 单句加权覆盖率下限
DEFAULT_MIN_SUPPORTED_RATIO = 0.80   # 达标句子占比下限
# 简报长度的相对容差：默认 ±25%（300 字 → 接受 225~375 字）。
# 早期版本固定用 ±60%（接受 120~480 字），宽到形同虚设：实测 392 字的简报
# 也被判「长度合规」，与产品「300 字简报」的承诺相矛盾，也与 expand_brief()
# 使用的目标区间（±15%）不在同一量级。
DEFAULT_LENGTH_TOLERANCE = 0.25
# —— 长度判据：只拦「退化」与「失控」，不追求"贴着目标字数" ——
#
# 2026-10-20 改。原设计是 [目标×0.60, 目标×1.25]，实测行不通：
# 同一批材料下本地 3B 的简报长度在 **92~350 字**之间波动（8 次生成），
# 而区间只有 1.25/0.60 = **2.08 倍**宽 —— 模型方差大于判据容差，
# 于是无论目标定多少都有一半落在外面，**8 次生成 0 次通过**。
# 长度本来只是"表达充分度"的粗代理，真正衡量可信度的是
# 支撑率 / 未支撑句 / 推测词那三条（它们仍是硬判据，一个都不放松）。
#
# 现在：低于硬下限判"写得太空"（这正是用户最初报障的"只有一句话"），
#       高于硬上限判"失控"（防小模型跑飞）；中间都算通过。
DEFAULT_MIN_RATIO = 0.30        # 硬下限 = max(LENGTH_HARD_FLOOR, 目标×此比例)
DEFAULT_MAX_RATIO = 1.60        # 硬上限 = 目标×此比例
LENGTH_HARD_FLOOR = 120         # 绝对下限：比这还短，实质就是"一两句话"

CLAIM_MATCH_MIN_JACCARD = 0.45       # claim 句子与简报原句视为同一句的最低重合度


def weighted_coverage(sentence: str, material: str) -> float:
    """加权关键词覆盖率：句子的关键词有多少比例真的出现在材料里。

    权重取 token 长度（长 n-gram 区分度高、信息量大），
    避免"中国""表示"这类短高频词反复命中把分数抬高。
    """
    if not sentence or not material:
        return 0.0
    tokens = Counter(tokenize(sentence))
    # 去掉框式词：引用句里的「据/表示/专家」不是事实，不该计入分母
    # （否则「据XX表示，<事实>」会因为多了几个架子词而覆盖率不足被误杀）
    tokens = Counter({t: c for t, c in tokens.items()
                      if t.replace(" ", "") not in FRAMING_TOKENS})
    if not tokens:
        return 0.0
    haystack = _WS_RE.sub("", material).lower()
    total = 0.0
    hit = 0.0
    for token, count in tokens.items():
        weight = len(token) * count
        total += weight
        if token.replace(" ", "") in haystack:
            hit += weight
    return hit / total if total else 0.0


def split_sentences(text: str) -> List[str]:
    """中英文句子切分：中文句末标点，以及英文的 ". "（点号后跟空白）。"""
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text)]
    return [p for p in parts if len(p) >= 4]


def _source_haystack(source: Dict[str, Any]) -> str:
    return "\n".join([
        str(source.get("title") or ""),
        str(source.get("text") or ""),
    ])


def best_source_for(sentence: str, sources: Sequence[Dict[str, Any]]
                    ) -> Tuple[Optional[Dict[str, Any]], float]:
    """在全部材料中找出对该句支撑最强的一个。返回 (source, coverage)。"""
    best_source = None
    best_cov = 0.0
    for src in sources:
        cov = weighted_coverage(sentence, _source_haystack(src))
        if cov > best_cov:
            best_source, best_cov = src, cov
    return best_source, best_cov


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_claim_to_sentence(claim_sentence: str, sentences: Sequence[str]) -> Optional[str]:
    """把模型声明的句子对应回简报中的原句（模型常改写标点或删字）。"""
    best: Optional[str] = None
    best_score = 0.0
    for s in sentences:
        score = _jaccard(claim_sentence, s)
        if score > best_score:
            best, best_score = s, score
    return best if best_score >= CLAIM_MATCH_MIN_JACCARD else None


def verify_claims(
    result: Dict[str, Any],
    sources: Sequence[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """校验生成结果。返回结构化报告，供界面展示与写入技术报告。"""
    task = (cfg or llm.load_llm_config()).get("task", {})
    min_coverage = float(task.get("verify_min_coverage", DEFAULT_MIN_COVERAGE))
    min_supported_ratio = float(
        task.get("verify_min_supported_ratio", DEFAULT_MIN_SUPPORTED_RATIO)
    )
    brief_chars = int(task.get("brief_chars", 300))

    by_index = {int(s["index"]): s for s in sources}
    claims = result.get("claims") or []
    brief = str(result.get("brief") or "")

    # ---- 把模型声明的 claims 映射到简报原句，作为"来源提示" ----
    sentences = split_sentences(brief)
    declared: Dict[str, Dict[str, Any]] = {}
    unmatched_claims: List[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        sentence = str(claim.get("sentence") or "").strip()
        if not sentence:
            continue
        try:
            idx = int(claim.get("source_index"))
        except (TypeError, ValueError):
            idx = -1
        target = sentence if sentence in sentences else match_claim_to_sentence(sentence, sentences)
        if target:
            declared.setdefault(target, {"source_index": idx, "claim": sentence})
        else:
            unmatched_claims.append(sentence)

    # ---- 逐句校验简报正文 ----
    checked: List[Dict[str, Any]] = []
    supported = 0
    declared_ok = 0
    declared_total = 0
    for sentence in sentences:
        hint = declared.get(sentence)
        declared_total += 1 if hint else 0

        source_used: Optional[Dict[str, Any]] = None
        coverage = 0.0
        declaration_correct = None
        reason = ""

        if hint and hint["source_index"] in by_index:
            declared_src = by_index[hint["source_index"]]
            coverage = weighted_coverage(sentence, _source_haystack(declared_src))
            source_used = declared_src
            declaration_correct = coverage >= min_coverage
            if not declaration_correct:
                # 声明来源不支撑时，再到全部材料里找最佳匹配，避免误杀
                alt_src, alt_cov = best_source_for(sentence, sources)
                if alt_cov > coverage:
                    source_used, coverage = alt_src, alt_cov
                    reason = "声明的来源不支撑，但其他材料可以支撑"
        else:
            source_used, coverage = best_source_for(sentence, sources)
            reason = "模型未声明依据，由校验器推断"

        ok = coverage >= min_coverage
        supported += 1 if ok else 0
        if declaration_correct:
            declared_ok += 1
        checked.append({
            "sentence": sentence,
            "source_index": (source_used or {}).get("index"),
            "source_name": (source_used or {}).get("source_name"),
            "coverage": round(coverage, 4),
            "supported": ok,
            "declared": bool(hint),
            "declaration_correct": declaration_correct,
            "reason": reason if not ok else "",
        })

    # 「未被原文支撑的句子」+「模型声明了但对不上正文的声明」。
    #
    # **曾有的 bug**：原实现是
    #     [s for s in split_sentences(brief)
    #      if not any(s in c["sentence"] or c["sentence"] in s for c in checked)]
    # 而 `checked` 恰恰是逐句由 split_sentences(brief) 构造出来的，
    # 所以对任意一句都能找到 c["sentence"] == s → 条件恒为真 → 恒为空列表。
    # 界面把这个字段当作「未找到依据的句子」展示，于是这一路警示长期显示 0，
    # 掩盖了真实的覆盖率问题。现在改为汇报真正没通过覆盖率的句子。
    uncovered = [c["sentence"] for c in checked if not c["supported"]]
    uncovered += unmatched_claims

    speculative = sorted({w for w in SPECULATIVE_WORDS if w in brief})

    n_claims = len(checked)
    ratio = (supported / n_claims) if n_claims else 0.0
    # 长度判据用**非对称区间**，而不是「与目标字数相差不超过 ±tol」。
    #
    # **为什么**（2026-10-15 实测）：目标字数由 :func:`brief.adaptive_brief_chars`
    # 以 ``BRIEF_RATIO(0.34) × 材料事实字数`` 估算，这是个**启发式**，会高估。
    # 实测事件「习近平：以更高标准、更实举措推进全面从严治党」：
    #   材料事实 1512 字 → 目标 514 字；本地 3B 自然写出 **322 字**，
    #   6 句覆盖率 0.94~1.00、支撑率 100%、未支撑句 0 —— 一份**高质量**简报，
    #   却因 ±25% 对称容差的下限是 386 字而被判 length_ok=False，
    #   整条降级成抽取式，界面上看起来像"生成失败"。
    # 对称容差的隐含假设是"目标算得准"，而这个假设不成立。
    #
    # 用户的要求是「材料多就写长，材料少就精炼」——
    # **写短不是错误，空洞才是**。真正要拦的是"写不到下限"与"写过头"，
    # 所以下限取 ``brief_min_ratio`` × 目标，上限仍是 ``1 + tol`` × 目标。
    length_tol = float(task.get("brief_chars_tolerance", DEFAULT_LENGTH_TOLERANCE))
    length_min_ratio = float(task.get("brief_min_ratio", DEFAULT_MIN_RATIO))
    length_max_ratio = float(task.get("brief_max_ratio", DEFAULT_MAX_RATIO))
    length_lower = max(LENGTH_HARD_FLOOR, int(brief_chars * length_min_ratio))
    length_upper = int(brief_chars * length_max_ratio)
    length_ok = length_lower <= len(brief) <= length_upper
    # 软区间只用来给"离目标有差距"的提示，不参与通过与否
    soft_lower = int(brief_chars * (1.0 - length_tol))
    soft_upper = int(brief_chars * (1.0 + length_tol))
    declaration_ratio = (declared_ok / declared_total) if declared_total else 0.0

    accepted = (
        n_claims > 0
        and ratio >= min_supported_ratio
        and not speculative
        and length_ok
    )

    notes: List[str] = []
    if n_claims == 0:
        notes.append("未能从简报中切出任何可校验的句子")
    if ratio < min_supported_ratio:
        notes.append(
            f"有原文支撑的句子占比 {ratio:.0%}，低于阈值 {min_supported_ratio:.0%}"
        )
    if speculative:
        notes.append("出现被禁止的推测性表述: " + "、".join(speculative))
    if not length_ok:
        if len(brief) < length_lower:
            notes.append(
                f"简报只有 {len(brief)} 字，低于下限 {length_lower} 字"
                f"（目标 {brief_chars} 字）—— 内容明显偏空"
            )
        else:
            notes.append(
                f"简报 {len(brief)} 字，超过上限 {length_upper} 字"
                f"（目标 {brief_chars} 字）—— 疑似跑飞或凑字数"
            )
    elif not (soft_lower <= len(brief) <= soft_upper):
        # 落在硬区间内、但离目标较远：只提示，不判失败
        notes.append(
            f"简报 {len(brief)} 字与目标 {brief_chars} 字有差距（仅提示，不判失败）"
        )
    if declared_total and declaration_ratio < 1.0:
        notes.append(
            f"模型声明的来源中 {declared_total - declared_ok}/{declared_total} 条"
            f"不直接支撑该句（已由校验器改判，仅作质量提示）"
        )
    if unmatched_claims:
        notes.append(f"有 {len(unmatched_claims)} 条声明与简报正文对不上")

    return {
        "accepted": accepted,
        "claims": checked,
        "inferred": declared_total == 0,
        "n_claims": n_claims,
        "n_supported": supported,
        "supported_ratio": round(ratio, 4),
        "min_coverage": min_coverage,
        "min_supported_ratio": min_supported_ratio,
        "declared_total": declared_total,
        "declared_ok": declared_ok,
        "declaration_ratio": round(declaration_ratio, 4),
        "uncovered_sentences": uncovered,
        "speculative_hits": speculative,
        "brief_chars": len(brief),
        "length_ok": length_ok,
        "length_lower": length_lower,
        "length_upper": length_upper,
        "notes": notes,
    }


def format_report(report: Dict[str, Any]) -> str:
    """把校验报告格式化成可读文本（供命令行与界面展示）。"""
    if not report:
        return "（无校验结果）"
    lines = []
    status = "通过" if report.get("accepted") else "未通过"
    lines.append(f"事实一致性校验：{status}")
    lines.append(
        f"  逐句溯源 {report.get('n_supported')}/{report.get('n_claims')} 句达标"
        f"（{report.get('supported_ratio', 0):.0%}，阈值 "
        f"{report.get('min_supported_ratio', 0):.0%}）"
    )
    lines.append(f"  简报长度 {report.get('brief_chars')} 字"
                 f"{'（合规）' if report.get('length_ok') else '（偏离目标）'}")
    if report.get("declared_total"):
        lines.append(
            f"  模型声明来源的准确率 {report.get('declaration_ratio', 0):.0%}"
            f"（{report.get('declared_ok')}/{report.get('declared_total')}）"
        )
    if report.get("inferred"):
        lines.append("  模型未提供 claims，依据由校验器逐句推断")
    if report.get("speculative_hits"):
        lines.append("  推测性表述: " + "、".join(report["speculative_hits"]))
    if report.get("uncovered_sentences"):
        lines.append(f"  未声明依据的句子 {len(report['uncovered_sentences'])} 句：")
        for s in report["uncovered_sentences"][:3]:
            lines.append(f"    - {s[:60]}")
    for note in report.get("notes") or []:
        lines.append(f"  说明: {note}")
    return "\n".join(lines)
