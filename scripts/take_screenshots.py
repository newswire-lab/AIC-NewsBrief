"""用 Playwright 驱动本机 Edge 给界面拍图（自动化、可复现）。

为什么需要这个脚本
------------------
界面截图要进技术报告与 PDF，需要**统一宽度、统一主题、内容裁剪**。
集成浏览器里那张页面视口是固定的（实测 ``window.innerWidth=492``），
``setViewportSize`` 对它无效，所以拍不出宽图 —— 于是改成独立启动一个浏览器实例。

不下载 Chromium，直接用系统已装的 Edge（``channel="msedge"``），
省掉 ~130 MB 下载，也和 ``scripts/make_pdf.py`` 走同一套浏览器。

用法（先起好服务：``python -m src.app --port 7860``）：
    python scripts/take_screenshots.py                    # 全部 5 张
    python scripts/take_screenshots.py --skip-brief       # 跳过要跑模型的「简报生成」
    python scripts/take_screenshots.py --width 1360 --theme dark
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs" / "screenshots"


def shoot(page, out_dir: Path, name: str, waits: float = 2.0,
          full: bool = False) -> str:
    """截图。

    **故意截页面而不是截元素**：``locator.screenshot()`` 会等元素"稳定"，
    而本页面有 60 秒自动刷新定时器、DOM 一直在变，实测每张图要多等十几秒
    （甚至看起来像卡死）。整页截图没有稳定性检查，快得多。
    代价是图里会带页面头部，但对成果展示来说这反而是好事（能看到界面全貌）。
    """
    page.wait_for_timeout(int(waits * 1000))
    path = out_dir / name
    page.screenshot(path=str(path), animations="disabled",
                    full_page=full, timeout=45000)
    kb = path.stat().st_size / 1024.0
    return f"{name}  {kb:6.1f} KB  {'整页' if full else '视口'}"


def open_tab(page, name: str, waits: float = 2.5) -> None:
    print(f"      -> 切到「{name}」", flush=True)
    page.get_by_role("tab", name=name).dispatch_event("click")
    page.wait_for_timeout(int(waits * 1000))


def click_button(panel, name: str) -> bool:
    """点页签内的按钮。

    用 ``dispatch_event("click")`` 而不是 ``click()``：集成浏览器/无头窗口下
    ``click()`` 会因“元素不在视口内”或“元素不稳定”而超时（实测踩到），
    而直接派发 DOM 事件不管这些，对 Gradio 同样生效。
    """
    try:
        panel.get_by_role("button", name=name).first.dispatch_event("click")
        return True
    except Exception as exc:
        print(f"[WARN] 未点到「{name}」：{type(exc).__name__}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="给界面拍图")
    ap.add_argument("--url", default="http://127.0.0.1:7860/")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1150)
    ap.add_argument("--theme", default="light", choices=["light", "dark"])
    ap.add_argument("--search-term", default="无人机")
    ap.add_argument("--brief-wait", type=int, default=240,
                    help="等简报生成的最长秒数")
    ap.add_argument("--skip-brief", action="store_true",
                    help="跳过「简报生成」页（要调用模型，耗时较长）")
    ap.add_argument("--only-brief", action="store_true",
                    help="只拍「简报生成」那一张（补拍用）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] 未安装 playwright：python -m pip install playwright")
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    url = args.url.rstrip("/") + f"/?__theme={args.theme}"
    done: list = []

    with sync_playwright() as pw:
        print("[1/4] 启动 Edge…", flush=True)
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:
            print(f"[ERROR] 启动 Edge 失败：{type(exc).__name__}: {exc}")
            print("        可试 --channel chromium（需先 playwright install chromium）")
            return 2
        print("[2/4] Edge 已启动，打开页面…", flush=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        try:
            page.goto(url, wait_until="load", timeout=60000)
            print("[3/4] 页面已加载，等待表格…", flush=True)
            # 等首屏那张表真的渲染出来：Dataframe 是 div 结构，不能等 <tbody>
            page.wait_for_timeout(9000)
            try:
                page.wait_for_selector("text=事件关键词", timeout=45000)
                print("[4/4] 表头已就绪，开始拍图", flush=True)
            except Exception:
                print("[WARN] 没等到「事件关键词」表头，继续拍（可能是空库）", flush=True)

            # 1) 热点事件
            if not args.only_brief:
                open_tab(page, "热点事件", 4.0)
                done.append(shoot(page, out_dir, "1_hot_events.png"))

            # 5) 搜索 —— 用逐字输入！locator.fill() 不会更新 Gradio 的组件状态，
            #    回调会收到空字符串，表现为"点了没反应"（实测踩过）
            if not args.only_brief:
                open_tab(page, "搜索", 2.0)
                box = page.get_by_role("textbox", name="关键词").first
                box.click()
                box.press_sequentially(args.search_term, delay=120)
                box.press("Enter")
                page.wait_for_timeout(7000)
                done.append(shoot(page, out_dir, "5_search.png", 1.0))

            # 2) 证据链
            if not args.only_brief:
                open_tab(page, "证据链", 2.5)
                panel = page.locator('div[role="tabpanel"]:visible').first
                click_button(panel, "重新加载事件列表")
                page.wait_for_timeout(7000)
                done.append(shoot(page, out_dir, "2_evidence.png", 1.0))

            # 4) 系统状态
            if not args.only_brief:
                open_tab(page, "系统状态", 3.0)
                done.append(shoot(page, out_dir, "4_status.png", 1.0, full=True))

            # 3) 简报生成（要真跑一次模型）
            if not args.skip_brief:
                open_tab(page, "简报生成", 3.0)
                panel = page.locator('div[role="tabpanel"]:visible').first
                click_button(panel, "重新加载事件列表")
                page.wait_for_timeout(5000)
                click_button(panel, "生成简报")

                t0 = time.time()
                ok = False
                while time.time() - t0 < args.brief_wait:
                    page.wait_for_timeout(5000)
                    txt = panel.inner_text()
                    # 判定要够具体：页签的**静态说明**里也写着「事实一致性校验报告」，
                    # 用它当判据会 1 秒就"通过"（实测踩过，生成其实才刚开始加载模型）。
                    # 「带依据句」只出现在生成结果里。
                    if "带依据句" in txt or "生成失败" in txt:
                        ok = True
                        break
                    spent = int(time.time() - t0)
                    if spent % 20 < 5:
                        print(f"      等待简报生成… {spent}s", flush=True)
                print(f"     简报生成{'完成' if ok else '超时'}，等待 {time.time() - t0:.0f}s", flush=True)
                done.append(shoot(page, out_dir, "3_brief.png", 2.0))
        finally:
            browser.close()

    print("=" * 70)
    for line in done:
        print("  " + line)
    print("=" * 70)
    print(f"输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
