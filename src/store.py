"""SQLite 存储层：文章入库、增量去重、源状态与健康记录。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import json
import sqlite3

from . import config
from .models import Article, SourceRun

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT    NOT NULL,
    source_name   TEXT    NOT NULL,
    source_org    TEXT    NOT NULL DEFAULT '',
    source_tier   INTEGER NOT NULL DEFAULT 2,
    region        TEXT    NOT NULL DEFAULT 'cn',
    lang          TEXT    NOT NULL DEFAULT 'zh',
    url           TEXT    NOT NULL,
    url_hash      TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    norm_title    TEXT    NOT NULL DEFAULT '',
    summary       TEXT    NOT NULL DEFAULT '',
    summary_zh    TEXT    NOT NULL DEFAULT '',
    content       TEXT    NOT NULL DEFAULT '',
    published_at  TEXT,
    fetched_at    TEXT,
    title_hash    TEXT    NOT NULL DEFAULT '',
    content_fp    TEXT    NOT NULL DEFAULT '',
    event_id      TEXT    NOT NULL DEFAULT '',
    cluster_size  INTEGER NOT NULL DEFAULT 1,
    credibility   REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source     ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_title_hash ON articles(title_hash);
CREATE INDEX IF NOT EXISTS idx_articles_content_fp ON articles(content_fp);
CREATE INDEX IF NOT EXISTS idx_articles_event      ON articles(event_id);

CREATE TABLE IF NOT EXISTS source_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0,
    new_count   INTEGER NOT NULL DEFAULT 0,
    dup_count   INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',
    run_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_source_time ON source_runs(source_id, run_at DESC);

CREATE TABLE IF NOT EXISTS source_state (
    source_id        TEXT PRIMARY KEY,
    etag             TEXT    NOT NULL DEFAULT '',
    last_modified    TEXT    NOT NULL DEFAULT '',
    last_ok_at       TEXT,
    last_status      TEXT    NOT NULL DEFAULT '',
    consecutive_fail INTEGER NOT NULL DEFAULT 0,
    total_items      INTEGER NOT NULL DEFAULT 0,
    total_new        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS briefs (
    event_id     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    backend      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    headline     TEXT NOT NULL DEFAULT '',
    value_point  TEXT NOT NULL DEFAULT '',
    brief        TEXT NOT NULL DEFAULT '',
    credibility  REAL NOT NULL DEFAULT 0,
    accepted     INTEGER NOT NULL DEFAULT 0,
    verify_json  TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_briefs_created ON briefs(created_at DESC);
"""

ARTICLE_COLUMNS: Sequence[str] = (
    "source_id", "source_name", "source_org", "source_tier", "region", "lang", "url",
    "url_hash", "title", "norm_title", "summary", "content", "published_at",
    "fetched_at", "title_hash", "content_fp", "event_id", "cluster_size",
    "credibility",
)


