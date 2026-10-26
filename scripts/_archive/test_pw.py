# 归档说明（2026-10-15）：本文件原为桌面上的**一次性探查脚本**，已归档保留以备复核。
# 原硬编码的本机绝对路径已改为由 __file__ 反推（本文件位于 scripts/_archive/，
# 故 parents[2] 即项目根）；原输出到桌面的文件改写到 logs/_archive_out/，
# 避免再次污染桌面。路径重写后未必每条都能原样复跑，仅供查证当时的实测依据。
import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_DESKTOP = _os.path.join(_ROOT, "logs", "_archive_out")
_os.makedirs(_DESKTOP, exist_ok=True)
import sys, time

t0 = time.time()
try:
    from playwright.sync_api import sync_playwright
    print("import ok  %.1fs" % (time.time() - t0))
except Exception as exc:
    print("import FAIL", type(exc).__name__, exc)
    raise SystemExit(1)

pw = sync_playwright().start()
print("start ok  %.1fs" % (time.time() - t0))

for kw in ({"channel": "msedge", "headless": True},
           {"channel": "msedge", "headless": True, "args": ["--no-sandbox", "--disable-gpu"]},
           {"executable_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "headless": True}):
    try:
        b = b_ = None
        b = pw.chromium.launch(**kw)
        print("launch OK", kw, "version=", b.version, "%.1fs" % (time.time() - t0))
        pg = b.new_page(viewport={"width": 1600, "height": 1150})
        pg.goto("http://127.0.0.1:7860/?__theme=light", wait_until="load", timeout=45000)
        pg.wait_for_timeout(9000)
        w = pg.evaluate("() => window.innerWidth")
        print("  innerWidth =", w)
        box = pg.locator("main").first.bounding_box()
        print("  main box =", box)
        b.close()
        break
    except Exception as exc:
        print("launch FAIL", kw, type(exc).__name__, str(exc)[:300])
        break
    finally:
        try:
            if b:
                b.close()
        except Exception:
            pass

pw.stop()
print("done %.1fs" % (time.time() - t0))
