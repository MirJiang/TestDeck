"""UI 用例执行引擎（Playwright，无头浏览器）。

步骤结构与 API 用例同一张表，type=ui 时字段含义：
  action: goto | click | fill | expect_text | screenshot
  url / selector / value
check 用 contains（页面应包含文字）复用现有展示。
"""
import os
import time
from pathlib import Path

from .browser import launch_browser

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

ACTIONS = {"goto", "click", "fill", "expect_text", "screenshot"}


def _sync_run(case, env, run_id: str, engine: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright
    base = (env.base_url or "").rstrip("/") if env else ""
    t0 = time.time()
    detail, pass_n, fail_n = [], 0, 0

    with sync_playwright() as p:
        try:
            browser, used, _note = launch_browser(p, engine)
        except RuntimeError as e:  # 引擎与回退都不可用：给出部署指引而不是 500
            return {"status": "failed", "pass_n": 0, "fail_n": 1, "duration": 0.0,
                    "detail": [{"idx": 1, "action": "error", "target": "", "pass": False,
                                "reason": str(e)[:300], "ms": 0}]}
        page = browser.new_page()
        for i, step in enumerate(case.steps or [], 1):
            action = step.get("action", "goto")
            url, sel, val = step.get("url", ""), step.get("selector", ""), step.get("value", "")
            if url.startswith("/") and base:
                url = base + url
            res = {"idx": i, "action": action, "target": url or sel, "pass": False, "reason": "", "ms": 0}
            st = time.time()
            try:
                if action == "goto":
                    page.goto(url, timeout=15000)
                    res.update(**{"pass": True, "reason": f"打开 {url}"})
                elif action == "click":
                    page.click(sel, timeout=8000)
                    res.update(**{"pass": True, "reason": f"点击 {sel}"})
                elif action == "fill":
                    page.fill(sel, val, timeout=8000)
                    res.update(**{"pass": True, "reason": f"在 {sel} 输入 {val}"})
                elif action == "expect_text":
                    page.wait_for_load_state("domcontentloaded")
                    deadline = time.time() + 5
                    body = page.inner_text("body", timeout=8000)
                    while val not in body and time.time() < deadline:
                        time.sleep(0.2)
                        body = page.inner_text("body", timeout=8000)
                    ok = val in body
                    res.update(**{"pass": ok, "reason": f"页面未包含「{val}」（应显示）" if not ok else f"包含「{val}」"})
                elif action == "screenshot":
                    path = STATIC_DIR / f"{run_id}-{i}.png"
                    page.screenshot(path=str(path), full_page=True)
                    res.update(**{"pass": True, "reason": "已截图", "screenshot": f"/static/{path.name}"})
                else:
                    res["reason"] = f"不支持的动作：{action}"
            except Exception as e:
                res["reason"] = f"{type(e).__name__}: {e}"[:200]
            res["ms"] = int((time.time() - st) * 1000)
            detail.append(res)
            if res["pass"]:
                pass_n += 1
            else:
                fail_n += 1
                break  # UI 步骤失败即中止（页面状态已不可靠）
        browser.close()

    return {"status": "failed" if fail_n else "passed", "pass_n": pass_n, "fail_n": fail_n,
            "duration": round(time.time() - t0, 2), "detail": detail}


def run_ui_case(case, env, run_id: str) -> dict:
    """同步执行 UI 用例（由执行队列在专用线程调用）。"""
    return _sync_run(case, env, run_id)
