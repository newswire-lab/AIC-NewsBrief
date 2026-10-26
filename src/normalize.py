"""跨语言归一化：把非中文材料转成中文事实要点。

**为什么必须要有这一层**

事实一致性校验用的是"加权关键词覆盖率"，本质是**词面比对**。
如果源材料是英文、而生成的简报是中文，中文 n-gram 永远不可能命中英文原文，
覆盖率恒为 0 —— 于是**完全正确的翻译型简报会被误判为"无依据"**。

实测就真实发生了这件事。模型把原文

    It marks a significant step for the Army's effort to add mobile maritime-strike
    capability to its long-range fires portfolio...

正确地译成

    这是对美国陆军努力增加远程精确火力能力的重大步骤。

却被校验器判为"在原文中找不到足够支撑"，占 0/2，触发自动降级。

**解法**：在生成与校验**之前**，先把非中文材料归一化为中文事实要点，
并把结果落库缓存（`articles.summary_zh`）。此后：

- 生成层拿到的是中文材料（也顺带解掉了"跨语言生成"的负担）；
- 校验层比对的是同一份中文材料，覆盖率才有意义。

这样"校验"才真的是在与材料比对，而不是在与译文比对。
归一化本身也是本作品"AI 与开源资源融合"的一环：
由开源模型承担理解与转写，由自研校验器承担核对。
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import sqlite3
import warnings

from . import llm
from .models import utcnow, iso

SYSTEM_PROMPT = (
    "你是一名严谨的新闻编译。请把用户提供的非中文新闻材料转写成中文事实要点，"
    "只保留客观事实，不添加评论、不做推测、不补充材料之外的信息。"
)

_TEMPLATE = """请把下面这段{lang_name}新闻材料转写为**中文事实要点**。

要求：
1. 输出 3~6 句简体中文，每句陈述一个客观事实（谁、做了什么、时间、地点、数量、型号等）；
2. **必须保留原文中的关键专名与数字**，例如机构名、装备型号、金额、数量、日期；
   专名可保留原文写法（如 Lockheed Martin、PrSM、HIMARS），不要把型号译得失真；
3. 不要写任何评论、分析、预测或背景补充；
4. 不要输出标题、不要编号、不要 Markdown 标记，直接输出句子。

原文材料：

