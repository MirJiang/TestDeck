import os  # noqa: F401  仅保留 stdlib；测试配置一律走 config

from app import config

config.set("TD_NO_SCHEDULER", "1")   # 测试禁用调度器，避免跨事件循环串扰
config.set("TD_MCP", "0")            # 测试禁用 MCP（会话管理器与多 TestClient 生命周期冲突）
config.set("TD_DB", ":memory:")      # 内存库，测试间互不落盘
