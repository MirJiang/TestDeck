"""评估任务集：固定目标 + 客观判定。

判定键（judge）对应 runner.py 里的判定函数，全部基于 mock_target 的服务端状态：
- ui_logged_in    ：登录按钮真实走通了 /api/ui-login（服务端记录）
- inquiry_created ：询价单创建成功（服务端计数增加）；real 模式额外校验
                    save 到 bill_no 的单号与服务器生成的单号一致（防模型编造）
- api_login_ok    ：/api/login 登录成功（服务端记录）
"""

TASKS = [
    {
        "name": "ui-login",
        "target": "ui",
        "goal": "在登录页用账号 ${username}、密码 ${password} 登录，直到页面出现「登录成功」字样",
        "start_url": "/page/login",
        "vars": {"username": "admin", "password": "admin123"},
        "judge": "ui_logged_in",
        "fake": True,
    },
    {
        "name": "ui-inquiry",
        "target": "ui",
        "goal": "先登录（页面若已是登录后状态可跳过），然后创建一张出发城市「上海」、"
                "到达城市「北京」的询价单，把页面生成的询价单号保存到变量 bill_no",
        "start_url": "/page/login",
        "vars": {"username": "admin", "password": "admin123"},
        "judge": "inquiry_created",
        "max_steps": 80,
        "fake": True,
    },
    {
        "name": "api-login",
        "target": "api",
        "goal": "调用登录接口（POST /api/login，JSON 体含 username/password，用 ${username} 和 ${password}）"
                "验证能成功登录，把返回的 token 保存到变量 token",
        "start_url": "",
        "vars": {"username": "admin", "password": "admin123"},
        "judge": "api_login_ok",
        "fake": False,   # API 目标走 ai.chat_json 循环，fake 模式只覆盖 brain 路径
    },
]

by_name = {t["name"]: t for t in TASKS}
