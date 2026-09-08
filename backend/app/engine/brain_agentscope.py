"""AgentScope Brain（批次 B）：ReAct agent 替换手搓 prompt/parse 循环，ai_drive 只是门面。

安全红线（所有者定调，注册制强制）：
- toolkit 只注册浏览器测试动作白名单 goto/click/fill/expect_text/click_xy/drag/save
  （视觉模式额外注册 look——只回传截图与页面状态，不是开放式工具）；
- done 由结构化输出承担（Verdict → AgentScope 内置 GenerateStructuredOutput 工具）；
- AgentScope 自带的 Bash/Read/Write/Edit/PowerShell 等 shell/文件/代码执行类工具一律不注册，
  Harness.execute 执行前再做一次白名单校验（双保险，Docker 内也一样）。

桥接（B1 验证项①）：调用方在执行队列线程（同步）；Playwright sync API 有线程亲和性
（page 调用必须发生在创建它的线程），AgentScope 的 Agent 是 async——
因此 agent 跑在独立线程的独立事件循环里，工具把 Playwright 调用经 SimpleQueue+Future
转发回队列线程执行（run_brain 在队列线程上泵送），两个世界互不阻塞、无共享事件循环。

模型对接（B1 验证项③）：OpenAIChatModel + OpenAICredential 对接平台 llm_configs；
FallbackChatModel 实现多模型回退链（本次新增能力）：链上逐个尝试，失败自动切下一档，
每档用量经回调回写 llm_logs。链 = 数据库中「使用中」的模型配置（按 id 序）+ .env 兜底。

可观测性（B3）：每个工具执行点回填 detail（idx/action/think/target/pass/reason/ms/
url/selector/value/saved/screenshot/warning/engine，与旧 ai_drive 完全同构）+ on_step
流式回调；取消（queue registry）与断路器（同动作连续失败 3 次）经 ToolResponse(state=
INTERRUPTED) 立即终止 Agent 的 reply 循环；步数上限双保险（ReActConfig.max_iters +
Harness 动作计数）。
"""
import asyncio
import base64
import threading
import time
from concurrent.futures import Future
from queue import Empty, SimpleQueue
from typing import Any, Callable

from pydantic import BaseModel, Field

from .. import ai as A
from . import queue as _q
from .ai_runner import _STATE_JS, _exec_action, _model_hint, DEFAULT_MAX_STEPS
from .ui_runner import STATIC_DIR

# 工具白名单（红线）：除这些动作与结构化输出 done 外，agent 没有任何工具
ALLOWED_ACTIONS = ("goto", "click", "fill", "expect_text", "click_xy", "drag", "save")


class Verdict(BaseModel):
    """done：目标达成或确认无法继续时给出的测试结论（结构化输出）。"""
    passed: bool = Field(description="测试目标是否达成")
    reason: str = Field(description="一句话测试结论（给人看的，说明达成了什么或卡在哪）")


_SYSTEM = (
    "你是浏览器自动化测试 agent，通过调用工具一步步操控真实网页完成测试目标。\n"
    "规则：\n"
    "- 每次调用动作工具都要填 think 参数：一句话说明这一步为什么这么做。\n"
    "- click/fill 的 selector 必须原样使用工具结果里「可交互元素」给出的 selector，不要自己编造。\n"
    "- fill 的 value 可用 ${变量} 引用可用变量；save 把页面上看到的业务单号等值存为变量。\n"
    "- 动作失败（尤其超时）后不要重复同一个 selector，改用元素列表里的其他元素或其他定位方式；"
    "连续失败说明页面状态和预期不符，先观察工具返回的最新页面状态再行动。\n"
    "- 工具每次执行后都会返回最新页面状态（URL/标题/可交互元素/页面文字），据此规划下一步。\n"
    "- 验证码：滑块用 drag（起点=手柄中心像素坐标，终点=缺口位置，拖动会自动模拟人手轨迹）；"
    "点选按顺序多次 click_xy（每次一个目标中心像素坐标）；图片文字/算式读出内容后 fill 进输入框。"
    "坐标以页面视口左上角为原点。视觉模式下先用 look 工具看截图再决定坐标。\n"
    "- 目标达成或确认无法继续时，调用 GenerateStructuredOutput 给出结论："
    "passed（布尔）与 reason（一句话总结）。\n"
    "- 只做与测试目标相关的操作，不要偏离目标随意浏览。"
)


