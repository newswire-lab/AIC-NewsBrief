"""AIC-NewsBrief：多源公开新闻聚合与可溯源简报生成。

包初始化职责：统一控制台编码。
Windows 控制台默认 GBK，打印非 GBK 字符（某些符号、扩展区汉字）会抛
UnicodeEncodeError 直接中断程序，这里统一把 errors 放宽为 replace。
"""

from __future__ import annotations

import sys

__version__ = "0.1.0"

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:  # pragma: no cover - 极端环境下忽略
            pass
