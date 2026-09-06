"""浏览器引擎选择逻辑（不启动真浏览器）。"""
import os

from app import config
config.set("TD_DB", ":memory:")

import pytest

from app.engine import browser
from app import config
from app.engine import browser
from app.engine.browser import launch_browser, browser_engine


class FakeBrowser:
    closed = False


class FakePlaywright:
    """按引擎记录调用并可控连接/启动成败。"""

    def __init__(self, cdp_ok=True, launch_ok=True):
        self.cdp_ok = cdp_ok
        self.launch_ok = launch_ok
        self.launched = []
        self.connected = []

    class _Chromium:
        def __init__(self, outer):
            self.outer = outer

        def launch(self, headless=False):
            if not self.outer.launch_ok:
                raise FileNotFoundError("Executable doesn't exist")
            self.outer.launched.append(headless)
            return FakeBrowser()

        def connect_over_cdp(self, url):
            if not self.outer.cdp_ok:
                raise ConnectionError("refused")
            self.outer.connected.append(url)
            return FakeBrowser()

    @property
    def chromium(self):
        return FakePlaywright._Chromium(self)


def test_default_chromium(monkeypatch):
    monkeypatch.delitem(config._values, "TD_BROWSER_ENGINE", raising=False)
    assert browser_engine() == "chromium"
    pw = FakePlaywright()
    _, used, note = launch_browser(pw)
    assert used == "chromium" and note == "" and pw.launched == [True] and not pw.connected


def test_lightpanda_connect(monkeypatch):
    monkeypatch.setitem(config._values, "TD_BROWSER_ENGINE", "lightpanda")
    monkeypatch.setitem(config._values, "TD_LIGHTPANDA_URL", "http://127.0.0.1:19222")
    monkeypatch.setattr(browser, "_cdp_alive", lambda url: True)   # 服务已在运行
    pw = FakePlaywright(cdp_ok=True)
    _, used, note = launch_browser(pw)          # engine 省略 → 跟随全局
    assert used == "lightpanda" and "19222" in note
    assert pw.connected == ["http://127.0.0.1:19222"] and not pw.launched


def test_lightpanda_fallback_to_chromium(monkeypatch):
    monkeypatch.setitem(config._values, "TD_BROWSER_ENGINE", "lightpanda")
    monkeypatch.delitem(config._values, "TD_LIGHTPANDA_BIN", raising=False)         # 无自动拉起 → 回退
    monkeypatch.setattr(browser, "_cdp_alive", lambda url: False)  # 服务不可达
    pw = FakePlaywright(cdp_ok=False, launch_ok=True)
    _, used, note = launch_browser(pw, engine="lightpanda")
    assert used == "chromium" and "回退" in note and pw.launched == [True]


def test_lightpanda_all_fail_raises(monkeypatch):
    monkeypatch.setitem(config._values, "TD_BROWSER_ENGINE", "lightpanda")
    monkeypatch.delitem(config._values, "TD_LIGHTPANDA_BIN", raising=False)
    monkeypatch.setattr(browser, "_cdp_alive", lambda url: False)
    pw = FakePlaywright(cdp_ok=False, launch_ok=False)             # 回退目标也没装
    with pytest.raises(RuntimeError) as ei:
        launch_browser(pw, engine="lightpanda")
    assert "playwright install" in str(ei.value) and "Lightpanda" in str(ei.value)