class Harness:
    """受控执行环境：agent 触碰浏览器的唯一通道（他们出脑子，我们出手）。

    工具在 agent 事件循环线程被调用，但 Playwright 调用必须回到创建 page 的
    队列线程执行——dispatch/pump 完成这个跨线程转发。
    """

    def __init__(self, page, goal: str, variables: dict, run_id: str, shot_tag: str,
                 on_step, engine: str, page_map: str, project_id: str,
                 vision: bool, max_actions: int):
        self.page = page
        self.goal = goal
        self.variables = dict(variables or {})
        self.run_id = run_id
        self.shot_tag = shot_tag
        self.on_step = on_step
        self.engine = engine
        self.page_map = page_map
        self.project_id = project_id
        self.vision = vision
        self.max_actions = max_actions

        self.detail: list[dict] = []
        self.saved: dict[str, str] = {}
        self.pass_n = self.fail_n = 0
        self.idx = 0
        self.t0 = time.time()
        self.stop = ""                 # 非空 = 取消/熔断/超限，工具返回 INTERRUPTED 结束 reply
        self.stop_kind = ""            # cancel | breaker | limit | error
        self.last_fail_key, self.same_fail = None, 0
        self.last_warn = ""
        self.usage: list[dict] = []    # [{model, input_tokens, output_tokens, ok}]
        self.calls: SimpleQueue = SimpleQueue()   # (fn, args, future) → 队列线程执行

    # ---------- 跨线程桥接 ----------

    def dispatch(self, fn: Callable, *args) -> Future:
        """把 Playwright 调用转发到队列线程。在 agent 事件循环线程调用，返回 Future。"""
        fut: Future = Future()
        self.calls.put((fn, args, fut))
        return fut

    def pump(self):
        """队列线程主循环：执行 agent 转发来的 Playwright 调用，直到收到结束哨兵。"""
        while True:
            try:
                fn, args, fut = self.calls.get(timeout=0.2)
            except Empty:
                continue
            if fn is None:              # agent 线程结束哨兵
                return
            try:
                fut.set_result(fn(*args))
            except Exception as e:      # 异常传回 agent 线程，由工具捕获转为失败记录
                fut.set_exception(e)

    # ---------- 动作执行（队列线程侧） ----------

    def execute(self, action: str, think: str, params: dict) -> tuple[bool, str]:
        """执行一个白名单动作并记录明细。返回 (ok, reason)。"""
        if action not in ALLOWED_ACTIONS:      # 红线双保险：白名单外一律拒绝
            return False, f"动作 {action} 不在白名单内，已拒绝执行"
        if self.stop:
            return False, self.stop

        # 执行前检查取消（与旧循环语义一致：步与步之间）
        if self.run_id and _q.is_cancelled(self.run_id):
            self._interrupt("cancel", "已手动取消")
            return False, self.stop

        # A2 执行比对信号：地图（期望基线）按钮缺失 → 本步记 warning（不判失败）
        warn = ""
        if self.project_id:
            try:
                from .app_mapper import map_gaps
                state = self.page.evaluate(_STATE_JS)
                w = map_gaps(self.project_id, state.get("url", ""), self.page)
            except Exception:
                w = ""
            if w != self.last_warn:
                warn = w
            self.last_warn = w

        act = {"action": action, **params}
        t0 = time.time()
        try:
            ok, reason, saved_kv = _exec_action(self.page, act, self.variables)
        except Exception as e:
            ok, reason, saved_kv = False, f"{type(e).__name__}: {e}"[:200], None
        ms = int((time.time() - t0) * 1000)

        self.idx += 1
        entry = {"idx": self.idx, "action": action, "think": (think or "")[:120], "ms": ms}
        if warn:
            entry["warning"] = warn
        entry.update(target=reason.split("，")[0][:80], **{"pass": ok}, reason=reason)
        entry["url"] = str(params.get("url", ""))
        entry["selector"] = str(params.get("selector", ""))
        entry["value"] = str(params.get("value", "")) if action in ("fill", "expect_text", "save") else ""
        if action == "save" and ok and saved_kv:
            self.saved[saved_kv[0]] = saved_kv[1]
            entry["saved"] = saved_kv[0]
        if self.engine and not self.detail:
            entry["engine"] = self.engine     # 引擎标注在首条明细（与旧循环一致）
        self.detail.append(entry)
        self.pass_n, self.fail_n = self.pass_n + (1 if ok else 0), self.fail_n + (0 if ok else 1)
        self._emit()

        # 断路器：同一动作+selector 连续失败 3 次，立即终止（避免模型反复撞墙）
        if not ok:
            key = (action, str(params.get("selector", "")))
            self.same_fail = self.same_fail + 1 if key == self.last_fail_key else 1
            self.last_fail_key = key
            if self.same_fail >= 3:
                self._interrupt(
                    "breaker",
                    f"动作 {action}({params.get('selector', '')}) 连续 {self.same_fail} 次失败，已提前终止")
        else:
            self.last_fail_key, self.same_fail = None, 0

        # 步数上限（与 ReActConfig.max_iters 双保险）
        if not self.stop and self.idx >= self.max_actions:
            self._interrupt("limit", f"达到最大步数（{self.max_actions}）仍未完成目标")
        return ok, reason

    def _interrupt(self, kind: str, msg: str):
        self.stop_kind = kind
        self.stop = msg
        self.idx += 1
        entry = {"idx": self.idx, "action": "error", "target": "", "pass": False,
                 "reason": msg, "ms": 0}
        if kind == "cancel":
            entry["action"] = "note"
        self._shot(entry)
        self.detail.append(entry)
        self._emit()
        self.fail_n += 1     # 与旧循环口径一致：取消/熔断/超限都计一次失败

    def _emit(self):
        if self.on_step:
            try:
                self.on_step([dict(d) for d in self.detail])
            except Exception:
                pass

    def _shot(self, entry: dict):
        try:
            path = STATIC_DIR / f"{self.run_id or 'ai'}-{self.shot_tag}-{int(time.time())}.png"
            self.page.screenshot(path=str(path), full_page=False)
            entry["screenshot"] = f"/static/{path.name}"
        except Exception:
            pass

    def shot_b64(self) -> str | None:
        """视口截图（jpeg base64），失败（如 Lightpanda 不支持）返回 None。"""
        try:
            return base64.b64encode(self.page.screenshot(type="jpeg", quality=80)).decode()
        except Exception:
            return None

    def state_text(self) -> str:
        """最新页面状态文本（与旧循环注入给模型的状态同构），失败时给降级说明。"""
        try:
            state = self.page.evaluate(_STATE_JS)
        except Exception as e:
            return f"页面状态读取失败：{e}"
        els = "; ".join(f"[{e['tag']}] {e['text'] or e['value']} → {e['selector']}"
                        for e in state.get("elements", [])[:40]) or "无"
        return (f"当前页面：{state.get('url', '')}「{state.get('title', '')}」\n"
                f"可交互元素：{els}\n页面文字：{state.get('text', '')}")

    # ---------- 结果组装 ----------

    def result(self, verdict: dict | None) -> dict:
        summary = ""
        if verdict is not None:
            ok = bool(verdict.get("passed"))
            summary = verdict.get("reason") or ("目标达成" if ok else "模型判定未达成")
            self.idx += 1
            entry = {"idx": self.idx, "action": "done", "target": "", "think": "",
                     "pass": ok, "reason": summary[:300], "ms": 0,
                     "url": "", "selector": "", "value": ""}
            self._shot(entry)
            self.detail.append(entry)
            self.pass_n, self.fail_n = self.pass_n + (1 if ok else 0), self.fail_n + (0 if ok else 1)
            self._emit()
            status = "passed" if ok else "failed"
        elif self.stop_kind == "cancel":
            summary, status = "用户取消", "failed"
        else:
            summary = self.stop or f"超过最大步数 {self.max_actions}"
            status = "failed"
        if status == "failed" and summary != "用户取消":
            hint = _model_hint()
            if hint:
                summary += f"；建议：{hint}"
        return {"status": status, "pass_n": self.pass_n, "fail_n": self.fail_n,
                "duration": round(time.time() - self.t0, 2), "detail": self.detail,
                "saved": self.saved, "summary": summary,
                "brain": "agentscope", "usage": list(self.usage)}