{text}
"""

_LANG_NAMES = {"en": "英文", "ja": "日文", "ko": "韩文", "ru": "俄文", "fr": "法文",
               "de": "德文", "es": "西班牙文"}

MAX_INPUT_CHARS = 4000

# 标题翻译与「事实要点转写」是两件事：标题要素齐全、长度受限、读起来要像标题，
# 不能沿用 _TEMPLATE（那个模板会输出 3~6 句话，拿来当标题完全不可用）。
MAX_TITLE_CHARS = 300     # 送进模型的标题截断长度
MAX_TITLE_LEN = 60        # 存入 title_zh 的最大长度
QUOTE_CHARS = "\"'“”‘’「」"
TITLE_SYSTEM_PROMPT = (
    "你是一名新闻标题译者。把用户给的外文新闻标题译成简洁准确的简体中文标题，"
    "只输出译文本身。"
)


def needs_normalize(lang: str, text: str) -> bool:
    """判断是否需要归一化：非中文源，或中文占比过低。"""
    if lang and lang != "zh":
        return True
    if not text:
        return False
    chinese = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return chinese / max(1, len(text)) < 0.2


def normalize_text(text: str, lang: str = "en",
                   backend: Optional[llm.BaseBackend] = None,
                   cfg: Optional[Dict[str, Any]] = None) -> str:
    """调用开源模型把一段非中文文本转成中文事实要点。"""
    text = (text or "").strip()
    if not text:
        return ""
    active = backend or llm.get_backend(cfg)
    prompt = _TEMPLATE.format(lang_name=_LANG_NAMES.get(lang, lang or "非中文"),
                              text=text[:MAX_INPUT_CHARS])
    gen = active.generate(prompt, system=SYSTEM_PROMPT, max_new_tokens=600,
                          temperature=0.2)
    return gen.text.strip()


def clean_title(raw: str) -> str:
    """清理模型输出的标题：去引号、去前缀、只取首行、限长。

    实测坑：模型偶尔会把「标题：」这类前缀或多行内容一起吐出来，
    直接入库会让界面看起来像坏了，所以统一在这里收口。
    """
    t = (raw or "").strip()
    if not t:
        return ""
    t = t.splitlines()[0].strip()
    for prefix in ("标题：", "标题:", "译文：", "译文:", "中文标题：", "中文标题:"):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    t = t.strip(QUOTE_CHARS).strip()
    if t.endswith("。"):
        t = t[:-1]
    return t[:MAX_TITLE_LEN]


def translate_title(text: str, lang: str = "en",
                    backend: Optional[llm.BaseBackend] = None,
                    cfg: Optional[Dict[str, Any]] = None) -> str:
    """把一条外文新闻标题译成中文短标题。已是中文（或翻译失败）返回空串。

    返回空串而不是原文，是为了让调用方用 ``title_zh or title`` 就能优雅降级 ——
    界面**绝不能**因为一条标题没译出来而报错或空白。
    """
    text = (text or "").strip()
    if not text or not needs_normalize(lang, text):
        return ""
    active = backend or llm.get_backend(cfg)
    prompt = (
        f"把下面的{_LANG_NAMES.get(lang, lang or '外文')}新闻标题译成中文。\n"
        "要求：\n"
        "1. 只输出译文，不要引号、不要解释、不要附原文；\n"
        "2. 不超过 40 个字，读起来像一个中文新闻标题；\n"
        "3. 机构名、公司名、装备型号用通用中文译名，没有通用译名的保留原文写法"
        "（如 Lockheed Martin、PrSM、HIMARS）；\n"
        "4. 数字、日期、金额原样保留，不要改写或四舍五入。\n\n"
        f"标题：{text[:MAX_TITLE_CHARS]}"
    )
    try:
        gen = active.generate(prompt, system=TITLE_SYSTEM_PROMPT,
                              max_new_tokens=80, temperature=0.1)
    except Exception:
        return ""
    return clean_title(gen.text)


def ensure_article_chinese(
    conn: sqlite3.Connection,
    row: Any,
    backend: Optional[llm.BaseBackend] = None,
    cfg: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> str:
    """确保某篇文章有中文事实要点，返回它。已缓存则直接返回。

    row 需要包含 id / lang / title / content / summary / summary_zh 字段。
    """
    cached = (row["summary_zh"] or "").strip() if "summary_zh" in row.keys() else ""
    if cached:
        return cached

    source_text = (row["content"] or "").strip() or (row["summary"] or "").strip()
    body = f"标题：{row['title']}\n\n{source_text}"
    if not needs_normalize(row["lang"], body):
        return body

    try:
        zh = normalize_text(body, lang=row["lang"], backend=backend, cfg=cfg)
    except Exception as exc:
        # 归一化失败时返回**空串**，而不是把错误信息拼进正文。
        #
        # **曾有的 bug**：原实现返回 f"[归一化失败: ...]\n{body}"，
        # 调用方只看「非空」，于是这段错误串会被当成「中文事实要点」
        # ① 作为简报素材喂给生成模型（制造噪声，甚至被抄进简报）；
        # ② 作为校验时比对的原文（把覆盖率算低，可能误触发降级）。
        # 返回空串后，调用方会走「外文材料未归一化」的正常提示分支。
        warnings.warn(
            f"归一化失败 id={row['id']}: {type(exc).__name__}: {exc}",
            RuntimeWarning, stacklevel=2,
        )
        return ""

    if zh:
        conn.execute(
            "UPDATE articles SET summary_zh = ? WHERE id = ?", (zh, row["id"])
        )
        if commit:
            conn.commit()
    return zh or body


def normalize_event(
    conn: sqlite3.Connection,
    event_id: str,
    backend: Optional[llm.BaseBackend] = None,
    cfg: Optional[Dict[str, Any]] = None,
    verbose: bool = True,
) -> int:
    """把某个事件簇下所有非中文文章归一化，返回处理条数。"""
    rows = conn.execute(
        "SELECT id, lang, title, content, summary, summary_zh FROM articles "
        "WHERE event_id = ? ORDER BY source_tier ASC",
        (event_id,),
    ).fetchall()
    done = 0
    for row in rows:
        if (row["summary_zh"] or "").strip():
            continue
        body = (row["content"] or "").strip() or (row["summary"] or "").strip()
        if not needs_normalize(row["lang"], f"{row['title']}\n{body}"):
            continue
        ensure_article_chinese(conn, row, backend=backend, cfg=cfg)
        done += 1
        if verbose:
            print(f"    已归一化: {row['title'][:40]}")
    return done
