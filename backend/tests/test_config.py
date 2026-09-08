"""配置中心：解析规则、set 覆盖、幂等（只认 .env 注册表，不读系统环境变量）。"""
import os  # noqa: F401  仅用于断言注册表与系统环境完全隔离

import pytest

from app import config
config.set("TD_DB", ":memory:")


@pytest.fixture(autouse=True)
def _restore_config():
    """用例内的 _values.clear() 不能把 conftest 的全局配置（内存库/禁调度/禁 MCP）带走。"""
    saved = dict(config._values)
    yield
    config._values.clear()
    config._values.update(saved)


def test_parse_rules(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "TD_SECRET=abc123\n"
        'TD_LLM_KEY="quoted-key"\n'
        "TD_ADMIN_PASSWORD = spaced \n"
        "BROKEN LINE WITHOUT EQUALS\n"
        "\n",
        encoding="utf-8")
    config._values.clear()
    config.load_dotenv_file(str(env_file))

    assert config.get("TD_SECRET") == "abc123"            # 普通 kv
    assert config.get("TD_LLM_KEY") == "quoted-key"       # 引号剥离
    assert config.get("TD_ADMIN_PASSWORD") == "spaced"    # 等号两侧空格剥离
    assert config.get("NOT_EXIST", "dft") == "dft"        # 缺省值
    config._values.clear()


def test_set_overrides_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TD_KEEP_DAYS=30\n", encoding="utf-8")
    config._values.clear()
    config.load_dotenv_file(str(env_file))
    assert config.get("TD_KEEP_DAYS") == "30"

    config.set("TD_KEEP_DAYS", "7")                       # 编程覆盖
    assert config.get("TD_KEEP_DAYS") == "7"
    config.unset("TD_KEEP_DAYS")
    config._values.clear()


def test_registry_isolated_from_os_environ(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TD_FROM_FILE=1\n", encoding="utf-8")
    config._values.clear()
    config.load_dotenv_file(str(env_file))
    assert config.get("TD_FROM_FILE") == "1"
    assert "TD_FROM_FILE" not in os.environ               # 不写回系统环境变量
    config._values.clear()


def test_missing_file_is_noop(tmp_path):
    n = len(config._values)
    config.load_dotenv_file(str(tmp_path / "nope.env"))   # 不报错即可
    assert len(config._values) >= n
