"""行为级评估 runner：固定任务集 × 端到端执行 × 客观判定 × 报告/基线对比。

用法（在 backend/ 目录下）：
  python -m evals.runner                          # fake 模式：脚本模型驱动真实浏览器，
                                                  #   验证评估管线本身（CI 跑的就是它）
  python -m evals.runner --mode real \\
      --base-url https://api.xxx.com --key sk-xxx --model qwen-max [--vision] [--rounds 3]
                                                  # real 模式：真模型跑行为基准（调提示词/
                                                  #   压缩参数/换模型前后各跑一次做对比）
  python -m evals.runner --mode fake --baseline evals/reports/<文件>.json
                                                  # 与历史报告对比通过率（回归检测）

判定原则：不信任模型的 done 结论——登录看服务端状态，建单看服务器单号，
save 的单号必须与服务器生成的一致（real 模式）。

退出码：fake 模式有任务不通过即非零（管线坏了要挡住）；real 模式默认 0
（行为基准是信息性的），--strict 时按通过率决定。
"""
import argparse
import itertools
import json
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

_REPORT_DIR = Path(__file__).resolve().parent / "reports"
MOCK_PORT = 9011


def _parse():
    p = argparse.ArgumentParser(prog="evals", description="TestDeck 行为级评估")
    p.add_argument("--mode", choices=["fake", "real"], default="fake")
    p.add_argument("--rounds", type=int, default=0, help="每任务轮数（fake 默认 1，real 默认 3）")
    p.add_argument("--tasks", default="", help="只跑指定任务（逗号分隔名称），默认全部")
    p.add_argument("--base-url", default="", help="real 模式：模型 API 地址")
    p.add_argument("--key", default="", help="real 模式：模型 API Key")
    p.add_argument("--model", default="", help="real 模型：模型名")
    p.add_argument("--vision", action="store_true", help="real 模式：开启视觉（截图辅助）")
    p.add_argument("--baseline", default="", help="对比的历史报告 JSON（通过率差异展示）")
    p.add_argument("--strict", action="store_true", help="real 模式下不通过也返回非零")
    return p.parse_args()


# ---------- 脚本模型（fake 模式）：按剧本发工具调用，驱动真实浏览器 ----------

def _tc(name, seq, **inp):
    from agentscope.message import ToolCallBlock
    return ToolCallBlock(type="tool_call", id=f"call-{next(seq)}", name=name,
                         input=json.dumps(inp, ensure_ascii=False))


def _scripted_model_factory(script):
    """script: list[ToolCallBlock 列表]；每次 _call_api 弹出一轮（与 brain 测试桩同构）。"""
    from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel
    from agentscope.model._model_response import ChatUsage
    from agentscope.credential import OpenAICredential
    from agentscope.formatter import OpenAIChatFormatter

    class ScriptedModel(ChatModelBase):
        Parameters = OpenAIChatModel.Parameters

        def __init__(self):
            super().__init__(
                credential=OpenAICredential(api_key="stub", base_url="http://stub.local"),
                model="eval-scripted", parameters=OpenAIChatModel.Parameters(),
                stream=False, max_retries=0)
            self.formatter = OpenAIChatFormatter()
            self.n = 0

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kw):
            content = list(script[self.n]) if self.n < len(script) else []
            self.n += 1
            return ChatResponse(content=content, is_last=True,
                                usage=ChatUsage(input_tokens=10, output_tokens=5, time=0.01))

    return ScriptedModel()


def _fake_scripts():
    """fake 模式的任务剧本：动作是真实的（真实浏览器真实请求），只是决策来自脚本。"""
    s = itertools.count(1)

    def t(name, **inp):
        return _tc(name, s, **inp)

    return {
        "ui-login": [
            [t("browser_fill", think="填账号", target="#u", value="${username}")],
            [t("browser_fill", think="填密码", target="#p", value="${password}")],
            [t("browser_click", think="点登录", target="#btn")],
            [t("browser_expect_text", think="断言登录成功", value="登录成功")],
            [t("GenerateStructuredOutput", passed=True, reason="页面显示登录成功")],
        ],
        "ui-inquiry": [
            [t("browser_fill", think="填账号", target="#u", value="${username}")],
            [t("browser_fill", think="填密码", target="#p", value="${password}")],
            [t("browser_click", think="点登录", target="#btn")],
            [t("browser_expect_text", think="确认已登录", value="登录成功")],
            [t("browser_goto", think="去创建询价单", url="/page/inquiry/create")],
            [t("browser_fill_many", think="批量填起止城市", fields=[
                {"target": "#from", "value": "上海"}, {"target": "#to", "value": "北京"}])],
            [t("browser_click", think="点创建", target="#create")],
            # 脚本模型读不到动态单号，fake 只验服务端建单成功；real 模式才强校验 bill_no
            [t("GenerateStructuredOutput", passed=True, reason="已创建询价单")],
        ],
    }


