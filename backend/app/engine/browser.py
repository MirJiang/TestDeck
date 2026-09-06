"""浏览器引擎选择：Chromium Headless Shell（默认）或 Lightpanda（AI 原生轻量引擎，beta）。

面向低配服务器部署：Lightpanda 内存占用约为 Chromium 的 1/9，且无需安装 Playwright 浏览器。
接入方式（按优先级）：
  1. 已有 Lightpanda 服务：连 TD_LIGHTPANDA_URL（默认 http://127.0.0.1:9222）即可
  2. 自动拉起：设 TD_LIGHTPANDA_BIN 指向 lightpanda 可执行文件，平台按需启动并复用进程
  3. 回退：以上都不可用时自动回退 Chromium（需 playwright install chromium --only-shell）

配置（写入 .env）：
  TD_BROWSER_ENGINE   chromium（默认）| lightpanda   全局默认引擎
  TD_LIGHTPANDA_URL   Lightpanda 的 CDP 地址
  TD_LIGHTPANDA_BIN   lightpanda 可执行文件路径（平台自动拉起）
"""
import atexit
import subprocess
import threading
import time
from urllib.parse import urlparse

import httpx

from .. import config

_lp_proc = None
_lp_lock = threading.Lock()


def browser_engine() -> str:
    return (config.get("TD_BROWSER_ENGINE") or "chromium").strip().lower()


def lightpanda_url() -> str:
    return (config.get("TD_LIGHTPANDA_URL") or "http://127.0.0.1:9222").strip()


def _cdp_alive(url: str) -> bool:
    try:
        r = httpx.get(f"{url.rstrip('/')}/json/version", timeout=2, trust_env=False)
        return r.status_code == 200
    except Exception:
        return False


def _spawn_lightpanda(url: str) -> None:
    """TD_LIGHTPANDA_BIN 指向可执行文件时自动拉起 lightpanda serve（进程常驻，多次执行复用）。"""
    global _lp_proc
    with _lp_lock:
        if _lp_proc and _lp_proc.poll() is None:
            return
        bin_path = config.get("TD_LIGHTPANDA_BIN").strip()
        if not bin_path:
            raise RuntimeError(
                "Lightpanda 未就绪：请启动其服务（./lightpanda serve --host 127.0.0.1 --port 9222），"
                "或在 .env 配置 TD_LIGHTPANDA_BIN 指向 lightpanda 可执行文件由平台自动拉起")
        u = urlparse(url)
        _lp_proc = subprocess.Popen(
            [bin_path, "serve", "--host", u.hostname or "127.0.0.1", "--port", str(u.port or 9222)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_lightpanda(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cdp_alive(url):
            return
        time.sleep(0.2)
    raise RuntimeError("Lightpanda 启动超时（10s）")


def launch_browser(p, engine: str | None = None) -> tuple[object, str, str]:
    """按引擎启动浏览器。返回 (browser, 实际使用的引擎, 说明)。

    p 是 playwright 的同步实例；engine 为空时取全局默认。
    Lightpanda 不可用时自动回退 Chromium；两者都不可用抛 RuntimeError（由调用方转为失败明细）。
    """
    eng = (engine or browser_engine()).lower()
    if eng == "lightpanda":
        url = lightpanda_url()
        try:
            if not _cdp_alive(url):
                _spawn_lightpanda(url)
                _wait_lightpanda(url)
            browser = p.chromium.connect_over_cdp(url)
            return browser, "lightpanda", f"Lightpanda @ {url}"
        except Exception as e:
            note = f"Lightpanda 不可用（{str(e)[:80]}），已回退 Chromium"
            try:
                return p.chromium.launch(headless=True), "chromium", note
            except Exception as e2:
                raise RuntimeError(
                    note + f"；Chromium 也未安装（{type(e2).__name__}）。"
                           "请在服务器执行 playwright install chromium --only-shell，或部署 Lightpanda 服务")
    return p.chromium.launch(headless=True), "chromium", ""


@atexit.register
def _shutdown():
    if _lp_proc and _lp_proc.poll() is None:
        try:
            _lp_proc.terminate()
        except Exception:
            pass
