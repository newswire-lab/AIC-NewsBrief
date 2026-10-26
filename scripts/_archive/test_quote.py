# -*- coding: utf-8 -*-
# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
"""验证：带署名的引用句不再被误判，且覆盖率不被框式词拉低。"""
import os, subprocess, sys

ROOT = _ROOT
sys.path.insert(0, ROOT)
os.chdir(ROOT)
rc = subprocess.run([sys.executable, "-m", "compileall", "-q",
                     os.path.join(ROOT, "src")], capture_output=True, text=True)
print("compileall rc =", rc.returncode, rc.stderr[-400:])

from src import brief, verify, store

material = ("[1] 来源：Defense News（机构：美国视线传媒集团，等级 tier2，地区 intl）\n"
            "时间：2026-10-14T01:00:00+00:00\n"
            "标题：Raytheon wins 8B deal for nuclear command terminals\n"
            "正文：雷神公司获得价值80亿美元的合同，为美国空军核指挥终端提供现代化改造。"
            "洛克希德公司高管表示，该合同将带动供应链升级。\n")

plain = "雷神公司获得价值80亿美元的合同，为美国空军核指挥终端提供现代化改造。"
attrib = "据美国国防新闻表示，雷神公司获得价值80亿美元的合同，为美国空军核指挥终端提供现代化改造。"
view = "据洛克希德公司高管表示，该合同将带动供应链升级。"

print("\n== 覆盖率（框式词已剔除）==")
for label, s in (("无署名事实句", plain), ("带署名引用句", attrib), ("专家观点句", view)):
    print("  %s：%.2f" % (label, verify.weighted_coverage(s, material)))
print("  最小要求 coverage >= %.2f" % verify.DEFAULT_MIN_COVERAGE)

print("\n== 推测词表 ==")
print("  仍禁止：", "、".join(verify.SPECULATIVE_WORDS[:8]))
print("  已移出（改为需署名）：", "、".join(verify.ATTRIBUTION_WORDS[:5]))
for w in ("或将", "预计", "分析认为", "有望"):
    print("    「%s」是否仍判为禁止：%s" % (w, w in verify.SPECULATIVE_WORDS))

print("\n== 提示词是否已含署名要求 ==")
sp = brief.SYSTEM_PROMPT + brief._BRIEF_TEMPLATE
for kw in ("署名", "编造机构名", "逻辑承接", "各方反应"):
    print("  含「%s」：%s" % (kw, kw in sp))
