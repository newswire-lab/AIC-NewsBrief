"""数据结构定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """统一输出带时区的 ISO8601 字符串，便于 SQLite 里按字符串排序。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Article:
    """一篇新闻。字段命名与 SQLite 表列一一对应。"""

    source_id: str
    source_name: str
    source_org: str
    source_tier: int
    region: str
    lang: str
    url: str
    url_hash: str
    title: str
    norm_title: str = ""
    summary: str = ""
    content: str = ""
    published_at: Optional[str] = None   # ISO8601(UTC)
    fetched_at: Optional[str] = None     # ISO8601(UTC)
    title_hash: str = ""                 # SimHash 的 hex 表示
    content_fp: str = ""
    # 以下字段由 D3-D4 的事件聚类与可信度打分填充
    event_id: str = ""
    cluster_size: int = 1
    credibility: float = 0.0
    # 去重诊断信息（不入库的原始值）
    raw_published: str = ""

    def to_row(self) -> dict:
        d = self.__dict__.copy()
        d.pop("raw_published", None)
        return d


@dataclass
class SourceRun:
    """单次源抓取的运行记录，用于源健康度分析。"""

    source_id: str
    status: str                          # ok / empty / http_error / parse_error / network_error
    item_count: int = 0
    new_count: int = 0
    dup_count: int = 0
    http_status: Optional[int] = None
    elapsed_ms: int = 0
    error: str = ""
    run_at: str = ""