def connect(db_file: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_file) if db_file else config.DB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# 事件关键词标签：由自研算法（src/keywords.py）批量预计算后写入。
# 单独建表而不加在 articles 上，是因为标签是**事件级**属性，
# 挂在某一篇文章上会随代表文章的变化而丢失。
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_tags (
    event_id   TEXT PRIMARY KEY,
    tags       TEXT NOT NULL DEFAULT '',
    n_docs     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(EXTRA_SCHEMA)
    _ensure_columns(conn)
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """幂等轻量迁移：为已存在的老库补上后加的列。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    migrations = (
        ("source_org", "TEXT NOT NULL DEFAULT ''"),
        ("summary_zh", "TEXT NOT NULL DEFAULT ''"),
        # 外文标题的中文译名（自研批量预热脚本 scripts/warm_translations.py 写入）
        ("title_zh", "TEXT NOT NULL DEFAULT ''"),
        # 新闻分区（自研关键词加权分类，见 src/topics.py）：主分区 + 次分区
        ("topic", "TEXT NOT NULL DEFAULT ''"),
        ("topic2", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, ddl in migrations:
        if name not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {ddl}")


# --------------------------------------------------------------------------
# 文章
# --------------------------------------------------------------------------

def exists_url_hash(conn: sqlite3.Connection, url_hash: str) -> bool:
    row = conn.execute("SELECT 1 FROM articles WHERE url_hash = ? LIMIT 1", (url_hash,)).fetchone()
    return row is not None


def insert_article(conn: sqlite3.Connection, art: Article) -> bool:
    """插入文章。

    返回 ``True`` = 入库成功；``False`` = **URL 重复**（正常情况，调用方计入 dup）。

    **曾有的 bug**：`except sqlite3.IntegrityError: return False` 把唯一冲突、
    NOT NULL 缺失、CHECK 失败等全部当成「URL 重复」。而调用方 collect.py 只看
    布尔值，于是真正的表结构/数据错误会被静默丢弃且不计入任何统计 ——
    问题会藏到很久以后才以「某源怎么一直没新文章」的形式暴露。
    现在只有 URL 唯一冲突才返回 False，其余完整性错误原样抛出。
    """
    row = art.to_row()
    sql = (
        f"INSERT INTO articles ({', '.join(ARTICLE_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(ARTICLE_COLUMNS))})"
    )
    try:
        conn.execute(sql, [row.get(c) for c in ARTICLE_COLUMNS])
        return True
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint failed: articles.url_hash" in str(exc):
            return False
        raise


def backfill_source_org(conn: sqlite3.Connection, commit: bool = True) -> int:
    """把 config/orgs.yaml 的机构映射回填到已有文章（幂等）。

    新增 source_org 列后老数据为空值，会导致"同一出版集团的多站点"
    被当成不同机构计数、从而高估交叉印证度，
    因此每次聚类与打分前都先回填一次。
    """
    org_map = config.load_org_map()
    if not org_map:
        return 0
    changed = 0
    for source_id, org in org_map.items():
        cur = conn.execute(
            "UPDATE articles SET source_org = ? WHERE source_id = ? AND source_org != ?",
            (org, source_id, org),
        )
        changed += cur.rowcount or 0
    if commit:
        conn.commit()
    return changed


def recent_title_hashes(conn: sqlite3.Connection, days: int = 7) -> List[Tuple[str, int]]:
    """取最近 N 天的 (url_hash, title_simhash)，供近似重复检测使用。"""
    rows = conn.execute(
        "SELECT url_hash, title_hash FROM articles "
        "WHERE published_at IS NOT NULL "
        "  AND datetime(published_at) >= datetime('now', ?) "
        "  AND title_hash != ''",
        (f"-{int(days)} days",),
    ).fetchall()
    out: List[Tuple[str, int]] = []
    for r in rows:
        try:
            out.append((r["url_hash"], int(r["title_hash"], 16)))
        except (TypeError, ValueError):
            continue
    return out


def recent_articles(
    conn: sqlite3.Connection,
    hours: int = 24,
    limit: int = 50,
    region: Optional[str] = None,
    topic: Optional[str] = None,
) -> List[sqlite3.Row]:
    sql = (
        "SELECT * FROM articles WHERE datetime(COALESCE(published_at, fetched_at)) >= datetime('now', ?) "
    )
    params: List[Any] = [f"-{int(hours)} hours"]
    if region:
        sql += "AND region = ? "
        params.append(region)
    if topic:
        sql += "AND topic = ? "
        params.append(topic)
    sql += "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?"
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
    last24 = conn.execute(
        "SELECT COUNT(*) AS c FROM articles WHERE datetime(COALESCE(published_at, fetched_at)) >= datetime('now', '-24 hours')"
    ).fetchone()["c"]
    last7 = conn.execute(
        "SELECT COUNT(*) AS c FROM articles WHERE datetime(COALESCE(published_at, fetched_at)) >= datetime('now', '-7 days')"
    ).fetchone()["c"]
    by_source = conn.execute(
        "SELECT source_name, region, COUNT(*) AS c, MAX(fetched_at) AS latest "
        "FROM articles GROUP BY source_id ORDER BY c DESC"
    ).fetchall()
    by_region = conn.execute(
        "SELECT region, COUNT(*) AS c FROM articles GROUP BY region ORDER BY c DESC"
    ).fetchall()
    return {
        "total": total, "last24": last24, "last7": last7,
        "by_source": by_source, "by_region": by_region,
    }


# --------------------------------------------------------------------------
# 源运行记录与状态
# --------------------------------------------------------------------------

def record_run(conn: sqlite3.Connection, run: SourceRun, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO source_runs (source_id, status, item_count, new_count, dup_count, "
        "http_status, elapsed_ms, error, run_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run.source_id, run.status, run.item_count, run.new_count, run.dup_count,
         run.http_status, run.elapsed_ms, run.error[:500], run.run_at),
    )
    if commit:
        conn.commit()


def get_state(conn: sqlite3.Connection, source_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
    ).fetchone()


def update_state(
    conn: sqlite3.Connection,
    source_id: str,
    status: str,
    ok: bool,
    etag: str = "",
    last_modified: str = "",
    item_delta: int = 0,
    new_delta: int = 0,
    commit: bool = True,
) -> None:
    row = get_state(conn, source_id)
    if row is None:
        conn.execute(
            "INSERT INTO source_state (source_id, etag, last_modified, last_ok_at, last_status, "
            "consecutive_fail, total_items, total_new) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, etag, last_modified, None if not ok else _now(), status,
             0 if ok else 1, item_delta, new_delta),
        )
    else:
        conn.execute(
            "UPDATE source_state SET etag = ?, last_modified = ?, "
            "last_ok_at = COALESCE(?, last_ok_at), last_status = ?, "
            "consecutive_fail = ?, total_items = total_items + ?, total_new = total_new + ? "
            "WHERE source_id = ?",
            (
                etag or row["etag"],
                last_modified or row["last_modified"],
                _now() if ok else None,
                status,
                0 if ok else (row["consecutive_fail"] + 1),
                item_delta, new_delta, source_id,
            ),
        )
    if commit:
        conn.commit()


def _now() -> str:
    from .models import utcnow, iso
    return iso(utcnow()) or ""


# --------------------------------------------------------------------------
# 简报持久化
# --------------------------------------------------------------------------

def save_brief(conn: sqlite3.Connection, payload: Dict[str, Any]) -> None:
    """保存一条简报（按 event_id 覆盖）。"""
    verification = payload.get("verification") or {}
    conn.execute(
        "INSERT INTO briefs (event_id, created_at, backend, model, headline, "
        "value_point, brief, credibility, accepted, verify_json, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(event_id) DO UPDATE SET "
        "  created_at=excluded.created_at, backend=excluded.backend, "
        "  model=excluded.model, headline=excluded.headline, "
        "  value_point=excluded.value_point, brief=excluded.brief, "
        "  credibility=excluded.credibility, accepted=excluded.accepted, "
        "  verify_json=excluded.verify_json, payload_json=excluded.payload_json",
        (
            str(payload.get("event_id", "")),
            _now(),
            str(payload.get("backend", "")),
            str(payload.get("model", "")),
            str(payload.get("headline", "")),
            str(payload.get("value_point", "")),
            str(payload.get("brief", "")),
            float(payload.get("credibility") or 0.0),
            1 if verification.get("accepted") else 0,
            json.dumps(verification, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()


def get_brief(conn: sqlite3.Connection, event_id: str) -> Optional[Dict[str, Any]]:
    """读取已保存的简报。"""
    row = conn.execute(
        "SELECT payload_json FROM briefs WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None or not row["payload_json"]:
        return None
    try:
        return json.loads(row["payload_json"])
    except ValueError:
        return None


def recent_briefs(conn: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT event_id, created_at, backend, model, headline, value_point, "
        "       credibility, accepted, substr(brief, 1, 120) AS brief_head "
        "FROM briefs ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()


def source_health(conn: sqlite3.Connection, limit: int = 200) -> List[sqlite3.Row]:
    """每个源最近一条运行记录。

    **曾有的 bug**：这里原本 LEFT JOIN `source_state` 取它的 `name` 列，
    但 `source_state` 根本没有 `name` 列（只有 etag/last_ok_at/失败计数等），
    所以本函数任何一次调用都抛 `no such column: name`，源健康视图永远拿不到数据。
    源的可读名字本来就在配置里（config/sources.yaml），库里只有 source_id，
    因此这里直接回传 source_id 作为 source_name，保持调用方的字段名不变。
    """
    return conn.execute(
        "SELECT r.*, r.source_id AS source_name FROM source_runs r "
        "WHERE r.id IN (SELECT MAX(id) FROM source_runs GROUP BY source_id) "
        "ORDER BY r.new_count DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
