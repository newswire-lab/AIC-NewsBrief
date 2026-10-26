"""从魔搭 ModelScope 下载开源模型权重到本地 models/ 目录。

用法：
    python scripts/download_model.py                  # 按 config/llm.yaml 的 local 配置下载
    python scripts/download_model.py --list           # 只查看文件清单与体积，不下载
    python scripts/download_model.py --model Qwen/Qwen3-1.7B --dir models/Qwen3-1.7B

为什么走魔搭：实测 huggingface.co 在本机不可达，而 ModelScope 完全可用
（2026-09-24 实测：官网 200、模型信息 API 200、文件清单 API 200）。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import llm                                            # noqa: E402


def list_files(model_id: str) -> None:
    """用 ModelScope 的 API 查看文件清单与总体积。"""
    import json
    import urllib.request

    url = f"https://www.modelscope.cn/api/v1/models/{model_id}/repo/files?Revision=master"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("查询失败: %s: %s" % (type(exc).__name__, exc))
        return

    files = (data.get("Data") or {}).get("Files") or []
    total = 0
    rows = []
    for item in files:
        if item.get("Type") != "blob":
            continue
        name = item.get("Path") or item.get("Name") or ""
        size = int(item.get("Size") or 0)
        total += size
        rows.append((size, name))
    rows.sort(reverse=True)
    print("模型 %s 共 %d 个文件，合计 %.2f GB" % (model_id, len(rows), total / 1024 ** 3))
    for size, name in rows[:15]:
        print("  %9.2f MB  %s" % (size / 1024 ** 2, name))


def resolve_snapshot_download():
    """兼容不同版本 modelscope 的导入路径。"""
    candidates = [
        ("modelscope", "snapshot_download"),
        ("modelscope.hub.snapshot_download", "snapshot_download"),
        ("modelscope.hub.api", "HubApi"),
    ]
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
            func = getattr(module, attr, None)
            if func is not None:
                return func
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="下载开源模型权重（魔搭 ModelScope）")
    parser.add_argument("--model", default=None, help="魔搭模型 id，默认取 config/llm.yaml")
    parser.add_argument("--dir", default=None, help="本地保存目录，默认取 config/llm.yaml")
    parser.add_argument("--list", action="store_true", help="只查看文件清单，不下载")
    args = parser.parse_args()

    cfg = llm.load_llm_config()
    model_id = args.model or str(cfg["local"]["model_id"])
    rel_dir = args.dir or str(cfg["local"]["model_dir"])
    local_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel_dir)

    print("=" * 74)
    print("开源模型权重下载")
    print("=" * 74)
    print("模型 id   : %s" % model_id)
    print("本地目录  : %s" % local_dir)

    list_files(model_id)
    if args.list:
        return 0

    os.makedirs(local_dir, exist_ok=True)
    snapshot_download = resolve_snapshot_download()
    if snapshot_download is None:
        print("[ERROR] 未找到 modelscope 的 snapshot_download，请先安装 modelscope")
        return 1

    print("-" * 74)
    print("开始下载（首次下载约数十秒到数分钟）...")
    try:
        path = snapshot_download(model_id, local_dir=local_dir)
    except TypeError:
        # 老版本参数名不同
        path = snapshot_download(model_id, cache_dir=local_dir)
    except Exception as exc:
        print("[ERROR] 下载失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    print("-" * 74)
    print("下载完成: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
