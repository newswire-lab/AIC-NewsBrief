"""配置加载与路径工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

SOURCES_FILE = CONFIG_DIR / "sources.yaml"
ORGS_FILE = CONFIG_DIR / "orgs.yaml"
DB_FILE = DATA_DIR / "news.db"

# tier -> 可信度基础权重（D3 可信度打分会用到）
TIER_WEIGHTS = {1: 1.0, 2: 0.7, 3: 0.4}
DEFAULT_TIER_WEIGHT = 0.5


@dataclass
class Source:
    """一个新闻源。"""

    id: str
    name: str
    url: str
    org: str = ""            # 所属媒体机构（出版方），用于交叉印证去重
    region: str = "cn"       # cn / intl
    lang: str = "zh"         # zh / en
    tier: int = 2            # 1 官方权威 / 2 主流媒体 / 3 聚合索引
    kind: str = "rss"        # rss / html
    enabled: bool = True
    max_items: int = 50
    note: str = ""

    @property
    def weight(self) -> float:
        return TIER_WEIGHTS.get(self.tier, DEFAULT_TIER_WEIGHT)

    @property
    def label(self) -> str:
        return f"{self.name}({self.id})"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_org_map(path: Optional[Path] = None) -> Dict[str, str]:
    """读取 config/orgs.yaml，返回 source_id -> 机构名。文件缺失时返回空映射。"""
    p = Path(path) if path else ORGS_FILE
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            raw: Dict[str, Any] = yaml.safe_load(f) or {}
    except Exception:
        return {}
    mapping = raw.get("orgs") or {}
    return {str(k): str(v) for k, v in mapping.items() if v}


def load_sources(
    path: Optional[Path] = None,
    only_enabled: bool = True,
    kind: Optional[str] = None,
) -> List[Source]:
    """读取 config/sources.yaml，返回 Source 列表。"""
    p = Path(path) if path else SOURCES_FILE
    if not p.exists():
        raise FileNotFoundError(f"数据源配置文件不存在: {p}")

    with p.open("r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    org_map = load_org_map()
    out: List[Source] = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict) or "id" not in item or "url" not in item:
            continue
        sid = str(item["id"])
        src = Source(
            id=sid,
            name=str(item.get("name", sid)),
            url=str(item["url"]),
            org=str(item.get("org", "")) or org_map.get(sid, sid),
            region=str(item.get("region", "cn")),
            lang=str(item.get("lang", "zh")),
            tier=int(item.get("tier", 2)),
            kind=str(item.get("kind", "rss")),
            enabled=bool(item.get("enabled", True)),
            max_items=int(item.get("max_items", 50)),
            note=str(item.get("note", "")),
        )
        if only_enabled and not src.enabled:
            continue
        if kind and src.kind != kind:
            continue
        out.append(src)
    return out


def source_by_id(sources: Iterable[Source]) -> Dict[str, Source]:
    return {s.id: s for s in sources}
