# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""把「界面截图自动化」的踩坑与脚本说明补进交接文档。"""
import os, re

ROOT = _ROOT
P = os.path.join(ROOT, "docs", "进度交接.md")
src = open(P, encoding="utf-8").read()
old = len(src)

SEC = """
#### 8.8.7 界面截图自动化：`scripts/take_screenshots.py`（2026-10-14）

原先的截图是手工用浏览器工具拍的，这次要重拍 5 张（新增了搜索页签与中文译名），
于是写成脚本。**过程里踩了 5 个坑，全部记录在此**，否则下次还会重新踩一遍。

**坑 1：集成浏览器的视口是固定的，`setViewportSize` 无效。**
实测 `window.innerWidth` 恒为 **492**，所以拍出来的图只有 412 px 宽，
放进报告完全不可用。→ **必须独立启动一个浏览器实例**。

**坑 2：`locator.screenshot()` 会等元素「稳定」，在本页面上极慢。**
本页面有 60 秒自动刷新定时器，DOM 一直在变，元素截图每张要多等十几秒甚至更久
（一度看起来像卡死）。→ **改成截页面**（`page.screenshot`），没有稳定性检查。

**坑 3：`click(force=True)` 不可靠。**
报 `Element is outside of the viewport`，即使 `force=True` 也点不到。
→ **改用 `dispatch_event("click")`** 直接派发 DOM 事件，Tab 切换与按钮都生效。

**坑 4：Python 的 stdout 被管道重定向时是块缓冲，看起来像"卡住不动"。**
`python script.py | Tee-Object` 时，输出要等缓冲区满或进程结束才出现。
→ **加 `-u`**（或 `flush=True`）。

**坑 5（与截图无关，但同一个坑）：`locator.fill()` 不会更新 Gradio 的组件状态。**
DOM 里值变了，但组件内部状态还是空的，回调收到空字符串 ——
表现为"点了按钮/按了回车，提示语却一直说『请输入关键词』"，
而**服务端日志一条错误都没有**，极易误判成回调坏了。
→ **用 `press_sequentially(文字, delay)` 逐字输入**。

**脚本要点**

- 用系统已装的 **Edge**（`channel="msedge"`）而不是下载 Chromium，
  省掉 ~130 MB 下载，也和 `scripts/make_pdf.py` 走同一套浏览器。
- 默认 1600×1150、浅色主题（`?__theme=light`）、整页截图；系统状态页用 `full_page=True`。
- 简报页会**真跑一次生成**并轮询等「事实一致性校验」出现，默认最多等 240 秒
  （`--brief-wait` 可调，`--skip-brief` 可跳过）。

```bash
# 先起服务，再拍图
python -m src.app --port 7860
python scripts/take_screenshots.py                 # 全部 5 张
python scripts/take_screenshots.py --skip-brief     # 跳过要跑模型的那张
python scripts/take_screenshots.py --width 1360 --theme dark --search-term 诺贝尔
```

**采到的截图（1600 px 宽，浅色主题）**

| 文件 | 内容 | 大小 |
|---|---|---|
| `1_hot_events.png` | 热点事件表（含「事件关键词」列） | 139 KB |
| `5_search.png` | 搜索页签：搜「无人机」30 条命中，含中文译名与原文标题 | 152 KB |
| `2_evidence.png` | 证据链：评分构成 + 全部来源 | 122 KB |
| `4_status.png` | 系统状态（整页） | 待重拍 |
| `3_brief.png` | 简报生成（需真跑一次模型） | 待重拍 |
"""

assert "#### 8.8.7" not in src
src = src.rstrip("\n") + "\n" + SEC
open(P, "w", encoding="utf-8", newline="\n").write(src)

chk = open(P, encoding="utf-8").read()
heads = [l for l in chk.splitlines() if re.match(r"^#{2,5} 8\.8", l)]
print("原 %d 字符 -> 新 %d 字符" % (old, len(chk)))
for h in heads:
    print("  ", h[:74])
assert chk.count("#### 8.8.7") == 1 and "press_sequentially" in chk
print("断言通过；总行数 =", len(chk.splitlines()))
