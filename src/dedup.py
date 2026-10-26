"""去重与近似重复检测。

分三层：
1. URL 归一化 + 指纹  -> 完全重复（同一篇稿件被反复抓到）
2. 标题 SimHash       -> 近似重复（同一事件多家媒体转发，标题只差几个字）
3. 正文指纹            -> 同源转载的兜底判据

SimHash 为纯标准库实现，不依赖第三方库。
"""

from __future__ import annotations

from hashlib import sha1
from typing import Iterable, List, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re

# 常见跟踪参数，参与去重会造成同一篇文章被当成多篇
_TRACKING_PREFIXES = ("utm_", "spm", "from", "ref", "share", "wxm_", "share_")
_TRACKING_KEYS: Set[str] = {
    "from", "ref", "refer", "source", "share_token", "fbclid", "gclid",
    "yclid", "spm", "scm", "s_cid", "tt_from", "isappinstalled", "scene",
}

_WS_RE = re.compile(r"\s+")
# 标题里各家媒体爱加的栏目前缀/后缀，归一化时去掉，提升近似重复命中率
_TITLE_NOISE_RE = re.compile(
    r"^[\s【\[\(（]*(?:独家|原创|视频|图集|直播|重磅|快讯|权威发布|热点|关注|专题)[\s】\]\)）:：\-—]*"
)
_TITLE_TAIL_RE = re.compile(r"[\s\-—|｜]+(?:新华网|人民网|央视网|中国军网|环球网|观察者网|澎湃新闻|中新网|中国新闻网)\s*$")


def normalize_url(url: str) -> str:
    """URL 归一化：小写主机与协议、去跟踪参数、去 fragment、去尾部斜杠。"""
    if not url:
        return ""
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return url
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    query_pairs = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        lk = k.lower()
        if lk in _TRACKING_KEYS or any(lk.startswith(p) for p in _TRACKING_PREFIXES):
            continue
        query_pairs.append((k, v))
    query_pairs.sort()
    query = urlencode(query_pairs)

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """归一化 URL 的 SHA1，作为主去重键。"""
    return sha1(normalize_url(url).encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    """标题归一化：去栏目前缀/媒体后缀、去空白与标点噪声、统一小写。"""
    if not title:
        return ""
    t = _WS_RE.sub("", title.strip())
    t = _TITLE_NOISE_RE.sub("", t)
    t = _TITLE_TAIL_RE.sub("", t)
    # 统一全角/半角标点，全角字母数字转半角
    table = {
        "，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?",
        "（": "(", "）": ")", "【": "[", "】": "]", "“": '"', "”": '"',
        "‘": "'", "’": "'", "、": ",", "—": "-", "～": "~", "　": "",
    }
    out = "".join(table.get(ch, ch) for ch in t)
    return out.lower()


def char_ngrams(text: str, sizes: Sequence[int] = (2, 3)) -> List[str]:
    """中文友好的字符 n-gram 切分（无需分词）。"""
    text = _WS_RE.sub("", text or "")
    grams: List[str] = []
    for n in sizes:
        if len(text) < n:
            continue
        grams.extend(text[i:i + n] for i in range(len(text) - n + 1))
    return grams


def _hash64(token: str) -> int:
    return int.from_bytes(sha1(token.encode("utf-8")).digest()[:8], "big")


def simhash64(text: str, sizes: Sequence[int] = (2, 3)) -> int:
    """64 位 SimHash。返回 0 表示空文本。"""
    grams = char_ngrams(text, sizes)
    if not grams:
        return 0
    weights = [0] * 64
    for gram in grams:
        h = _hash64(gram)
        for bit in range(64):
            if h >> bit & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1
    out = 0
    for bit in range(64):
        if weights[bit] > 0:
            out |= 1 << bit
    return out


def simhash_hex(value: int) -> str:
    return format(value, "016x")


def hamming64(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def text_fingerprint(text: str, length: int = 300) -> str:
    """正文指纹：取归一化后前 length 个字符做 SHA1。"""
    norm = _WS_RE.sub("", text or "")[:length]
    if not norm:
        return ""
    return sha1(norm.encode("utf-8")).hexdigest()


def title_simhash(title: str) -> int:
    return simhash64(normalize_title(title))


# 标题 SimHash 海明距离阈值：<= 该值视为同一事件（近似重复）
NEAR_DUP_HAMMING = 3


def is_near_dup(h1: int, h2: int, threshold: int = NEAR_DUP_HAMMING) -> bool:
    if h1 == 0 or h2 == 0:
        return False
    return hamming64(h1, h2) <= threshold


def find_near_dup(
    target_hash: int,
    candidates: Iterable[Tuple[str, int]],
    threshold: int = NEAR_DUP_HAMMING,
) -> Tuple[str, int]:
    """在 (标识, simhash) 候选集中找最相近的一个。返回 (标识, 距离)，未命中返回 ("", -1)。"""
    best_id, best_dist = "", -1
    if target_hash == 0:
        return best_id, best_dist
    for cand_id, cand_hash in candidates:
        if cand_hash == 0:
            continue
        d = hamming64(target_hash, cand_hash)
        if d <= threshold and (best_dist == -1 or d < best_dist):
            best_id, best_dist = cand_id, d
    return best_id, best_dist
