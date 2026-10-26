"""魔搭 ModelScope 模型权重下载器：**完整性校验 + 断点续传**。

**为什么重写这一版**

上一版为了 16 个线程写不同偏移，先对目标文件 `truncate` 预分配到完整大小。
结果是：**文件大小从下载一开始就等于完整大小**。一旦下载被中断，
"按文件大小判断是否完整"的跳过逻辑会被欺骗，得到一个大小正确、
但大部分区间是空洞的权重文件 —— 表现为模型能加载、却只输出常量 token。

实测该故障确实发生过：`model.safetensors` 大小与远端完全一致，
但抽样 400 个 4KB 块中有 **19.2% 全零**。

本版的四道防线：

1. **按内容校验而非按大小**：ModelScope 的文件清单 API 提供 `Sha256`，
   大于阈值的文件必须哈希一致才算完整；
2. **下载到 `.part` 临时文件**，校验通过后才改名 —— 正式文件名一旦存在即为可信；
3. **分片进度落盘**（`.chunks.json`），中断后只补缺失分片；
4. 校验失败时报错并保留现场，绝不静默接受。

用法：
    python scripts/fast_download.py               # 下载 config/llm.yaml 配置的模型
    python scripts/fast_download.py --verify      # 只校验已有文件，不下载
    python scripts/fast_download.py --workers 8
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import llm                                            # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_FILES = "https://www.modelscope.cn/api/v1/models/{mid}/repo/files?Revision=master"
API_BLOB = ("https://www.modelscope.cn/api/v1/models/{mid}/repo"
            "?Revision=master&FilePath={path}")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

CHUNK = 8 * 1024 * 1024          # 分片大小
HASH_MIN_SIZE = 32 * 1024 * 1024  # 超过该大小才做哈希校验（小文件按大小即可）
_print_lock = threading.Lock()


def http_open(url: str, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def list_files(model_id: str):
    """返回 [(path, size, sha256), ...]。"""
    with http_open(API_FILES.format(mid=model_id), timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for item in (data.get("Data") or {}).get("Files") or []:
        if item.get("Type") != "blob":
            continue
        out.append((
            item.get("Path") or item.get("Name") or "",
            int(item.get("Size") or 0),
            (item.get("Sha256") or "").lower(),
        ))
    return out


def sha256_of(path: str, progress: bool = False) -> str:
    h = hashlib.sha256()
    total = os.path.getsize(path)
    read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if progress and total:
                with _print_lock:
                    sys.stdout.write("\r  校验中 %5.1f%%" % (100.0 * read / total))
                    sys.stdout.flush()
    if progress:
        sys.stdout.write("\r" + " " * 24 + "\r")
    return h.hexdigest()


def is_complete(dest: str, size: int, remote_sha: str) -> bool:
    """文件是否已完整且可信。"""
    if not os.path.exists(dest) or os.path.getsize(dest) != size:
        return False
    if not remote_sha or size < HASH_MIN_SIZE:
        return True
    return sha256_of(dest) == remote_sha


def _load_map(map_path: str) -> set:
    if not os.path.exists(map_path):
        return set()
    try:
        with open(map_path, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()


def _save_map(map_path: str, done: set) -> None:
    tmp = map_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)
    os.replace(tmp, map_path)


def _fetch_range(url: str, start: int, end: int, retries: int = 5) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            with http_open(url, {"User-Agent": UA, "Range": f"bytes={start}-{end}"},
                           timeout=120) as resp:
                return resp.read()
        except Exception as exc:
            last = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"分片 {start}-{end} 下载失败: {last}")


def download_file(model_id: str, path: str, size: int, remote_sha: str,
                  dest: str, workers: int) -> bool:
    if is_complete(dest, size, remote_sha):
        print("  [跳过] %-28s 已完整且校验通过" % path)
        return True

    url = API_BLOB.format(mid=model_id, path=path)
    part = dest + ".part"
    map_path = dest + ".chunks.json"
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # 小文件：单次下载，直接校验
    if size <= HASH_MIN_SIZE:
        t0 = time.time()
        data = _fetch_range(url, 0, max(0, size - 1))
        if len(data) != size:
            raise RuntimeError(f"长度不符: 期望 {size}，实际 {len(data)}")
        digest = hashlib.sha256(data).hexdigest()
        if remote_sha and digest != remote_sha:
            raise RuntimeError(f"SHA256 不符（期望 {remote_sha[:12]}…，实际 {digest[:12]}…）")
        with open(dest, "wb") as f:
            f.write(data)
        print("  [OK] %-28s %8.2f MB  单线程 %.1fs" % (path, size / 1024 ** 2,
                                                      time.time() - t0))
        return True

    # 大文件：分片 + 续传 + 哈希校验
    offset_map = [(s, min(s + CHUNK - 1, size - 1)) for s in range(0, size, CHUNK)]
    starts = {s for s, _ in offset_map}
    done = _load_map(map_path) & starts
    if not os.path.exists(part) or os.path.getsize(part) != size:
        with open(part, "wb") as f:
            f.truncate(size)
        done = set()

    todo = [(s, e) for s, e in offset_map if s not in done]
    total_chunks = len(offset_map)
    finished = len(done)
    if done:
        print("  [续传] %-28s 已有 %d/%d 分片" % (path, finished, total_chunks))

    t0 = time.time()
    errors = []
    lock = threading.Lock()

    def worker(item):
        s, e = item
        blob = _fetch_range(url, s, e)
        if len(blob) != e - s + 1:
            raise RuntimeError(f"分片 {s} 长度不符")
        with lock:
            with open(part, "r+b") as fh:
                fh.seek(s)
                fh.write(blob)
            done.add(s)
        return len(blob)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker, item): item for item in todo}
        for fut in cf.as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                errors.append(str(exc))
            finished = len(done)
            with _print_lock:
                sys.stdout.write(
                    "\r  [..] %-28s %5.1f%%  %6.2f/%.2f GB"
                    % (path, 100.0 * finished / total_chunks,
                       finished * CHUNK / 1024 ** 3, size / 1024 ** 3))
                sys.stdout.flush()
            if finished % 20 == 0:
                _save_map(map_path, done)
    _save_map(map_path, done)

    if errors:
        print("\r  [FAIL] %-28s %d 个分片失败：%s" % (path, len(errors), errors[0][:80]))
        return False

    print("\r  校验 %-28s ..." % path, end="")
    digest = sha256_of(part, progress=True)
    if remote_sha and digest != remote_sha:
        print("  [FAIL] %-28s SHA256 不符，保留 .part 供排查" % path)
        print("         期望 %s\n         实际 %s" % (remote_sha, digest))
        return False

    os.replace(part, dest)
    try:
        os.remove(map_path)
    except OSError:
        pass
    print("\r  [OK] %-28s %8.2f MB  分片 %d 并发 %d  耗时 %.1f 分钟 (%s)      "
          % (path, size / 1024 ** 2, total_chunks, workers,
             (time.time() - t0) / 60, digest[:12]))
    return True


def verify_all(model_id: str, local_dir: str, files) -> int:
    print("=" * 74)
    print("校验已有文件")
    print("=" * 74)
    bad = 0
    for path, size, remote_sha in sorted(files, key=lambda x: x[1]):
        dest = os.path.join(local_dir, path)
        if not os.path.exists(dest):
            print("  [缺失] %s" % path)
            bad += 1
            continue
        if not remote_sha or size < HASH_MIN_SIZE:
            ok = os.path.getsize(dest) == size
            print("  [%s] %-28s 按大小判定" % ("OK" if ok else "BAD", path))
        else:
            print("  校验 %-28s ..." % path, end="")
            ok = sha256_of(dest) == remote_sha
            print("\r  [%s] %-28s SHA256 %s" % ("OK" if ok else "BAD", path,
                                                "一致" if ok else "**不一致**"))
        if not ok:
            bad += 1
    print("-" * 74)
    print("结果: %d 个文件有问题" % bad)
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="魔搭模型下载（校验+续传）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--dir", default=None)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--verify", action="store_true", help="只校验，不下载")
    args = parser.parse_args()

    cfg = llm.load_llm_config()
    model_id = args.model or str(cfg["local"]["model_id"])
    rel_dir = args.dir or str(cfg["local"]["model_dir"])
    local_dir = os.path.join(PROJECT_ROOT, rel_dir)

    print("=" * 74)
    print("模型权重下载（完整性校验 + 断点续传）")
    print("=" * 74)
    print("模型   : %s" % model_id)
    print("目录   : %s" % local_dir)

    files = list_files(model_id)
    total = sum(s for _, s, _ in files)
    print("文件   : %d 个，合计 %.2f GB" % (len(files), total / 1024 ** 3))
    has_sha = sum(1 for _, _, h in files if h)
    print("哈希   : %d/%d 个文件提供 Sha256" % (has_sha, len(files)))
    print("-" * 74)

    if args.verify:
        return verify_all(model_id, local_dir, files)

    t0 = time.time()
    failed = []
    for path, size, remote_sha in sorted(files, key=lambda x: x[1]):
        dest = os.path.join(local_dir, path)
        try:
            if not download_file(model_id, path, size, remote_sha, dest, args.workers):
                failed.append(path)
        except Exception as exc:
            failed.append(path)
            print("  [FAIL] %s -> %s: %s" % (path, type(exc).__name__, str(exc)[:70]))

    print("-" * 74)
    bad = verify_all(model_id, local_dir, files)
    print("总耗时 %.1f 分钟" % ((time.time() - t0) / 60))
    if failed or bad:
        print("仍有问题的文件: %s" % ", ".join(failed) if failed else "见上方校验结果")
        return 1
    print("全部文件已下载并通过完整性校验 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
