"""校验本地模型权重文件的完整性。

背景：分片下载为了让 16 个线程写入不同偏移，先对目标文件做了 truncate 预分配，
因此**文件大小从一早就等于完整大小**。若下载中断或部分分片失败，
"按大小判断是否完整"的跳过逻辑会被骗过，得到一个大小正确但内容是空洞的文件
—— 表现就是模型加载成功、但输出退化为常量 token。

本脚本用三条独立证据判断文件好坏：
1. ModelScope API 是否提供校验和，提供则直接比对
2. 均匀抽样读若干数据块，统计"整块全零"的比例（空洞检测）
3. 本文件的 SHA256

用法：
    python scripts/check_model_file.py
    python scripts/check_model_file.py --file model.safetensors
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import llm                                            # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_FILES = "https://www.modelscope.cn/api/v1/models/{mid}/repo/files?Revision=master"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
BLOCK = 4096
SAMPLES = 400


def api_file_info(model_id: str, target: str):
    req = urllib.request.Request(API_FILES.format(mid=model_id),
                                headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for item in (data.get("Data") or {}).get("Files") or []:
        if (item.get("Path") or item.get("Name")) == target:
            return item
    return None


def zero_block_scan(path: str, samples: int = SAMPLES) -> tuple:
    size = os.path.getsize(path)
    if size == 0:
        return 0, 0
    step = max(BLOCK, size // samples)
    all_zero = 0
    checked = 0
    with open(path, "rb") as f:
        offset = 0
        while offset + BLOCK <= size and checked < samples:
            f.seek(offset)
            block = f.read(BLOCK)
            if not block:
                break
            checked += 1
            if not any(block):
                all_zero += 1
            offset += step
    return all_zero, checked


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="校验模型权重完整性")
    parser.add_argument("--file", default="model.safetensors")
    parser.add_argument("--no-hash", action="store_true", help="跳过 SHA256 计算")
    args = parser.parse_args()

    cfg = llm.load_llm_config()
    model_id = str(cfg["local"]["model_id"])
    model_dir = os.path.join(PROJECT_ROOT, str(cfg["local"]["model_dir"]))
    path = os.path.join(model_dir, args.file)

    out = []
    out.append("=" * 74)
    out.append("模型权重完整性校验")
    out.append("=" * 74)
    out.append("模型 id : %s" % model_id)
    out.append("文件    : %s" % path)
    if not os.path.exists(path):
        out.append("[ERROR] 文件不存在")
        print("\n".join(out))
        return 1

    local_size = os.path.getsize(path)
    out.append("本地大小: %d 字节 (%.2f MB)" % (local_size, local_size / 1024 ** 2))

    info = api_file_info(model_id, args.file)
    expected = None
    if info:
        expected = int(info.get("Size") or 0)
        out.append("远端大小: %d 字节 (%.2f MB)" % (expected, expected / 1024 ** 2))
        out.append("大小一致: %s" % (local_size == expected))
        hashes = {k: v for k, v in info.items()
                  if isinstance(v, str) and len(v) == 64}
        out.append("远端提供的哈希字段: %s" % (hashes or "（无）"))
    else:
        out.append("远端未找到该文件条目")

    out.append("-" * 74)
    zero, checked = zero_block_scan(path)
    ratio = (zero / checked) if checked else 0.0
    out.append("空洞检测: 抽样 %d 块，其中全零块 %d 个（%.1f%%）"
               % (checked, zero, ratio * 100))
    if ratio > 0.05:
        out.append("  >>> 判定：**文件不完整**，存在大面积未写入的空洞，需要重新下载")
    else:
        out.append("  >>> 判定：未发现明显空洞")

    if not args.no_hash:
        out.append("-" * 74)
        out.append("正在计算 SHA256（%.2f GB，约需十几秒）..." % (local_size / 1024 ** 3))
        digest = sha256_of(path)
        out.append("本地 SHA256: %s" % digest)
        if info:
            remote = {k: v for k, v in info.items()
                      if isinstance(v, str) and len(v) == 64}
            matched = [k for k, v in remote.items() if v.lower() == digest]
            if remote:
                out.append("与远端哈希比对: %s" % ("一致 ✅" if matched else "**不一致 ❌**"))
            else:
                out.append("远端未提供可比对哈希，无法据此判定")

    out.append("=" * 74)
    text = "\n".join(out)
    with io.open(os.path.join(PROJECT_ROOT, "logs", "model_integrity.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
