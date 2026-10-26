# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
import os, re, sys

root = _ROOT
out = []
for fn in ("normalize.py", "store.py", "cluster.py"):
    p = os.path.join(root, "src", fn)
    out.append("===== " + fn + " =====")
    for i, ln in enumerate(open(p, encoding="utf-8").read().splitlines(), 1):
        if re.match(r"^(def |class |[A-Z][A-Z_0-9]{2,} *=|    def )", ln):
            out.append("%4d: %s" % (i, ln[:110].encode("ascii", "replace").decode("ascii")))
    out.append("")

open(_os.path.join(_DESKTOP, "_api_dump.txt"), "w", encoding="utf-8").write("\n".join(out))
print("done, lines:", len(out))
