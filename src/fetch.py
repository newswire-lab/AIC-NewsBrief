"""HTTP 抓取层：统一 UA、超时、重试、条件请求（ETag / Last-Modified）。

设计要点：
- 只做"礼貌抓取"：串行 + 每源间隔 + 条件请求，不并发轰炸、不绕过反爬。
- 304 直接复用上次结果，既省流量也降低对源站压力。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional
import time

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.8, text/html;q=0.7, */*;q=0.5"
)

DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 2
RETRY_BACKOFF = 1.5
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: Optional[int] = None
    content: bytes = b""
    encoding: Optional[str] = None
    elapsed_ms: int = 0
    not_modified: bool = False
    error: str = ""
    etag: str = ""
    last_modified: str = ""


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": _ACCEPT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    return s


def fetch(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    validators: Optional[Dict[str, str]] = None,
) -> FetchResult:
    """抓取单个 URL。validators 支持 {"etag": ..., "last_modified": ...}。"""
    sess = session or build_session()
    validators = validators or {}
    headers: Dict[str, str] = {}
    if validators.get("etag"):
        headers["If-None-Match"] = validators["etag"]
    if validators.get("last_modified"):
        headers["If-Modified-Since"] = validators["last_modified"]

    last_error = ""
    last_status: Optional[int] = None
    t0 = time.time()

    for attempt in range(retries + 1):
        try:
            resp = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            elapsed = int((time.time() - t0) * 1000)

            if resp.status_code == 304:
                return FetchResult(
                    url=url, ok=True, status=304, not_modified=True,
                    elapsed_ms=elapsed, encoding=resp.encoding,
                    etag=validators.get("etag", ""),
                    last_modified=validators.get("last_modified", ""),
                )
            if resp.status_code in RETRY_STATUS and attempt < retries:
                last_status, last_error = resp.status_code, f"HTTP {resp.status_code}"
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            if resp.status_code >= 400:
                return FetchResult(
                    url=url, ok=False, status=resp.status_code, elapsed_ms=elapsed,
                    error=f"HTTP {resp.status_code} {resp.reason}",
                )
            return FetchResult(
                url=url, ok=True, status=resp.status_code, content=resp.content,
                encoding=resp.encoding, elapsed_ms=elapsed,
                etag=resp.headers.get("ETag", ""),
                last_modified=resp.headers.get("Last-Modified", ""),
            )
        except requests.RequestException as exc:
            elapsed = int((time.time() - t0) * 1000)
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            return FetchResult(url=url, ok=False, status=last_status or None,
                               elapsed_ms=elapsed, error=last_error)

    return FetchResult(url=url, ok=False, status=last_status, error=last_error,
                       elapsed_ms=int((time.time() - t0) * 1000))
