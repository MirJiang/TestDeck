"""应用配置中心：只从 .env 配置文件读取，不使用系统环境变量。

查找顺序：backend/.env → 项目根/.env，找到第一个即止。
格式：key=value，支持 # 注释、空行、引号包裹、等号两侧空格。
get(key, default) 读取配置；set(key, value) 供测试与编程覆盖。
"""
from pathlib import Path

_values: dict = {}
_loaded = False


def load_dotenv_file(path: str | None = None) -> str | None:
    """载入 .env。已自动载入过则跳过；显式传 path 时强制重新载入并返回路径。"""
    global _loaded
    if path:
        _apply(path)
        return path
    if _loaded:
        return None
    _loaded = True
    for candidate in (Path(__file__).resolve().parent.parent / ".env",          # backend/.env
                      Path(__file__).resolve().parent.parent.parent / ".env"):  # 项目根/.env
        if candidate.exists():
            _apply(str(candidate))
            return str(candidate)
    return None


def get(key: str, default: str = "") -> str:
    load_dotenv_file()
    return _values.get(key, default)


def set(key: str, value: str) -> None:
    """编程式覆盖（测试 / 嵌入场景）。"""
    _values[key] = value


def unset(key: str) -> None:
    _values.pop(key, None)


def _apply(path: str) -> None:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            _values[key] = value
