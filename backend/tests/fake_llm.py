"""假 LLM 服务器：OpenAI 兼容接口，按脚本顺序返回动作决策。

用途：AI 用例引擎的端到端验证（无需真实大模型 key）。
每次调用延迟约 1 秒，便于验证"边执行边写库"的流式进度。
启动：python -m uvicorn tests.fake_llm:app --port 9111
"""
import json
import time
from fastapi import FastAPI, Request

app = FastAPI()

SCRIPT = [
    {"think": "先输入用户名", "action": "fill", "selector": "#u", "value": "${username}"},
    {"think": "再输入密码", "action": "fill", "selector": "#p", "value": "${password}"},
    {"think": "点击登录按钮", "action": "click", "selector": "#btn"},
    {"think": "页面应显示登录成功", "action": "expect_text", "value": "登录成功"},
    {"think": "目标达成", "action": "done", "pass": True, "reason": "已用指定账号登录成功"},
]
_n = {"i": 0}


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat(req: Request):
    time.sleep(1.0)   # 模拟真实模型延迟，让流式进度可观察
    body = await req.json()
    act = SCRIPT[min(_n["i"], len(SCRIPT) - 1)]
    _n["i"] += 1
    return {"choices": [{"message": {"content": json.dumps(act, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 30}}


@app.get("/models")
@app.get("/v1/models")
def models():
    return {"data": [{"id": "fake-chat-mini"}, {"id": "fake-chat-pro"}, {"id": "fake-embed"}]}


@app.get("/reset")
def reset():
    _n["i"] = 0
    return {"ok": True}
