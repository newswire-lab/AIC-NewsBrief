#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验答辩视频是否符合国赛规则（时长 / 格式 / 音轨 / 体积）。

为什么要单独校验
----------------
国赛答辩视频要求「无快进、无剪辑、一镜到底」，也就是说录完就没有补救机会：
时长超了、没录到声音、体积超限，都可能直接导致无效或扣分。上传前跑一遍这个脚本，
比人工盯着看半天更可靠。

判定口径（取两套要求的交集，宁严勿松）：
- 初赛《作品材料规范》：MP4，3~5 分钟，≤300MB
- 国赛《半决赛比赛流程》：汇报 + 演示 ≤ 6 分钟
  → 因此 3:00 ≤ 时长 ≤ 5:00 为 PASS，5:00 < 时长 ≤ 6:00 为 WARN（国赛合规但初赛口径超标）

用法：
    python scripts/check_defense_video.py "D:\\path\\to\\video.mp4"

退出码：0 全部通过（含 WARN）；1 存在 FAIL。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

MAX_MB = 300.0
MIN_SEC, PREF_MAX_SEC, HARD_MAX_SEC = 180.0, 300.0, 360.0


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def probe(ffmpeg: str, path: str) -> str:
    """ffmpeg -i 会把元数据打到 stderr，且因为没有输出文件而返回非 0，属正常。"""
    cmd = [ffmpeg, "-hide_banner", "-i", path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=120)
    return proc.stdout.decode("utf-8", "replace")


def parse(text: str) -> dict:
    info: dict = {"duration": None, "width": None, "height": None,
                  "fps": None, "vcodec": None, "acodec": None,
                  "bitrate_kbps": None, "audio_channels": None}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", text)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info["duration"] = h * 3600 + mi * 60 + s

    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        info["bitrate_kbps"] = int(m.group(1))

    mv = re.search(r"Stream #\d+:\d+.*?: Video:\s*(\w+)", text)
    if mv:
        info["vcodec"] = mv.group(1)
    m = re.search(r"(\d{2,5})x(\d{2,5})", text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"([\d.]+)\s*fps", text)
    if m:
        info["fps"] = float(m.group(1))

    ma = re.search(r"Stream #\d+:\d+.*?: Audio:\s*(\w+)(.*)", text)
    if ma:
        info["acodec"] = ma.group(1)
        mc = re.search(r"stereo|mono|(\d+)\s*channels", ma.group(2))
        if mc:
            info["audio_channels"] = mc.group(0)

    return info


def mmss(sec: float) -> str:
    return "%d:%02d" % (int(sec) // 60, int(sec) % 60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="答辩视频路径（.mp4）")
    a = ap.parse_args()

    p = a.video
    if not os.path.exists(p):
        print("[FAIL] 文件不存在: %s" % p)
        return 1

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[FAIL] 找不到 ffmpeg。请先安装 imageio-ffmpeg 或把 ffmpeg 加入 PATH")
        return 1

    size_mb = os.path.getsize(p) / 1024 / 1024
    info = parse(probe(ffmpeg, p))
    ext = os.path.splitext(p)[1].lower()

    print("=" * 62)
    print("答辩视频校验：%s" % os.path.basename(p))
    print("=" * 62)

    fails: list[str] = []
    warns: list[str] = []

    # --- 格式 ---
    if ext == ".mp4":
        print("[PASS] 容器格式      MP4")
    elif ext in (".mov", ".mkv", ".avi", ".wmv"):
        print("[FAIL] 容器格式      %s  -> 必须转成 MP4" % ext.upper())
        fails.append("容器格式")
    else:
        print("[WARN] 容器格式      %s（未识别的扩展名）" % ext)

    # --- 时长 ---
    d = info["duration"]
    if d is None:
        print("[FAIL] 时长          解析失败，请人工确认")
        fails.append("时长")
    else:
        line = "时长          %s（%d 秒）" % (mmss(d), int(d))
        if d < MIN_SEC:
            print("[FAIL] %s  -> 不足 3 分钟（初赛口径 3~5 分钟）" % line)
            fails.append("时长过短")
        elif d <= PREF_MAX_SEC:
            print("[PASS] %s  -> 3~5 分钟，两套口径均合规" % line)
        elif d <= HARD_MAX_SEC:
            print("[WARN] %s  -> 国赛 ≤6 分钟合规，但超过初赛 5 分钟口径" % line)
            warns.append("时长略长")
        else:
            print("[FAIL] %s  -> 超过 6 分钟，违反国赛要求" % line)
            fails.append("时长超限")

    # --- 音轨 ---
    if info["acodec"]:
        print("[PASS] 音轨          %s %s" % (info["acodec"],
                                             info["audio_channels"] or ""))
    else:
        print("[FAIL] 音轨          未检测到音频流 -> 无声音等于无效")
        fails.append("无音轨")

    # --- 画质 ---
    if info["width"]:
        res = "%dx%d" % (info["width"], info["height"])
        fps = "%.0f fps" % info["fps"] if info["fps"] else "fps 未知"
        print("[INFO] 分辨率        %s @ %s" % (res, fps))
        if min(info["width"], info["height"]) < 720:
            print("[WARN] 分辨率偏低    低于 720p，评委可能看不清 PPT 细节")
            warns.append("分辨率偏低")
    else:
        print("[WARN] 分辨率        解析失败")

    # --- 体积 ---
    line = "体积          %.1f MB" % size_mb
    if size_mb <= MAX_MB:
        print("[PASS] %s  -> 未超过 300MB" % line)
    else:
        print("[FAIL] %s  -> 超过 300MB，需压缩后再传" % line)
        fails.append("体积超限")

    if info["bitrate_kbps"]:
        print("[INFO] 总码率        %d kb/s" % info["bitrate_kbps"])

    print("-" * 62)
    print("时长上限 6:00 ｜ 体积上限 300MB ｜ 必须有音轨 ｜ 不得剪辑")
    if fails:
        print("结论：不通过 -> %s" % "、".join(fails))
        print("      先修掉上面标 [FAIL] 的项，再上传。")
        return 1
    if warns:
        print("结论：通过（有 %d 项提示 -> %s）" % (len(warns), "、".join(warns)))
    else:
        print("结论：全部通过，可以上传到【视频类文件】栏目。")
    print("提醒：脚本查不出「是否剪辑」和「画面里有没有校名」，这两项仍需人工回看。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
