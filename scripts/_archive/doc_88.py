# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""把 §8.8 追加到进度交接文档，并修正 §8.7.4 里已过期的截止日期描述。"""
import os, sys, re

ROOT = _ROOT
P = os.path.join(ROOT, "docs", "进度交接.md")
src = open(P, encoding="utf-8").read()
old_len = len(src)

# --- 1. 修正 8.7.4 里已过期的截止日期 ---
OLD_TAIL = """**距提交截止（2026-10-15 20:00）剩 3 天**，建议顺序：
① 重启服务 → 重截截图 →（可选）验证生成层两个修复
② 需求 3 搜索 →  ③ 需求 4 翻译 + 预热 →  ④ 重跑 PDF/自查/打包 →  ⑤ 录演示视频"""
NEW_TAIL = """**提交截止已确认为 2026-10-30**（用户 2026-10-14 告知，原记的 10-15 有误）。
时间宽裕，故 4 项需求全部做完整版，不再压缩取舍；顺带把此前搁置的
「生成层两项修复」也纳入验证范围（见 §8.8）。"""
assert OLD_TAIL in src, "未找到 8.7.4 的截止日期段落"
src = src.replace(OLD_TAIL, NEW_TAIL)

SEC = """

### 8.8 检索与自动翻译落地（2026-10-14）

#### 8.8.1 截止时间更正

用户 2026-10-14 明确：**作品提交是 10 月 30 日**。此前文档里记的 10-15 有误，
一度按「只剩 3 天」压缩了取舍 —— 现已改回完整版：4 项需求全做，
并把此前搁置的「生成层两项修复」（自适应简报长度、溯源提示词）重新纳入验证。

#### 8.8.2 需求 3：关键词检索（已完成）

**新增 `src/search.py`（纯 SQL + 自研排序，不调模型）。**

两个被实测逼出来的设计决定，都写进了模块文档字符串：

1. **不用 FTS5，用 `LIKE`。** 库里文章是千级，`LIKE` 全表扫描实测 **23~187 ms**，
   完全够用；而 SQLite 的 FTS5 对中文默认用 `unicode61` 分词器，会把**一整句中文
   切成一个 token**，想搜「诺贝尔」必须写成 `诺贝尔*` 前缀查询才命中 —— 对普通用户
   是更差的选择。所以在当前规模下 `LIKE` 是更准的方案，不是妥协。
2. **排序放在 Python。** 命中位置决定这条结果的价值：
   `标题 5.0 分 > 摘要 2.5 分 > 正文 1.0 分 > 来源名 0.5 分`，
   再叠时效加成 `1.5 × exp(-age/72h)`（与可信度 `f_time` 同一衰减尺度，
   免得同一界面出现两套"新鲜"标准）。权重写成常量，可解释、可测试。

**自查发现并修掉的 3 个真问题**（都是先跑再说，不是读代码"看出来"的）：

| # | 现象 | 修法 |
|---|---|---|
| 1 | 搜 `hypersonic` 时，Sightline Media Group 旗下 Defense News / Army Times / Military Times / Navy Times / Air Force Times **五条结果标题完全一样**，各占一行，整页被同一件事刷屏 | 按 `title_hash` **折叠同一通稿**，保留分最高的一条，记下「共 N 站同稿」。实测 `hypersonic` 由 10 行降到 6 行，其中 1 行标注「5 站同稿」 |
| 2 | 搜「中国新闻社」**命中 0 条** —— 只能搜正文，不能按来源过滤 | WHERE 里加入 `source_name / source_org`，给 `W_SOURCE=0.5` 的低权重，保证排在真内容命中之后。实测「新华社 无人机」能正确返回（标题命中 5.50 分排第一） |
| 3 | 「待翻译外文标题」按 `lang != 'zh'` 统计得到 994，但抽查发现**中国军网有 8 篇英文稿被标成了 `lang='zh'`** | 弃用 `lang` 字段做判断，统一交给 `normalize.needs_normalize()`（与归一化模块同一口径）。待译总数由 994 修正为 **1002** |

**自查实测数据**（`compileall` rc=0，20/20 模块可导入，`build_ui()` 58 个组件）

```
库存 1784 篇 ｜ 单条查询 12~187 ms
「诺贝尔」        -> 3 条，全部标题命中
「无人机 反制」    -> 1 条（是「无人机」15 条的子集，AND 语义成立）
「中国新闻社」     -> 8 条，17 ms（新增的来源过滤能力）
排序合理性        标题命中最低分 5.00 > 正文命中最高分 1.79  ✅
```

**落地位置**
- `src/search.py`：`parse_query` / `search_articles` / `_collapse_same_story` / `coverage`
- `src/app.py`：新增「搜索」页签（关键词框 + 时窗 + 条数 + 结果表 10 列）
- `src/cli.py`：新增 `search` 子命令

**一条刻意的约束**：搜索回调**不挂 `gr.Timer`**。定时器会每 60 秒把全表 LIKE 扫一遍，
而用户可能根本没在看这一页 —— 纯属白烧 CPU。检索由「回车 / 按钮」触发，只读不写。
（其余三个页签保持自动刷新，因为它们是"看"的场景，不是"找"的场景。）

#### 8.8.3 需求 4：外文自动翻译（代码完成，待批量预热）

**分两层，各司其职：**

- `normalize.translate_title()`：**只翻标题**（80 tokens，2~4 秒/条）。
  与既有的 `normalize_text()`（正文转写 3~6 句事实要点，600 tokens）分开写，
  因为「标题」和「事实要点」对输出的要求完全不同 —— 沿用同一个模板会得到
  一段话而不是一个标题。提示词明确要求：≤40 字、保留专名型号原文写法、
  数字原样不改；配 `clean_title()` 收口（去引号、去「标题：」前缀、只取首行、限长），
  实测模型偶尔会连包装一起吐出来。
- `scripts/warm_translations.py`：**离线批量预热**，写入 `articles.title_zh`。

**为什么必须离线做**：单条要调一次本地模型，放进 Web 界面同步等会卡死页面；
做成后台线程又要处理并发与写库竞争。这与本项目一贯的分工一致 ——
**慢的、要调模型的都在离线；界面里只做「读 + 自研算法」**。

翻译完顺带重算事件关键词标签（`keywords.tag_all`，全库 0.3 秒）：因为关键词的候选文本
优先取 `summary_zh` / 中文标题，所以**翻译一落库，英文事件的关键词标签会自动变中文**。

**界面里的优雅降级**：一律写 `title_zh or title`，译名为空就显示原文，
不阻塞、不报错。检索结果表另开一列放原文标题，用户能核对译文对应哪条原句。

#### 8.8.4 下次继续的入口

```bash
# 1. 数据刷新（采集 -> 聚类 -> 打分 -> 打关键词标签，全链路约 3~5 分钟）
python -m src.cli pipeline
# 2. 翻译预热（大约 3 秒/条，先跑 150 条够演示用）
python scripts/warm_translations.py --limit 150
# 3. 只翻最近 7 天的、并顺带转写正文
python scripts/warm_translations.py --hours 168 --with-summary --limit 60
# 4. 检索（命令行视角）
python -m src.cli search 无人机 --hours 168
python -m src.cli search "新华社 无人机" --limit 10
# 5. 起服务（**改过 app.py 后必须重启才会生效**）
python -m src.app --port 7860
```

**仍未完成**
1. 翻译预热实跑（代码已就绪，未在真实数据上批量跑过）
2. 重启服务后浏览器逐项验收（关键词列 / 搜索页签 / 译名）
3. 生成层两项修复的端到端验证（自适应长度 + 溯源提示词）
4. 重拍 `docs/screenshots/`（现图已过期：没有关键词列、没有搜索页签）
5. 重出 PDF、重跑匿名自查、重打包 `dist/AIC-NewsBrief-src.zip`
6. 用户侧：录演示视频、填 `config/anonymize_watchlist.txt`
"""

# --- 2. 追加 §8.8 ---
assert "### 8.8 " not in src, "§8.8 已存在，勿重复追加"
src = src.rstrip("\n") + SEC

open(P, "w", encoding="utf-8", newline="\n").write(src)

# --- 3. 回读校验 ---
chk = open(P, encoding="utf-8").read()
heads = [l for l in chk.splitlines() if re.match(r"^#{2,4} 8", l)]
print("原 %d 字符 -> 新 %d 字符" % (old_len, len(chk)))
print("八章小节顺序：")
for h in heads:
    print("  ", h[:64])
assert chk.index("### 8.5") < chk.index("### 8.6") < chk.index("### 8.7") < chk.index("### 8.8")
assert "2026-10-30" in chk
assert "2026-10-15 20:00" not in chk, "旧截止日期没清干净"
assert chk.count("### 8.8 ") == 1
print("断言通过；总行数 =", len(chk.splitlines()))
