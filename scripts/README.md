# scripts 目录说明

本目录的脚本分两类：**交付/复现必需的核心脚本**，与**开发期的一次性探针脚本**。
之所以把后者也保留在仓库里，是为了让评审者能够审计「结论是怎么测出来的」——
但**它们不参与作品的正常运行链路**。

> 主流程入口在 `src/cli.py`（`python -m src.cli --help`）与 `src/app.py`。

---

## 一、核心脚本（评审者需要用到）

| 脚本 | 用途 | 需要 GPU | 典型命令 |
|---|---|---|---|
| `fast_download.py` | 从魔搭下载模型权重，多线程分片 + 断点续传 + SHA256 校验；**校验通过才把 `.part` 改名为正式文件** | 否 | `python scripts/fast_download.py --model Qwen/Qwen2.5-3B-Instruct --dir models/Qwen2.5-3B-Instruct` |
| `check_model_file.py` | 校验已下载权重的完整性：按 4KB 块均匀抽样检测「全零空洞」，并与远端 SHA256 比对 | 否 | `python scripts/check_model_file.py --file model-00001-of-00002.safetensors` |
| `compare_models.py` | 对照实验：在**同一批事件**上比较不同开源模型的一次成型率、字数、逐句溯源率与耗时 | 是 | `python scripts/compare_models.py --events 5` |
| `evaluate_similarity.py` | 语义相似度阈值标定：人工标注样例上计算同/不同事件的得分分布，用于确定 0.50 这一阈值 | 否 | `python scripts/evaluate_similarity.py` |
| `check_anonymize.py` | **匿名化自查**：扫描仓库中是否含有姓名/学校/学号等身份信息；退出码非 0 表示存在风险 | 否 | `python scripts/check_anonymize.py` |
| `check_brief_length.py` | **作品简介字数校验**：按三种口径统计并对照 300 字上限；退出码 0 = 合规 | 否 | `python scripts/check_brief_length.py --verbose` |
| `analyze_orgs.py` | 分析「多源印证」里有多少是同一家媒体集团的不同站点，用于修正交叉印证计数 | 否 | `python scripts/analyze_orgs.py` |

**提交前建议按序执行**：

```powershell
python scripts/check_anonymize.py        # 匿名化自查，退出码须为 0
python scripts/check_brief_length.py     # 简介字数，退出码须为 0
```

---

## 二、开发期探针脚本（一次性，不参与运行）

这些脚本是开发过程中为了**摸底实测数据**而写的，结论已固化进
`config/sources.yaml` 的注释与 `docs/技术报告.md`，保留仅供审计复现。

| 脚本 | 当时要回答的问题 |
|---|---|
| `probe_feeds.py` | 哪些新闻源的 RSS 真实可用？（探测后把不可用的源标注 `enabled: false` 并写明原因） |
| `probe_domains.py` | 各候选站点在网络环境下是否可达 |
| `probe_model_sources.py` | 模型托管平台（魔搭 / hf-mirror / 各 API）哪些可达 |
| `probe_html.py` / `check_html.py` / `debug_html.py` | 国防部等只有栏目页的站点，HTML 列表页能否抽出条目 |
| `dump_context.py` | 抽取失败时把页面上下文打印出来定位原因 |
| `debug_llm.py` | 本地模型加载与生成的最小可复现调试入口 |
| `diagnose_model.py` | 权重是否损坏的诊断（**空洞检测**的早期版本；现在优先用 `check_model_file.py`） |

> 权重目录默认取 `config/llm.yaml` 的配置，可用环境变量
> `NEWSBRIEF_MODEL_DIR` 临时覆盖。

---

## 三、为什么不把探针脚本删掉

技术报告里的几处关键结论——「6 家站点其实同属一家媒体集团」「语义阈值取 0.50」
「19.2% 的文件块为零说明权重下载有空洞」——都来自这些脚本。
保留它们，评审者可以自行复跑核对，而不是只能相信报告里的数字。