# ---------- 模型层：llm_configs 对接 + 多模型回退链 ----------

def _chain_entries() -> list[tuple[str, str, str]]:
    """回退链配置来源：数据库「使用中」模型（按 id 序）+ .env 兜底（去重后追加）。"""
    entries: list[tuple[str, str, str]] = []   # (base_url, api_key, model)
    try:
        from ..db import SessionLocal
        from ..models import LLMConfig
        db = SessionLocal()
        try:
            rows = db.query(LLMConfig).filter(LLMConfig.is_active == True).order_by(LLMConfig.id)  # noqa
            for r in rows:
                if r.base_url and r.api_key:
                    entries.append((r.base_url.rstrip("/"), r.api_key, r.model or ""))
        finally:
            db.close()
    except Exception:
        pass
    fb = A.fallback_llm()
    if fb[0] and fb[1] and fb not in entries:
        entries.append(fb)
    return entries


def build_model_chain(on_usage: Callable[[str, int, int, bool], None] | None = None):
    """从平台 llm_configs 构建回退链模型；全部不可用时返回 None。"""
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    entries = _chain_entries()
    if not entries:
        return None
    models = []
    for base, key, model in entries:
        models.append(OpenAIChatModel(
            credential=OpenAICredential(api_key=key, base_url=base),
            model=model,
            stream=False,               # 与旧循环一致：非流式，用量一次性拿到
            max_retries=1,              # 单档快速失败，交给回退链换档
            parameters=OpenAIChatModel.Parameters(temperature=0.1),
        ))
    return make_fallback_model(models, on_usage=on_usage)


