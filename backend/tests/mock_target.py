"""E2E 用的被测系统：模拟登录/用户信息接口。"""
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    if body.get("username") == "admin" and body.get("password") == "admin123":
        _EVAL_STATE["api_login"] = True
        return {"code": 0, "msg": "登录成功", "data": {"token": "tok-123", "uid": 1}}
    return {"code": 4001, "msg": "用户名或密码错误"}


@app.get("/api/user/info")
async def info():
    return {"code": 0, "data": {"uid": 1, "name": "张伟"}}


@app.get("/api/user/orders")
async def orders():
    return {"code": 0, "data": {"list": [{"id": 1}, {"id": 2}]}}


DEMO_PAGE = """
<html><body style="font-family:sans-serif;padding:40px">
<h2>运营后台 · 登录</h2>
<input id="u" placeholder="用户名"> <input id="p" type="password" placeholder="密码">
<button id="btn" onclick="fetch('/api/ui-login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})}).then(r=>r.json()).then(d=>document.getElementById('msg').innerText=d.code===0?'登录成功，欢迎回来':'登录失败，用户名或密码错误')">登 录</button>
<p id="msg"></p>
</body></html>
"""


@app.get("/page/login")
async def page_login():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(DEMO_PAGE)


# ---------- 询价单多角色业务（流程测试演示） ----------
# 货主创建询价单 → 多个物流公司各自登录页面报价 → 货主查看结果
_INQUIRIES = {}
_seq = [100]


@app.post("/api/inquiry/create")
async def inquiry_create(req: Request):
    body = await req.json()
    _seq[0] += 1
    iid = f"IQ-{_seq[0]}"
    _INQUIRIES[iid] = {"from": body.get("from", ""), "to": body.get("to", ""), "quotes": {}}
    return {"code": 0, "data": {"id": iid}}


@app.post("/api/inquiry/{iid}/quote")
async def inquiry_quote(iid: str, req: Request):
    body = await req.json()
    if iid not in _INQUIRIES:
        return {"code": 404, "msg": "询价单不存在"}
    _INQUIRIES[iid]["quotes"][body.get("carrier", "?")] = body.get("price", 1000)
    return {"code": 0, "data": {"quoted": len(_INQUIRIES[iid]["quotes"])}}


def _quote_page(iid: str, carrier: str):
    return f"""
<html><body style="font-family:sans-serif;padding:40px">
<h2>物流公司报价台 · {carrier}</h2>
<p>询价单 <b>{iid}</b> 状态：待报价</p>
<label>运价 <input id="price" value="1280"></label>
<button id="quote" onclick="fetch('/api/inquiry/{iid}/quote',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{carrier:'{carrier}',price:document.getElementById('price').value}})}}).then(r=>r.json()).then(d=>document.getElementById('msg').innerText=d.code===0?'报价成功，已有 '+d.data.quoted+' 家报价':'报价失败')">提交报价</button>
<p id="msg"></p>
</body></html>"""


@app.get("/page/inquiry/quote")
async def inquiry_quote_page(id: str, carrier: str = "A"):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_quote_page(id, carrier))


@app.get("/page/inquiry/result")
async def inquiry_result_page():
    from fastapi.responses import HTMLResponse
    rows = "".join(
        f"<tr><td>{iid}</td><td>{len(v['quotes'])}</td><td>{'、'.join(v['quotes']) or '—'}</td></tr>"
        for iid, v in _INQUIRIES.items())
    return HTMLResponse(f"""
<html><body style="font-family:sans-serif;padding:40px">
<h2>货主 · 询价单列表</h2>
<table border="1" cellpadding="6"><tr><th>询价单号</th><th>已收报价</th><th>报价方</th></tr>{rows}</table>
<p id="msg"></p>
</body></html>""")


# ---------- 行为评估（evals）支撑：服务端可判定的状态 + 询价创建页 ----------
# 评估的判定不信任模型自己的 done 结论，全部以服务端状态/已存变量为准（客观判定）。
_EVAL_STATE = {"ui_login": False, "api_login": False}


@app.post("/api/ui-login")
async def ui_login(req: Request):
    body = await req.json()
    if body.get("username") == "admin" and body.get("password") == "admin123":
        _EVAL_STATE["ui_login"] = True
        return {"code": 0}
    return {"code": 4001}


_CREATE_PAGE = """
<html><body style="font-family:sans-serif;padding:40px">
<h2>新建询价单</h2>
<label>出发城市 <input id="from" placeholder="上海"></label><br><br>
<label>到达城市 <input id="to" placeholder="北京"></label><br><br>
<button id="create" onclick="fetch('/api/inquiry/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:document.getElementById('from').value,to:document.getElementById('to').value})}).then(r=>r.json()).then(d=>document.getElementById('msg').innerText=d.code===0?'已创建询价单 '+d.data.id+'，等待物流报价':'创建失败')">创建询价单</button>
<p id="msg"></p>
<p><a href="/page/inquiry/result">查看询价单列表</a></p>
</body></html>
"""


@app.get("/page/inquiry/create")
async def inquiry_create_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_CREATE_PAGE)


@app.get("/__eval/state")
async def eval_state():
    return {"ui_login": _EVAL_STATE["ui_login"], "api_login": _EVAL_STATE["api_login"],
            "inquiries": len(_INQUIRIES), "inquiry_ids": sorted(_INQUIRIES)}


@app.post("/__eval/reset")
async def eval_reset():
    _INQUIRIES.clear(); _seq[0] = 100
    _EVAL_STATE["ui_login"] = _EVAL_STATE["api_login"] = False
    return {"ok": True}
