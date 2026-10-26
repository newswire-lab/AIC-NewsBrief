# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""把 §8.8.6（本轮收工状态）追加到进度交接文档。"""
import os, re

ROOT = _ROOT
P = os.path.join(ROOT, "docs", "进度交接.md")
src = open(P, encoding="utf-8").read()
old_len = len(src)

SEC = """
#### 8.8.6 本轮收工状态（2026-10-14 夜）

**四项需求全部落地，且都在浏览器里逐项验收过。**

| # | 需求 | 状态 | 浏览器验收证据 |
|---|---|---|---|
| 1 | 热点事件标签改为按事件提炼关键词 | ✅ | 「事件关键词」列已出现：「亿美元核、雷神获、合同」「名士官转隶太空作战部门、美陆军将」「亚马逊、投资、股票、公司」 |
| 2 | 简报下拉框改事件标题 | ✅ | 标签 = `标题 [N分·M机构·X小时前]`（代码级验证） |
| 3 | 关键词检索 | ✅ | 「搜索」页签实测：搜「无人机」得 **21 条**，排序为标题命中（6.48/6.46）> 正文命中（2.5）；含「原文标题」「命中片段」列 |
| 4 | 外文自动翻译 | ✅ | 表内与检索结果均显示中文译名 + 原文标题；如「雷神获价值80亿美元核指挥终端合同」/「俄罗斯新型炮塔可自动拦截敌方无人机」 |

**实测数据（都来自真实运行，不是估算）**

```
数据：文章 2108 篇 ｜ 事件簇 1403 个 ｜ 采集新增 324 条 / 0 失败
标签：1403 个事件全部有标签，平均 3.63 个；跨进程确定性 ✅
检索：单条查询 12~187 ms；「无人机」21 条
翻译：60/60 成功，65.4 秒（平均 1.1 秒/条）—— 比原先估计的 2~4 秒快
       外文标题译名进度 60/1195；译名落库后英文事件标签自动转中文（18/18）
界面：build_ui() 58 个组件（新增搜索页签后由 48 增至 58）
```

**翻译预热要接着做**（当前只译了 5%）

```bash
# 每条约 1.1 秒，一次跑 300 条约 5.5 分钟
python scripts/warm_translations.py --limit 300
# 只补最近 7 天的
python scripts/warm_translations.py --hours 168 --limit 300
# 想连正文事实要点一起转写（慢很多，但简报素材质量更好）
python scripts/warm_translations.py --with-summary --limit 60
```

**踩坑记录：用浏览器工具验证 Gradio 界面时**

- **`locator.fill()` 不会让 Gradio 的输入框状态更新** —— DOM 里值变了，
  但组件内部状态还是空的，回调收到的 `query` 是空串，
  表现为「点了按钮/按了回车，提示语却一直说『请输入关键词』」。
  必须用 **`pressSequentially(文字, {delay})` 逐字输入**才会触发。
  这个坑很容易被误判成"回调坏了"，实际服务端日志里一条错误都没有。
- **`click()` 报 `Element is outside of the viewport`**：集成浏览器的视口很小，
  先 `setViewportSize({width:1440, height:1000})`，或对页签用 `dispatchEvent('click')`。
- **Gradio 页签按钮「不稳定」导致 click 超时**：用 `dispatchEvent` 绕开稳定性等待。
- **`run_playwright_code` 里写的是 JavaScript**（写 Python 的 `try:` /
  `key=value` 会报 `Unexpected token ':'`），且 `page` 是已注入的同步对象，
  用 `await` 即可。

**下一步（按优先级）**

1. **翻译预热接着跑**：目标把最近 7 天的外文标题译完（当前 60/1195）
2. **生成层两项修复的端到端验证**（§8.7.3 遗留）：
   `adaptive_brief_chars` 自适应长度 + `TRACEABILITY_HINT` 溯源提示词，
   要跑若干事件看「通过率 vs 素材厚度」的关系，并如实记录
3. **重拍截图**：`docs/screenshots/` 现有 4 张已过期
   （没有关键词列、没有搜索页签、没有中文译名）
4. **重出 PDF + 重跑匿名自查 + 重打包** `dist/AIC-NewsBrief-src.zip`
5. **文档同步**：README 与 `技术报告.md` 要补检索、翻译、关键词三节，
   并把 §8.8.5 里那三处缺陷的发现过程写进技术报告的「踩坑与修正」一节
   （这是"自研 vs 大模型"论证的有力素材）
6. 用户侧：录演示视频、填 `config/anonymize_watchlist.txt`
"""

assert "#### 8.8.6" not in src
src = src.rstrip("\n") + "\n" + SEC
open(P, "w", encoding="utf-8", newline="\n").write(src)

chk = open(P, encoding="utf-8").read()
heads = [l for l in chk.splitlines() if re.match(r"^#{2,5} 8\.8", l)]
print("原 %d 字符 -> 新 %d 字符" % (old_len, len(chk)))
for h in heads:
    print("  ", h[:72])
assert chk.count("#### 8.8.6") == 1 and "pressSequentially" in chk
print("断言通过；总行数 =", len(chk.splitlines()))
