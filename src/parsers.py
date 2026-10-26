"""RSS 2.0 / Atom 1.0 / RDF(RSS 1.0) 解析器。

纯标准库实现（不依赖 feedparser），原因有二：
1. 目标环境不可控，少一个依赖少一个坑；
2. 解析与字段归一化是本作品的算法环节之一，自己掌控更便于扩展
   （后续要在这一层做栏目识别、正文抽取、来源权重标注）。

容错策略：严格 XML 解析失败时，退化为正则分块解析，尽量把条目捞出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional, Sequence, Tuple
import html
import re
import xml.etree.ElementTree as ET

DEFAULT_TZ_OFFSET = 8  # 无时区信息时，按东八区解释（国内源占多数）

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_P_RE = re.compile(r"</p\s*>", re.I)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


@dataclass
class RawItem:
    """解析器输出的原始条目，尚未做去重与归一化。"""

    title: str = ""
    link: str = ""
    summary: str = ""
    content: str = ""
    published: Optional[datetime] = None
    guid: str = ""
    author: str = ""


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------

def decode_bytes(data: bytes, hint: Optional[str] = None) -> str:
    """按 BOM -> XML 声明 -> meta charset -> 猜测 的顺序解码。"""
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")

    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([\w\-]+)[\"']", data[:300], re.I)
    candidates: List[str] = []
    if m:
        candidates.append(m.group(1).decode("ascii", "replace"))
    m = re.search(rb"charset=[\"']?([\w\-]+)", data[:3000], re.I)
    if m:
        candidates.append(m.group(1).decode("ascii", "replace"))
    if hint:
        candidates.append(hint)
    candidates += ["utf-8", "gb18030"]

    seen = set()
    for enc in candidates:
        key = enc.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def strip_html(text: str) -> str:
    """去标签、反转义实体、压空白，保留段落换行。"""
    if not text:
        return ""
    out = _CDATA_RE.sub(r"\1", text)
    out = _SCRIPT_RE.sub(" ", out)
    out = _BR_RE.sub("\n", out)
    out = _P_RE.sub("\n", out)
    out = _TAG_RE.sub(" ", out)
    out = html.unescape(out).replace("\u3000", " ")
    lines = [_WS_RE.sub(" ", ln).strip() for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def parse_datetime(value: str, tz_offset: int = DEFAULT_TZ_OFFSET) -> Optional[datetime]:
    """兼容 RFC822（RSS）与 ISO8601（Atom）以及若干宽松格式，统一返回 UTC。"""
    if not value:
        return None
    s = _WS_RE.sub(" ", str(value).strip())
    if not s:
        return None

    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return _to_utc(dt, tz_offset)
    except (TypeError, ValueError):
        pass

    try:
        return _to_utc(datetime.fromisoformat(s.replace("Z", "+00:00")), tz_offset)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
        "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M", "%Y年%m月%d日",
        "%m-%d %H:%M", "%m/%d %H:%M",
    ):
        try:
            return _to_utc(datetime.strptime(s, fmt), tz_offset)
        except ValueError:
            continue

    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        try:
            return _to_utc(datetime(y, mo, d, hh, mm), tz_offset)
        except ValueError:
            return None
    return None


def _to_utc(dt: datetime, tz_offset: int = DEFAULT_TZ_OFFSET) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=tz_offset)))
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# XML 工具
# --------------------------------------------------------------------------

def localname(tag: str) -> str:
    """剥掉 XML 命名空间，返回本地名（小写）。"""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.lower()


def _children(node: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in list(node) if localname(c.tag) == name]


def _first_child(node: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    if node is None:
        return None
    for child in list(node):
        if localname(child.tag) in names:
            return child
    return None


def _text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    raw = "".join(node.itertext())
    if not raw and node.text:
        raw = node.text
    return raw.strip()


def _link_of(node: ET.Element) -> str:
    """RSS 用 <link>文本</link>；Atom 用 <link rel="alternate" href="..."/>。"""
    for child in list(node):
        if localname(child.tag) != "link":
            continue
        href = child.get("href")
        rel = (child.get("rel") or "alternate").lower()
        if href and rel in ("alternate", ""):
            return href.strip()
        if child.text and child.text.strip().startswith("http"):
            return child.text.strip()
    for child in list(node):
        if localname(child.tag) == "link" and child.get("href"):
            return child.get("href", "").strip()
    return ""


# --------------------------------------------------------------------------
# 主解析入口
# --------------------------------------------------------------------------

def parse_feed(data: bytes, hint_encoding: Optional[str] = None) -> Tuple[List[RawItem], str]:
    """解析 feed 内容，返回 (条目列表, 频道标题)。"""
    text = decode_bytes(data, hint_encoding)
    if not text.strip():
        return [], ""

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        items = _fallback_regex_parse(text)
        return items, ""

    root_name = localname(root.tag)
    feed_title = ""

    if root_name == "rss":
        channel = _first_child(root, "channel") or root
        feed_title = _text(_first_child(channel, "title"))
        nodes = _children(channel, "item")
    elif root_name == "feed":  # Atom
        feed_title = _text(_first_child(root, "title"))
        nodes = _children(root, "entry")
    elif root_name == "rdf":   # RSS 1.0 / RDF
        channel = _first_child(root, "channel")
        feed_title = _text(_first_child(channel, "title")) if channel is not None else ""
        nodes = _children(root, "item")
    else:
        nodes = _children(root, "item") or _children(root, "entry")

    items: List[RawItem] = []
    for node in nodes:
        title = strip_html(_text(_first_child(node, "title")))
        link = _link_of(node)
        raw_summary = _text(_first_child(node, "description", "summary"))
        if not raw_summary:
            raw_summary = _text(_first_child(node, "encoded"))
        raw_content = _text(_first_child(node, "encoded", "content"))
        if not raw_content:
            raw_content = raw_summary
        published = parse_datetime(_text(
            _first_child(node, "pubdate", "date", "published", "updated", "created")
        ))
        items.append(RawItem(
            title=title,
            link=link,
            summary=strip_html(raw_summary),
            content=strip_html(raw_content),
            published=published,
            guid=_text(_first_child(node, "guid", "id")),
            author=_text(_first_child(node, "author", "creator")),
        ))

    return items, feed_title


def _tag_text(block: str, *names: str) -> str:
    for name in names:
        m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
        if m:
            return _CDATA_RE.sub(r"\1", m.group(1)).strip()
    return ""


def _tag_attr(block: str, name: str, attr: str) -> str:
    m = re.search(rf"<{name}[^>]*\b{attr}=[\"']([^\"']+)[\"']", block, re.I)
    return m.group(1).strip() if m else ""


def _fallback_regex_parse(text: str) -> List[RawItem]:
    blocks = re.findall(r"<item[\s>].*?</item>", text, re.S | re.I)
    if not blocks:
        blocks = re.findall(r"<entry[\s>].*?</entry>", text, re.S | re.I)
    items: List[RawItem] = []
    for block in blocks:
        title = strip_html(_tag_text(block, "title"))
        link = _tag_text(block, "link") or _tag_attr(block, "link", "href")
        summary = strip_html(_tag_text(block, "description", "summary"))
        content = strip_html(_tag_text(block, "encoded", "content") or summary)
        published = parse_datetime(
            _tag_text(block, "pubDate", "published", "updated", "dc:date")
        )
        if not (title or link):
            continue
        items.append(RawItem(
            title=title, link=link, summary=summary, content=content,
            published=published,
            guid=_tag_text(block, "guid", "id"),
        ))
    return items