# ---------- 环境 ----------

def _boot_mock_target():
    import uvicorn
    from tests import mock_target
    server = uvicorn.Server(uvicorn.Config(mock_target.app, host="127.0.0.1",
                                           port=MOCK_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="eval-mock").start()
    import httpx
    for _ in range(60):
        try:
            httpx.get(f"http://127.0.0.1:{MOCK_PORT}/__eval/state", timeout=1)
            return server
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"mock target 未能在 127.0.0.1:{MOCK_PORT} 启动")


def _eval_state():
    import httpx
    return httpx.get(f"http://127.0.0.1:{MOCK_PORT}/__eval/state", timeout=5).json()


def _reset_state():
    import httpx
    httpx.post(f"http://127.0.0.1:{MOCK_PORT}/__eval/reset", timeout=5)


def _judge(task, r, before, after, mode):
    """客观判定：只看服务端状态与已存变量（real 模式下 save 的单号必须与服务器一致）。"""
    j = task["judge"]
    if j == "ui_logged_in":
        return bool(after.get("ui_login")), f"服务端登录状态={after.get('ui_login')}"
    if j == "inquiry_created":
        ok = after.get("inquiries", 0) > before.get("inquiries", 0)
        note = f"询价单 {before.get('inquiries')}→{after.get('inquiries')}"
        if ok and mode == "real":
            bill = str((r.get("saved") or {}).get("bill_no", ""))
            ok = bill in (after.get("inquiry_ids") or [])
            note += f"；bill_no={bill or '（未保存）'}{'与服务器一致' if ok else '与服务器不符或未保存'}"
        return ok, note
    if j == "api_login_ok":
        return bool(after.get("api_login")), f"服务端 API 登录状态={after.get('api_login')}"
    return False, f"未知判定键 {j}"


def _run_tokens(run_id):
    from app.db import SessionLocal
    from app.models import LLMLog
    db = SessionLocal()
    try:
        rows = db.query(LLMLog).filter(LLMLog.run_id == run_id).all()
        return sum((x.prompt_tokens or 0) + (x.completion_tokens or 0) for x in rows)
    finally:
        db.close()