def make_fallback_model(models: list, on_usage=None):
    """多模型回退链：按序尝试链上的 OpenAIChatModel，失败自动切下一档。

    实现为 ChatModelBase 子类（模板方法 _call_api），复用 AgentScope 的
    非流式管道；每档调用结果经 on_usage(model, input_tokens, output_tokens, ok)
    回调供 llm_logs 记账。
    """
    from agentscope.model import ChatModelBase, OpenAIChatModel
    from agentscope.formatter import OpenAIChatFormatter

    class FallbackChatModel(ChatModelBase):
        Parameters = OpenAIChatModel.Parameters

        def __init__(self):
            super().__init__(credential=models[0].credential, model=models[0].model,
                             parameters=models[0].parameters, stream=False,
                             max_retries=models[0].max_retries,
                             context_size=models[0].context_size)
            self.chain = models
            self.on_usage = on_usage
            self.formatter = OpenAIChatFormatter()   # Agent 推理管道需要（与 OpenAIChatModel 对齐）

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kw):
            last: Exception | None = None
            for m in self.chain:
                try:
                    resp = await m._call_api(m.model, messages, tools=tools,
                                             tool_choice=tool_choice, **kw)
                    u = getattr(resp, "usage", None)
                    if self.on_usage:
                        self.on_usage(m.model,
                                      getattr(u, "input_tokens", 0) or 0,
                                      getattr(u, "output_tokens", 0) or 0, True)
                    return resp
                except Exception as e:
                    last = e
                    if self.on_usage:
                        self.on_usage(m.model, 0, 0, False)
                    continue
            raise last if last else RuntimeError("模型回退链为空")

    return FallbackChatModel()


# ---------- 工具层：白名单注册 ----------

def _tool_response(text: str, interrupted: bool = False, image_b64: str | None = None):
    from agentscope.message import Base64Source, DataBlock, TextBlock, ToolResultState
    from agentscope.tool import ToolResponse
    content: list = []
    if image_b64:
        content.append(DataBlock(type="data", source=Base64Source(
            type="base64", media_type="image/jpeg", data=image_b64)))
    content.append(TextBlock(type="text", text=text))
    return ToolResponse(content=content,
                        state=ToolResultState.INTERRUPTED if interrupted else ToolResultState.SUCCESS)


