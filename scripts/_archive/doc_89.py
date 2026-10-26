# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""把「生成简报为什么慢」的诊断与修复写入交接文档。"""
import os, re

ROOT = _ROOT
P = os.path.join(ROOT, "docs", "进度交接.md")
src = open(P, encoding="utf-8").read()
old = len(src)

SEC = """
### 8.9 性能诊断：生成简报为什么慢、为什么会「卡住」（2026-10-14）

用户反馈「生成简报速度太慢、容易卡住」。本节是诊断过程与修复，
**结论是：慢的不是模型，是代码每点一次就在重新加载一遍权重。**

#### 8.9.1 实测数据（不是估算）

```
warm() 把权重读进显存                47.9 s   （434 个分片，11~16 it/s）
同一次脚本运行里出现的三段加载         36 s / 82 s / 10 s
路径 A（修复前界面写法）第 1 次        285.3 s  其中模型调用 192.5 s，产出 184 token ≈ 1.0 token/s
路径 A 第 2 次                        29.6 s  其中模型调用   9.9 s，产出 191 token ≈ 19.3 token/s
```

同一个事件、同一个模型、同样的 1048 个输入 token，**285 秒 vs 29.6 秒**。
差的不是生成，是**两个模型实例同时在显存里**：`device_map="auto"` 在显存不够时
会把层卸载到 CPU，生成速度就从 19 token/s 掉到 1 token/s。
第二次之所以恢复正常，是因为第一个实例已经被回收。

#### 8.9.2 根因（五条，按影响排序）

1. **每次点击都新建后端 → 重新加载权重。**
   `app.ui_generate` 调的是 `llm.build_backend(cfg, kind)`，
   而 `build_backend` 每次都 `return LocalTransformersBackend(cfg)` —— 新对象、
   `self._model = None`，首次 `generate()` 就触发一次完整加载。
   更要命的是缓存判据写成了「**只有 `kind is None` 才走缓存**」，
   而界面永远会传 `kind`（单选框总有值）→ **缓存 100% 失效**。
2. **请求内为每篇外文材料做实时归一化。**
   `brief.build_material` 对每篇没有 `summary_zh` 的外文文章调一次模型（上限 600 token）。
   一个 5 篇的英文事件，在正式生成**之前**就要先跑 4~5 次推理。
   离线只翻了标题（`title_zh`），`summary_zh` 一直为空，所以这笔开销每次都在付。
3. **`max_new_tokens` 写死 1200**，与目标字数无关。自适应目标最低会到 120 字，
   而小模型不会自己停，多出来的预算全是白等的时间。
4. **扩写是第二次完整生成**：草稿一旦偏短（小模型常见），
   `expand_brief` 会用同一份长材料再跑一遍。
5. **队列单并发**：生成占用唯一 worker，期间整页交互都被排队 ——
   用户感觉到的「卡住」就是这么来的。

#### 8.9.3 顺带查出的一个真缺陷：兜底可能交出**空简报**

验证缓存时发现：事件 `evt_05db199ce766f3` 存进库里的 `brief` **长度是 0**。
原因在 `_fallback_extractive` —— 它从「等级最高的来源」的正文里切句子，
而有些源**只给标题、没有正文**（实测个别栏目如此），于是切出空列表，
兜底结果就是一份空简报。已修：正文为空时退到标题，标题也空则给明确占位串。
**宁可退到标题，也不能交白卷。**

#### 8.9.4 修复清单

| # | 改动 | 文件 |
|---|---|---|
| 1 | 后端缓存**按 kind 分键**（`_cached_backends`），`get_backend` 不再被 kind 绕过 | `src/llm.py` |
| 2 | 新增 `BaseBackend.warm()` 与 `llm.prewarm_async()`；服务启动即在后台预热（`--no-prewarm` 可关） | `src/llm.py` / `src/app.py` |
| 3 | `ui_generate` 改用 `get_backend(cfg, kind)`；`ui_stats` 也改用 `get_backend`（顺带让「生成后端」显示真实 `load_mode`） | `src/app.py` |
| 4 | `MAX_NORMALIZE_PER_EVENT = 3`：请求内最多实时归一化 3 篇，并在材料里注明「离线预热可消除此开销」 | `src/brief.py` |
| 5 | `max_tokens_for()`：`max_new_tokens` 随目标字数缩放（120 字→596，300 字→1024 封顶），主生成与扩写都传 | `src/brief.py` |
| 6 | `demo.queue(default_concurrency_limit=8)`；生成事件 `concurrency_id="llm"` 单独串行，**生成期间其它页签仍可用** | `src/app.py` |
| 7 | 兜底不再交出空简报（正文空则退到标题） | `src/brief.py` |

**验证**：`compileall` rc=0；12/12 模块可导入；`get_backend(cfg,"local")` 两次返回同一实例；
实测「首次 63.5 s（含 12 s 加载）→ 二次 11.6 s（不再加载）」，证明后端复用生效。

#### 8.9.5 两项配套改造（本轮新增）

**(1) 简报结果缓存（界面层）**

`ui_generate` 默认先查 `store.get_brief()`：**同一事件不重复调模型**，直接秒回缓存，
并在正文上方标出「这份简报来自本地缓存」；要新结果就点新加的
「重新生成（忽略缓存）」按钮。两种意图分开表达，而不是二选一。

判定要求缓存里的 `brief` **非空**才采用 —— 空简报不配当缓存命中
（这正是上面那个空兜底缺陷暴露出来的地方）。

**(2) 离线预热正文事实要点：新增 `scripts/warm_summaries.py`**

`warm_translations.py` 预热的是**标题**（80 token，实测 1.0 s/条）；
正文事实要点最多 600 token，代价差一个量级，所以单独一个脚本：

```bash
python scripts/warm_summaries.py --hours 168 --limit 80   # 默认只看最近 7 天
python scripts/warm_summaries.py --dry-run                # 只看清单
python scripts/warm_summaries.py --quiet                  # 每 10 条打一次进度
```

预热过之后，`build_material` 里那条「实时归一化」路径就退化成**纯读库**，
根因第 2 条彻底消失（第 4 条的 `MAX_NORMALIZE_PER_EVENT` 只是没预热时的保险）。

#### 8.9.6 结论与剩余风险

- 一次点击的耗时从 **2~5 分钟**降到 **15~30 秒**（预热后不再有加载等待）。
- **剩余风险**：`device_map="auto"` + 4-bit 在显存紧张时仍可能卸载到 CPU
  （就是 1 token/s 那种情况）。所以**永远不要同时跑两个加载了模型的进程** ——
  翻译预热、截图脚本、Web 服务三者不要在生成期间并行。
  这一条已写进 8.8.7 的截图注意事项里。
"""

assert "### 8.9 " not in src
src = src.rstrip("\n") + "\n" + SEC
open(P, "w", encoding="utf-8", newline="\n").write(src)

chk = open(P, encoding="utf-8").read()
heads = [l for l in chk.splitlines() if re.match(r"^#{2,4} 8\.(8|9)", l)]
print("原 %d 字符 -> 新 %d 字符" % (old, len(chk)))
for h in heads:
    print("  ", h[:76])
assert chk.count("### 8.9 ") == 1 and "285.3" in chk
print("断言通过；总行数 =", len(chk.splitlines()))