def run(args) -> int:
    # 隔离环境：临时 SQLite（必须在导入 app 模块前设置，同测试的做法）
    tmp = tempfile.mkdtemp(prefix="td-eval-")
    from app import config
    config.set("TD_DB", str(Path(tmp) / "eval.db"))
    config.set("TD_NO_SCHEDULER", "1")
    config.set("TD_MCP", "0")

    from types import SimpleNamespace
    from evals.tasks import TASKS, by_name
    from app.engine.ai_runner import run_ai_case

    # 评估不经过 app.main 的 lifespan：建表要自己做（app_pages/llm_logs 等执行链路会用到）
    from app import models
    from app.db import engine as _db_engine
    models.Base.metadata.create_all(_db_engine)

    # 模型接入：fake=按任务剧本的脚本模型；real=把 CLI 提供的模型插进临时库（brain 链与
    # chat_json 都从 llm_configs 读「使用中」条目，插一条两边全覆盖）
    _select_script = None
    if args.mode == "real":
        if not (args.base_url and args.key and args.model):
            print("real 模式需要 --base-url --key --model")
            return 2
        from app.db import SessionLocal
        from app.models import LLMConfig
        db = SessionLocal()
        try:
            db.add(LLMConfig(name="eval", base_url=args.base_url.rstrip("/"), api_key=args.key,
                             model=args.model, is_active=True, vision=bool(args.vision)))
            db.commit()
        finally:
            db.close()
    else:
        from app.engine import brain_agentscope as B
        from app import ai as A
        models = {name: _scripted_model_factory(script)
                  for name, script in _fake_scripts().items()}
        state = {"current": None}

        def _chain(on_usage=None):
            return state["current"]

        def _select(name):
            m = models.get(name)
            if m is not None:
                m.n = 0   # 复用同一模型实例，轮次间重置剧本进度
            state["current"] = m

        B.build_model_chain = _chain
        A.llm_available = lambda: True
        A.vision_enabled = lambda: False
        A._log_usage = lambda *a, **k: None
        _select_script = _select

    rounds = args.rounds or (1 if args.mode == "fake" else 3)
    names = [n.strip() for n in args.tasks.split(",") if n.strip()] or [t["name"] for t in TASKS]
    tasks = [by_name[n] for n in names if n in by_name]
    _boot_mock_target()
    base = f"http://127.0.0.1:{MOCK_PORT}"

    rows = []
    for task in tasks:
        if args.mode == "fake" and not task.get("fake", False):
            print(f"[skip] {task['name']}：该任务需要真实模型（fake 模式只覆盖 brain 路径）")
            continue
        for rd in range(1, rounds + 1):
            _reset_state()
            if _select_script is not None:
                _select_script(task["name"])
            before = _eval_state()
            run_id = f"E-{task['name']}-{rd}"
            case = SimpleNamespace(
                id=f"case-{task['name']}", name=task["name"], type="ai", project_id="",
                username=task["vars"].get("username", ""),
                password=task["vars"].get("password", ""),
                steps=[{"target": task["target"], "goal": task["goal"],
                        "start_url": task["start_url"],
                        "max_steps": task.get("max_steps", 60)}])
            env = SimpleNamespace(id="eval-env", name="eval", base_url=base,
                                  variables=dict(task["vars"]))
            t0 = time.time()
            try:
                r = run_ai_case(case, env, run_id)
            except Exception as e:
                r = {"status": "failed", "saved": {},
                     "summary": f"执行异常：{type(e).__name__}: {e}"[:200]}
            ok, note = _judge(task, r, before, _eval_state(), args.mode)
            tokens = _run_tokens(run_id) if args.mode == "real" else 0
            row = {"task": task["name"], "round": rd, "passed": ok,
                   "engine_status": r.get("status"), "duration": round(time.time() - t0, 1),
                   "tokens": tokens, "note": note,
                   "summary": (r.get("summary") or "")[:160]}
            rows.append(row)
            print(f"[{'PASS' if ok else 'FAIL'}] {task['name']} #{rd} "
                  f"({row['engine_status']}, {row['duration']}s, {row['tokens']} tok) {note}")

    report = _write_report(args, rows, base)
    print(f"\n报告：{report}")
    if args.baseline:
        _compare_baseline(args.baseline, rows)
    n_pass = sum(1 for x in rows if x["passed"])
    print(f"通过 {n_pass}/{len(rows)}（{args.mode} 模式）")
    if args.mode == "fake":
        return 0 if n_pass == len(rows) and rows else 1
    return 1 if (args.strict and n_pass < len(rows)) else 0


def _write_report(args, rows, base) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = args.mode if args.mode == "fake" else f"real-{args.model}"
    data = {"ts": ts, "mode": args.mode, "model": args.model or "scripted",
            "rounds": args.rounds or (1 if args.mode == "fake" else 3),
            "mock": base, "rows": rows}
    jpath = _REPORT_DIR / f"{ts}-{tag}.json"
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# TestDeck 行为评估 · {ts} · {args.mode} 模式",
             f"- 模型：{data['model']}　轮数/任务：{data['rounds']}　被测：{base}",
             "", "| 任务 | 轮 | 结果 | 引擎状态 | 耗时s | tokens | 判定依据 | 摘要 |",
             "|---|---|---|---|---|---|---|---|"]
    for x in rows:
        lines.append(f"| {x['task']} | {x['round']} | {'✅' if x['passed'] else '❌'} "
                     f"| {x['engine_status']} | {x['duration']} | {x['tokens']} "
                     f"| {x['note']} | {x['summary']} |")
    (_REPORT_DIR / f"{ts}-{tag}.md").write_text("\n".join(lines), encoding="utf-8")
    return jpath


def _compare_baseline(baseline_path, rows):
    from collections import defaultdict
    try:
        base = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"基线读取失败：{e}")
        return
    old = defaultdict(lambda: [0, 0])
    for x in base.get("rows", []):
        old[x["task"]][0] += 1 if x["passed"] else 0
        old[x["task"]][1] += 1
    new = defaultdict(lambda: [0, 0])
    for x in rows:
        new[x["task"]][0] += 1 if x["passed"] else 0
        new[x["task"]][1] += 1
    print(f"\n基线对比（{baseline_path}）：")
    for task in sorted(set(old) | set(new)):
        o, n = old[task], new[task]
        if not n[1]:
            continue
        delta = (n[0] / n[1] - (o[0] / o[1] if o[1] else 0)) * 100
        print(f"  {task}: 本次 {n[0]}/{n[1]} vs 基线 {o[0]}/{o[1]}（{delta:+.0f}%）")


if __name__ == "__main__":
    sys.exit(run(_parse()))
