"""探测开源模型/数据/工具链的可达性。

用途：AI+开源 赛道要求"开源资源对核心功能起关键作用"，因此必须确认
本机能真正获取开源模型（权重或推理服务）。huggingface 已知不可达，
需要确认国内替代通道是否可用。
"""

from __future__ import annotations

import concurrent.futures as cf
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import fetch as fetchmod                              # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TARGETS = [
    # 模型托管 / 镜像
    ("魔搭 ModelScope", "https://www.modelscope.cn/"),
    ("魔搭 API 模型信息", "https://www.modelscope.cn/api/v1/models/Qwen/Qwen2.5-3B-Instruct"),
    ("魔搭 文件清单", "https://www.modelscope.cn/api/v1/models/Qwen/Qwen2.5-3B-Instruct/repo/files"),
    ("hf-mirror", "https://hf-mirror.com/"),
    ("HF 官方(已知不通)", "https://huggingface.co/"),
    ("OpenXLab", "https://www.openxlab.org.cn/"),
    ("ModelScope 数据集", "https://www.modelscope.cn/api/v1/datasets"),
    # 推理服务（开源模型 API）
    ("硅基流动", "https://api.siliconflow.cn/v1/models"),
    ("阿里百炼 DashScope", "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"),
    ("智谱开放平台", "https://open.bigmodel.cn/api/paas/v4/models"),
    ("DeepSeek 平台", "https://api.deepseek.com/v1/models"),
    # 工具链
    ("ollama 官网", "https://ollama.com/"),
    ("GitHub Releases", "https://github.com/ollama/ollama/releases"),
    ("GitHub API", "https://api.github.com/rate_limit"),
    ("PyPI 清华镜像", "https://pypi.tuna.tsinghua.edu.cn/simple/pypdf/"),
    ("PyPI 阿里镜像", "https://mirrors.aliyun.com/pypi/simple/pypdf/"),
    ("Gitee", "https://gitee.com/"),
    ("百度网盘(提交用)", "https://pan.baidu.com/"),
]


def probe(item):
    name, url = item
    try:
        res = fetchmod.fetch(url, session=None, timeout=8, retries=0)
    except Exception as exc:
        return "EXC  %-24s %s" % (name, str(exc)[:44])
    if res.ok:
        return "OK   %-24s %-5s %7dB  %s" % (name, res.status, len(res.content), url[:46])
    return "FAIL %-24s %-5s %s" % (name, str(res.status or "-"), (res.error or "")[:44])


def main() -> int:
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        lines = list(ex.map(probe, TARGETS))
    body = "\n".join(lines)
    print(body)
    with io.open(os.path.join(PROJECT_ROOT, "logs", "model_sources_probe.txt"),
                 "w", encoding="utf-8") as f:
        f.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
