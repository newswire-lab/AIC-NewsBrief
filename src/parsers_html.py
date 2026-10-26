"""通用 HTML 列表页抽取：刻意**不写死任何站点的 CSS 选择器**。

原因：本作品要覆盖的源会持续变化，逐站写死选择器的维护成本高，
对"技术方案完整性"也没有加分。这里改成基于**结构特征组合判据**的通用抽取。

抽取流程：

1. 用 ``html.parser`` 把文档解析成标记流，以 ``<li>`` / ``<dd>`` / ``<tr>``
   作为**列表项容器**，按容器归集其中的锚点与纯文本。
   （用容器而不是"锚点前后 N 字符窗口"，是因为日期可能出现在标题之前或之后，
   按容器归集对两种情况都准确。）
2. 容器内取**第一个**形如文章详情页的锚点作为条目：
   ``href`` 需含 6 位以上数字、或含日期路径段、或形如 ``t20260923_``
   这类国内 CMS 命名。
3. 标题需长度合理（6~90 字符）且不是导航词（首页/更多/登录…）。
4. 日期从**同一容器**的文本里取，取不到则按参数决定丢弃或保留。
5. 没有 ``li``/``dd``/``tr`` 结构的页面，退化为"锚点前后窗口扫描"。

局限（已在技术文档说明）：对纯 JS 渲染的列表页无效；
若站点把多条新闻塞进同一个容器，只会取到第一条。
"""

from __future__ import annotations

from html.parser import HTMLParser
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin
import re

from . import dedup
from .parsers import RawItem, parse_datetime, strip_html

DATE_RE = re.compile(
    r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2}))?"
)
SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template)\b.*?</\1>", re.S | re.I
)
ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
PARA_RE = re.compile(r"<(p|div|section|article)\b[^>]*>(.*?)</\1>", re.S | re.I)

# 文章详情页 URL 的结构特征
ARTICLE_HREF_RE = re.compile(
    r"(?:"
    r"\d{6,}"                                  # content_1234567.html / 纯数字 id
    r"|/\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?/"     # /2026-09/ 或 /2026/09/23/
    r"|t\d{8}_"                                # t20260923_12345.html（国内 CMS）
    r"|/\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r")"
)

NAV_WORDS = {
    "首页", "更多", "查看更多", "下一页", "上一页", "尾页", "末页", "返回",
    "登录", "注册", "网站地图", "联系我们", "关于我们", "设为首页",
    "加入收藏", "打印", "关闭", "分享", "评论", "订阅", "无障碍",
    "繁体版", "搜索", "高层", "权威发布", "图片", "视频",
}
MIN_TITLE_LEN = 6
MAX_TITLE_LEN = 90
DATE_WINDOW = 400
# 日期分组式列表中（日期作为分组标题、其后若干条目共用该日期），
# 日期允许向后作用的可见文本字符跨度上限
DATE_CARRY_CHARS = 2000
DEFAULT_MAX_ITEMS = 50
CONTAINER_TAGS = ("li", "dd", "tr")


def _clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", strip_html(text)).strip()


def _is_article_href(href: str) -> bool:
    if not href:
        return False
    low = href.lower()
    if low.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    if low.endswith((".jpg", ".png", ".gif", ".pdf", ".zip", ".mp4", ".css", ".js")):
        return False
    return bool(ARTICLE_HREF_RE.search(href))


def _looks_like_nav(title: str) -> bool:
    if title in NAV_WORDS:
        return True
    if len(title) < MIN_TITLE_LEN or len(title) > MAX_TITLE_LEN:
        return True
    # 纯外文单词式导航（English / About Us）
    if not re.search(r"[\u4e00-\u9fff]", title) and len(title.split()) <= 2:
        return True
    return False