def build_toolkit(h: Harness):
    """注册白名单工具。除这些外 agent 无任何工具（done=结构化输出，look 仅视觉模式）。"""
    from agentscope.tool import FunctionTool, Toolkit

    async def _run(action: str, think: str, params: dict, feedback: str = "") -> Any:
        """在队列线程执行动作，返回工具响应（含最新页面状态）。"""
        fut = h.dispatch(h.execute, action, think, params)
        ok, reason = await asyncio.wrap_future(fut)
        if h.stop:
            return _tool_response(f"{reason}\n执行已终止：{h.stop}", interrupted=True)
        state = await _state_text()
        text = f"{'成功' if ok else '失败'}：{reason}"
        if feedback:
            text += f"\n{feedback}"
        return _tool_response(f"{text}\n\n{state}")

    # state_text 也要走队列线程（page.evaluate 有线程亲和性）
    async def _state_text():
        return await asyncio.wrap_future(h.dispatch(h.state_text))

    async def browser_goto(think: str, url: str):
        """打开新地址。url 用完整地址或以 / 开头的相对路径。仅在需要跳转到全新页面时使用。"""
        return await _run("goto", think, {"url": url})

    async def browser_click(think: str, selector: str):
        """点击元素。selector 必须原样使用页面状态里给出的 selector。"""
        return await _run("click", think, {"selector": selector})

    async def browser_fill(think: str, selector: str, value: str):
        """在输入框填入文字。value 可用 ${变量} 引用可用变量。"""
        return await _run("fill", think, {"selector": selector, "value": value})

    async def browser_expect_text(think: str, value: str):
        """断言页面包含指定文字，不包含则该步失败。用于验证操作结果。"""
        return await _run("expect_text", think, {"value": value})

    async def browser_click_xy(think: str, x: int, y: int):
        """按像素坐标点击（视口左上角为原点）。用于点选验证码等无 selector 可用的图形目标，一次点一个。"""
        return await _run("click_xy", think, {"x": x, "y": y})

    async def browser_drag(think: str, x: int, y: int, x2: int, y2: int):
        """从 (x,y) 按住拖拽到 (x2,y2)，自动分步模拟人手轨迹。用于滑块验证码：起点=手柄中心，终点=缺口位置。"""
        return await _run("drag", think, {"x": x, "y": y, "x2": x2, "y2": y2})

    async def browser_save(think: str, name: str, value: str):
        """把页面上看到的业务值（单号/金额等）存为变量 ${name}，供后续步骤引用。"""
        return await _run("save", think, {"name": name, "value": value})

    async def browser_look(think: str):
        """拍摄当前视口截图并返回（附最新页面状态）。需要看图定位（验证码/图形元素）时使用。"""
        fut = h.dispatch(h.shot_b64)
        img = await asyncio.wrap_future(fut)
        state = await _state_text()
        if not img:
            return _tool_response(f"截图不可用（当前浏览器引擎不支持），依据文字状态操作。\n{state}")
        return _tool_response(state, image_b64=img)

    tools = [browser_goto, browser_click, browser_fill, browser_expect_text,
             browser_click_xy, browser_drag, browser_save]
    if h.vision:
        tools.append(browser_look)
    return Toolkit(tools=[FunctionTool(f) for f in tools])


# ---------- 同步入口（门面，签名与返回结构对齐 ai_drive） ----------