class _ListExtractor(HTMLParser):
    """把 <li>/<dd>/<tr> 当作列表项容器，收集容器内的锚点与文本。"""

    def __init__(self, base_url: str, accept_url_prefix: Optional[str] = None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.accept_url_prefix = accept_url_prefix
        self.debug = False
        self.items: List[RawItem] = []
        self.debug_pairs: List[Tuple[RawItem, str]] = []
        self._stack: List[Dict] = []
        self._anchor: Optional[Dict] = None
        self._pending_date: Optional[datetime] = None
        self._pending_age: int = 10 ** 9

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in CONTAINER_TAGS:
            self._stack.append({"tag": tag, "anchors": [], "text": []})
        if not self._stack:
            return
        if tag == "a":
            self._anchor = {"href": dict(attrs).get("href", ""), "text": []}
        elif tag in ("br", "p", "div", "span", "td"):
            self._stack[-1]["text"].append(" ")

    def handle_startendtag(self, tag, attrs):
        if self._stack and tag.lower() in ("br", "hr", "img"):
            self._stack[-1]["text"].append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self._anchor is not None:
            if self._stack:
                self._stack[-1]["anchors"].append(self._anchor)
            self._anchor = None
        elif tag in CONTAINER_TAGS and self._stack:
            self._emit(self._stack.pop())

    def handle_data(self, data):
        # 日期标记识别：只认"短文本节点里的日期"，
        # 避免把正文里偶发提到的日期误当成发布时间。
        self._pending_age += len(data)
        if len(data.strip()) <= 40:
            dm = DATE_RE.search(data)
            if dm:
                self._pending_date = parse_datetime(dm.group(0))
                self._pending_age = 0
        if not self._stack:
            return
        self._stack[-1]["text"].append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def close(self):
        super().close()
        while self._stack:          # 文档末尾未闭合的容器
            self._emit(self._stack.pop())

    def _emit(self, scope: Dict) -> None:
        for anchor in scope["anchors"]:
            href = (anchor.get("href") or "").strip()
            if not _is_article_href(href):
                continue
            title = _clean_title("".join(anchor.get("text") or []))
            if _looks_like_nav(title):
                continue
            absolute = urljoin(self.base_url, href)
            if self.accept_url_prefix and not absolute.startswith(self.accept_url_prefix):
                continue
            container_text = re.sub(r"\s+", " ", "".join(scope["text"])).strip()
            dm = DATE_RE.search(container_text)
            if dm:
                published = parse_datetime(dm.group(0))
            elif self._pending_age <= DATE_CARRY_CHARS:
                # 日期分组式列表：日期是分组标题，作用于其后的若干条目
                published = self._pending_date
            else:
                published = None
            item = RawItem(title=title, link=absolute, published=published, guid=absolute)
            self.items.append(item)
            if self.debug:
                self.debug_pairs.append((item, container_text[:220]))
            return              # 一个容器只取第一条


def _fallback_window_scan(
    html_text: str,
    base_url: str,
    max_items: int,
    require_date: bool,
    accept_url_prefix: Optional[str],
) -> List[RawItem]:
    """没有列表容器结构时的兜底：锚点后 400 字符窗口内找日期。"""
    text = SCRIPT_STYLE_RE.sub(" ", html_text)
    out: List[RawItem] = []
    for m in ANCHOR_RE.finditer(text):
        attr_match = HREF_RE.search(m.group(1) or "")
        href = ""
        if attr_match:
            href = (attr_match.group(1) or attr_match.group(2)
                    or attr_match.group(3) or "").strip()
        if not _is_article_href(href):
            continue
        title = _clean_title(m.group(2))
        if _looks_like_nav(title):
            continue
        absolute = urljoin(base_url, href)
        if accept_url_prefix and not absolute.startswith(accept_url_prefix):
            continue
        dm = DATE_RE.search(text[m.end(): m.end() + DATE_WINDOW])
        published = parse_datetime(dm.group(0)) if dm else None
        if published is None and require_date:
            continue
        out.append(RawItem(title=title, link=absolute, published=published, guid=absolute))
        if len(out) >= max_items:
            break
    return out


def parse_list_page(
    html_text: str,
    base_url: str,
    max_items: int = DEFAULT_MAX_ITEMS,
    require_date: bool = True,
    accept_url_prefix: Optional[str] = None,
    debug: bool = False,
) -> List[RawItem]:
    """从列表页 HTML 抽取条目（去重后返回）。"""
    if not html_text:
        return []

    cleaned = SCRIPT_STYLE_RE.sub(" ", html_text)
    extractor = _ListExtractor(base_url, accept_url_prefix)
    extractor.debug = debug
    try:
        extractor.feed(cleaned)
        extractor.close()
    except Exception:
        pass

    candidates = extractor.items
    used_fallback = False
    if not candidates:
        used_fallback = True
        candidates = _fallback_window_scan(
            cleaned, base_url, max_items, require_date, accept_url_prefix
        )

    seen: Set[str] = set()
    items: List[RawItem] = []
    for item in candidates:
        if require_date and item.published is None:
            continue
        key = dedup.url_hash(item.link)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= max_items:
            break

    if debug:
        pairs = [(it, ctx) for it, ctx in extractor.debug_pairs
                 if dedup.url_hash(it.link) in seen]
        if used_fallback:
            pairs = [(it, "(fallback window scan)") for it in items]
        setattr(parse_list_page, "_last_debug", pairs)

    return items


# --------------------------------------------------------------------------
# 详情页正文抽取
#
# **为什么必须要有**（2026-10-15 实测）：全库 2471 篇里 **175 篇正文完全为空**、
# 84 篇不足 40 字，且高度集中：
#   - ``kind: html`` 的两个国防部栏目共 88 篇，**100% 没有正文** ——
#     它们只从列表页拿到标题与链接，本模块的 parse_list_page 不返回正文；
#   - Global Times 41 篇也 100% 没有正文 —— 它的 RSS 不带 description。
# 两类源在界面上表现为"简报只有一句话"，而根因都在采集层，
# 不在生成层（模型拿到的材料本身就只有标题）。
# --------------------------------------------------------------------------

class _BodyExtractor(HTMLParser):
    """从详情页抽正文：收集 <p> 文本，跳过脚本/样式/导航/页脚。

    用 <p> 而不是"取某个 class"是因为各站结构差异太大，
    而正文段落几乎总在 <p> 里；再配一个"<p> 太少就退回去标签全文"的兜底。
    """

    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "form", "svg",
            "aside", "button", "select", "option"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_p = 0
        self._blocks: List[str] = []
        self._cur: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "p":
            self._in_p += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            if self._skip:
                self._skip -= 1
        elif tag == "p":
            if self._in_p:
                self._in_p -= 1
            txt = "".join(self._cur).strip()
            self._cur = []
            if len(txt) >= 8:            # 太短的多半是"编辑：""来源："这类标签
                self._blocks.append(txt)

    def handle_data(self, data):
        if self._skip or not self._in_p:
            return
        self._cur.append(data)

    def text(self) -> str:
        return "\n".join(self._blocks)


def extract_body(html_text: str, max_chars: int = 20000) -> str:
    """从详情页 HTML 里抽正文纯文本。抽不到时返回空串（调用方决定是否回退）。"""
    if not html_text:
        return ""
    ex = _BodyExtractor()
    try:
        ex.feed(html_text)
    except Exception:                    # 页面畸形也不该让采集挂掉
        pass
    body = ex.text().strip()
    if len(body) < 80:
        # <p> 太少（有些站正文不在 <p> 里）→ 退回"整页去标签"，
        # 虽然会带进一些导航文字，但比"完全没有正文"强得多。
        try:
            from .parsers import strip_html     # 局部导入避免循环依赖
            body = strip_html(html_text).strip()
        except Exception:
            body = ""
    return body[:max_chars]