def run_brain(page, goal: str, variables: dict, max_steps: int = DEFAULT_MAX_STEPS,
              run_id: str = "", shot_tag: str = "ai", on_step=None, engine: str = "",
              page_map: str = "", project_id: str = "") -> dict:
    """AgentScope 驱动的同步入口。须在执行队列线程调用（Playwright 线程亲和性）。

    返回结构与 ai_drive 完全一致：{status, pass_n, fail_n, duration, detail, saved, summary}，
    额外带 brain="agentscope" 与 usage（供 llm_logs 记账）。
    """
    if not A.llm_available():
        return {"status": "failed", "pass_n": 0, "fail_n": 1, "duration": 0.0,
                "detail": [{"idx": 1, "action": "error", "target": "", "pass": False,
                            "reason": "AI 测试需要配置大模型：请在「模型配置」页添加并启用，"
                                      "或在 .env 配置 TD_LLM_BASE_URL / TD_LLM_KEY", "ms": 0}],
                "saved": {}, "summary": "未配置 LLM"}

    def _on_usage(model, pt, ct, ok):
        try:
            A._log_usage("agent-step", pt, ct, ok, model=model)
        except Exception:
            pass

    h = Harness(page, goal, variables, run_id, shot_tag, on_step, engine, page_map,
                project_id, vision=A.vision_enabled(), max_actions=max_steps)
    result_box: dict = {}

    def _agent_thread_main():
        try:
            asyncio.run(_agent_main(h, goal, variables, max_steps, result_box, _on_usage))
        except BaseException as e:   # 异常也要让队列线程的 pump 退出
            result_box.setdefault("error", f"{type(e).__name__}: {e}"[:300])
        finally:
            h.calls.put((None, None, None))   # 结束哨兵

    t = threading.Thread(target=_agent_thread_main, daemon=True,
                         name=f"brain-{run_id or 'ai'}")
    t.start()
    h.pump()          # 队列线程：泵送 Playwright 调用直到 agent 结束
    t.join()

    if "error" in result_box:
        h._interrupt("error", f"Agent 执行异常：{result_box['error']}")
    return h.result(result_box.get("verdict"))   # 队列线程组装（done 截图线程亲和）


async def _agent_main(h: Harness, goal: str, variables: dict, max_steps: int,
                      result_box: dict, on_usage):
    """agent 线程的事件循环主体：构建模型/工具/Agent 并跑一轮 reply。"""
    from agentscope.agent import Agent, ReActConfig
    from agentscope.message import UserMsg
    from agentscope.permission import (PermissionBehavior, PermissionContext,
                                       PermissionMode, PermissionRule)
    from agentscope.state import AgentState

    model = build_model_chain(on_usage=on_usage)
    if model is None:
        result_box["error"] = "无可用模型配置"
        return
    toolkit = build_toolkit(h)

    # 红线白名单强制（AgentScope 工具权限）：DONT_ASK 无人值守模式——
    # 只有 ALLOW 规则放行的注册工具可执行，其余一律 DENY（不询问、不挂起）。
    # done 走内置 GenerateStructuredOutput（只读工具，走 read-only 快速放行）。
    allowed = [f"browser_{a}" for a in ALLOWED_ACTIONS] + (["browser_look"] if h.vision else [])
    perm = PermissionContext(mode=PermissionMode.DONT_ASK)
    for name in allowed:
        perm.allow_rules[name] = [PermissionRule(
            tool_name=name, rule_content=None,
            behavior=PermissionBehavior.ALLOW, source="testdeck")]
    state = AgentState(permission_context=perm)

    var_txt = "; ".join(f"${k}={v}" for k, v in (variables or {}).items()) or "无"
    prompt = f"测试目标：{goal}\n可用变量：{var_txt}"
    if h.page_map:
        prompt += f"\n\n{h.page_map}"
    if h.vision:
        prompt += ("\n\n当前已开启视觉：涉及验证码或图形定位时，先调用 browser_look 看截图再操作坐标。")

    agent = Agent(name="testdeck", system_prompt=_SYSTEM, model=model, toolkit=toolkit,
                  state=state, react_config=ReActConfig(max_iters=max(2, max_steps)))
    try:
        final = await agent.reply(UserMsg(name="user", content=prompt),
                                  structured_schema=Verdict)
    except Exception as e:
        result_box["error"] = f"{type(e).__name__}: {e}"[:300]
        return

    verdict = None
    so = getattr(final, "structured_output", None)
    if so is not None:
        verdict = so.model_dump() if isinstance(so, BaseModel) else dict(so)
    result_box["verdict"] = verdict   # 结果组装回队列线程做（done 截图有线程亲和性）
