"""AgentScope Brain（批次 B）：ReAct agent 替换手搓 prompt/parse 循环，ai_drive 只是门面。

安全红线（所有者定调，注册制强制）：
- toolkit 只注册浏览器测试动作白名单 goto/click/dblclick/fill/fill_many/expect_text/click_xy/drag/save
  （fill_many 在工具层拆成原子 fill 执行；视觉模式额外注册 look——只回传截图与页面状态，
  不是开放式工具）；
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
from .. import config
from . import queue as _q
from .ai_runner import _STATE_JS, _exec_action, _model_hint, DEFAULT_MAX_STEPS
from .ui_runner import STATIC_DIR

# 工具白名单（红线）：除这些动作与结构化输出 done 外，agent 没有任何工具
# fill_many 是批量 fill（工具层逐个拆成 fill 执行，明细仍是原子 fill，固化回放不受影响）
ALLOWED_ACTIONS = ("goto", "click", "dblclick", "fill", "fill_many", "expect_text",
                   "click_xy", "click_xy_many", "drag", "scroll", "save")


class Verdict(BaseModel):
    """done：目标达成或确认无法继续时给出的测试结论（结构化输出）。"""
    passed: bool = Field(description="测试目标是否达成")
    reason: str = Field(description="一句话测试结论（给人看的，说明达成了什么或卡在哪）")


_SYSTEM = (
    "你是浏览器自动化测试 agent，通过调用工具一步步操控真实网页完成测试目标。\n"
    "规则：\n"
    "- 每次调用动作工具都要填 think 参数：一句话说明这一步为什么这么做。\n"
    "- click/fill/fill_many 的 target 填最新页面状态里方括号中的元素句柄（如 b12）；"
    "同一元素句柄整个执行中不变，但翻页/弹层后要先看最新回包再用句柄。\n"
    "- 需要连续填写多个输入框时优先一次 browser_fill_many 批量填（最多 12 个），减少往返；"
    "下拉选项、日期面板等非输入元素仍逐个 click。fill/save 的值可用 ${变量} 引用可用变量；"
    "save 把页面上看到的业务单号等值存为变量。\n"
    "- 动作失败（尤其超时）后不要重复同一目标，改用元素列表里的其他元素或其他定位方式；"
    "连续失败说明页面状态和预期不符，先观察工具返回的最新页面状态再行动。\n"
    "- 弹窗/列表里的行，单击只高亮不生效时改用 browser_dblclick 双击该行（双击选行类控件）。\n"
    "- 视口下方可能还有未展示的表单区块：元素清单里找不到需要的字段时先 browser_scroll 向下滚动再看状态；"
    "点击/填写元素句柄时浏览器也会自动滚到该元素。\n"
    "- 日期/时间输入框优先直接 fill 完整值（如 2026-09-30 18:00:00），多数组件支持；"
    "打开面板点选是兜底，面板按钮也优先用元素句柄而非坐标。\n"
    "- 工具每次执行后都会返回最新页面状态（URL/标题/可交互元素/页面文字），据此规划下一步。\n"
    "- 验证码：滑块用 drag（起点=手柄中心像素坐标，终点=缺口位置，拖动会自动模拟人手轨迹）；"
    "点选先用 look 看清全部文字的顺序与坐标，再用 browser_click_xy_many 一次点完（坐标数组，"
    "每个字的目标中心像素坐标），不要逐个 click_xy；图片文字/算式读出内容后 fill 进输入框。"
    "坐标以页面视口左上角为原点。视觉模式下先用 look 工具看截图再决定坐标。\n"
    "- 目标达成或确认无法继续时，调用 GenerateStructuredOutput 给出结论："
    "passed（布尔）与 reason（一句话总结）。\n"
    "- 只做与测试目标相关的操作，不要偏离目标随意浏览。"
)

# 压缩摘要的测试状态卡片（替代通用"续作摘要"）：字段即模板占位符，缺一会 KeyError，
# 所以全部 required。结构化卡片让压缩可以更频繁而不失真——模型拿到的是清单不是作文。
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "description": "测试目标，一句话"},
        "page_now": {"type": "string", "description": "当前页面 URL（含 hash 路由）与所在表单/列表名"},
        "form_state": {"type": "string", "description": "表单已填清单，逐行「字段=值」，含已选下拉、已填输入框、已勾选项"},
        "variables": {"type": "string", "description": "已保存变量清单「${名}=值」，没有则写「无」"},
        "progress": {"type": "string", "description": "已完成的关键操作（登录/验证码/弹窗处理/跳转），简短时序"},
        "next_steps": {"type": "string", "description": "接下来要做的 2-4 步，具体到字段与按钮名"},
        "blockers": {"type": "string", "description": "已踩过的坑：失效或超时的元素定位、会被遮挡的按钮、面板交互陷阱；没有则写「无」"},
    },
    "required": ["goal", "page_now", "form_state", "variables", "progress", "next_steps", "blockers"],
}
_SUMMARY_TEMPLATE = (
    "<system-info>以下是此前工作的测试状态卡片（据此继续，不要重复已完成的步骤）\n"
    "# 测试目标\n{goal}\n\n# 当前页面\n{page_now}\n\n# 表单已填\n{form_state}\n\n"
    "# 已存变量\n{variables}\n\n# 已完成操作\n{progress}\n\n# 下一步\n{next_steps}\n\n"
    "# 阻塞与教训\n{blockers}\n</system-info>"
)


class Harness:
    """受控执行环境：agent 触碰浏览器的唯一通道（他们出脑子，我们出手）。

    工具在 agent 事件循环线程被调用，但 Playwright 调用必须回到创建 page 的
    队列线程执行——dispatch/pump 完成这个跨线程转发。
    """

    def __init__(self, page, goal: str, variables: dict, run_id: str, shot_tag: str,
                 on_step, engine: str, page_map: str, project_id: str,
                 vision: bool, max_actions: int, map_keys: set | None = None):
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
        self.map_keys = map_keys or set()   # 地图已收录页面 key（页面文字省略判定）

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
        self._prev_state = None        # 上一次 state_text 的 (url, 元素集合签名)，增量回报用
        self._handles: dict[str, str] = {}   # selector → 句柄（显示用，整个执行期一一对应）
        self._sel_by_h: dict[str, str] = {}  # 句柄 → selector（动作解析用）
        self._handle_n = 0
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
                url = self.page.evaluate("location.href")   # 只取 URL，不做全页状态扫描
                w = map_gaps(self.project_id, url, self.page)
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
        try:
            self.page.wait_for_timeout(300)   # SPA：给子菜单展开/页面渲染留时间，工具返回的状态才是最新的
        except Exception:
            pass

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
        """最新页面状态文本（与旧循环注入给模型的状态同构），失败时给降级说明。

        元素用短句柄标注（如 [b3] input 请输入账号）：句柄在整个执行期与 selector
        一一对应、永不复用，动作里填句柄即可——编码 agent 用「文件:行号」稳定寻址
        同理，比每步重发完整 CSS 选择器省大量 token。与上一步同页面且可交互元素
        集合一致时只回紧凑摘要；页面已被应用地图收录时省略页面文字（与地图重复）。
        """
        try:
            state = self.page.evaluate(_STATE_JS)
        except Exception as e:
            return f"页面状态读取失败：{e}"
        url = state.get("url", "")
        els = state.get("elements", [])
        for e in els:                       # 句柄注册：新 selector 领新号，见过的沿用
            if e["selector"] not in self._handles:
                self._handle_n += 1
                hnd = f"b{self._handle_n}"
                self._handles[e["selector"]] = hnd
                self._sel_by_h[hnd] = e["selector"]
        keys = tuple((e["selector"], "" if e["tag"] in ("input", "textarea") else e["text"])
                     for e in els)
        prev = self._prev_state
        if prev and prev["url"] == url and prev["keys"] == keys:
            vals = {e["selector"]: e.get("value", "") for e in els if e.get("value")}
            changed = [f"{self._handles[s]} → {v[:24]}" for s, v in vals.items()
                       if prev["vals"].get(s) != v]
            extra = ("；输入值已更新：" + "；".join(changed[:6])) if changed else ""
            return f"当前页面：{url}（可交互元素与上一步一致，无新增/消失{extra}）"
        self._prev_state = {"url": url, "keys": keys,
                            "vals": {e["selector"]: e.get("value", "") for e in els if e.get("value")}}
        els_txt = "; ".join(f"[{self._handles[e['selector']]}] {e['tag']} {e['text'] or e['value']}"
                            for e in els) or "无"
        page_txt = state.get("text", "")
        from .app_mapper import _page_key
        known = bool(url) and _page_key(url) in self.map_keys
        # 地图已收录页才省页面文字；但校验/报错信息是临时文字、地图里没有，含关键词时必须保留
        errs = ("必填", "校验", "失败", "错误", "不能为空", "未填写", "未选择")
        if page_txt and known and not any(k in page_txt for k in errs):
            page_txt = "（该页已收录应用地图，页面文字略）"
        return (f"当前页面：{url}「{state.get('title', '')}」\n"
                f"可交互元素：{els_txt}\n页面文字：{page_txt}")

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
    ctx = 0
    try:
        ctx = int(config.get("TD_MODEL_CONTEXT_SIZE") or 65536)
    except (TypeError, ValueError):
        ctx = 65536
    models = []
    for base, key, model in entries:
        kwargs = {"context_size": ctx} if ctx > 0 else {}
        models.append(OpenAIChatModel(
            credential=OpenAICredential(api_key=key, base_url=base),
            model=model,
            stream=False,               # 与旧循环一致：非流式，用量一次性拿到
            max_retries=1,              # 单档快速失败，交给回退链换档
            parameters=OpenAIChatModel.Parameters(temperature=0.1),
            **kwargs,
        ))
    return make_fallback_model(models, on_usage=on_usage)


def _thinking_safe_choice(tool_choice, tools):
    """思考模式模型兼容（B4 实测：DeepSeek 思考模式拒绝 tool_choice="none"/强制函数，400）。

    - mode="none"（上下文压缩摘要用）→ 工具与 choice 一并去掉，等价纯文本补全；
    - mode=函数名（max_iters 收尾强制结构化输出）→ 降级 auto，提示词已明确要求
      此时调用结构化输出工具；
    - 其余（None/auto）原样透传。
    返回 (tool_choice, tools)。
    """
    mode = getattr(tool_choice, "mode", None) if tool_choice else None
    if mode is None or mode == "auto":
        return tool_choice, tools
    if mode == "none":
        return None, None
    from agentscope.tool import ToolChoice
    return ToolChoice(mode="auto"), tools    # 强制函数名 → auto（模型层只认 ToolChoice 对象）


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
                    tc, tl = _thinking_safe_choice(tool_choice, tools)
                    resp = await m._call_api(m.model, messages, tools=tl,
                                             tool_choice=tc, **kw)
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
    """工具返回必须用 ToolChunk：本版本 Toolkit 的适配层只识别 ToolChunk，
    传 ToolResponse 会走 json.dumps 失败兜底 str(result)，把含 base64 的对象 repr
    整个塞进 TextBlock（实测一张截图 ≈ 13 万 token，几轮就撑爆 128k 上下文）。
    ToolChunk 的块会原样进入 ToolResultBlock，图片被 formatter 正常提升为 image_url。"""
    from agentscope.message import Base64Source, DataBlock, TextBlock, ToolResultState
    from agentscope.tool import ToolChunk
    content: list = []
    if image_b64:
        content.append(DataBlock(type="data", source=Base64Source(
            type="base64", media_type="image/jpeg", data=image_b64)))
    content.append(TextBlock(type="text", text=text))
    return ToolChunk(content=content,
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

    def _tgt(target: str) -> str:
        """元素寻址：句柄（如 b12）翻译回 selector；不是已注册句柄则原样视为 selector（兼容）。"""
        t = (target or "").strip()
        return h._sel_by_h.get(t, t)

    async def browser_click(think: str, target: str):
        """点击元素。target 填最新页面状态里方括号中的元素句柄（如 b12）；同一元素句柄在整个执行中不变。"""
        return await _run("click", think, {"selector": _tgt(target)})

    async def browser_dblclick(think: str, target: str):
        """双击元素。用于"单击只高亮、双击才选中"的列表/弹窗行（双击选行类控件）。target 填元素句柄。"""
        return await _run("dblclick", think, {"selector": _tgt(target)})

    async def browser_fill(think: str, target: str, value: str):
        """在输入框/文本域填入文字。target 填元素句柄；value 可用 ${变量} 引用可用变量。"""
        return await _run("fill", think, {"selector": _tgt(target), "value": value})

    async def browser_fill_many(think: str, fields: list):
        """批量填写多个输入框/文本域，一次调用只返回一次页面状态。fields 是对象数组：
        [{"target": "元素句柄", "value": "要填的内容"}]，最多 12 项，value 可用 ${变量}。
        只用于输入框/文本域；下拉、日期面板等仍逐个用 click。"""
        if not isinstance(fields, list) or not fields:
            return _tool_response('fields 需要是非空数组，如 [{"target":"b3","value":"100"}]')
        outs = []
        for f in fields[:12]:
            if not isinstance(f, dict) or not str(f.get("target", "")).strip():
                outs.append("失败：fields 项需要是 {target, value} 对象且 target 非空")
                continue
            ok, reason = await asyncio.wrap_future(h.dispatch(
                h.execute, "fill", think,
                {"selector": _tgt(str(f.get("target", ""))), "value": str(f.get("value", ""))}))
            outs.append(f"{'成功' if ok else '失败'}：{reason}")
            if h.stop:
                break
        if h.stop:
            return _tool_response("\n".join(outs) + f"\n执行已终止：{h.stop}", interrupted=True)
        return _tool_response("\n".join(outs) + "\n\n" + await _state_text())

    async def browser_expect_text(think: str, value: str):
        """断言页面包含指定文字，不包含则该步失败。用于验证操作结果。"""
        return await _run("expect_text", think, {"value": value})

    async def browser_click_xy(think: str, x: int, y: int):
        """按像素坐标点击（视口左上角为原点）。用于图形目标，一次点一个；多个点优先用 browser_click_xy_many。"""
        return await _run("click_xy", think, {"x": x, "y": y})

    async def browser_click_xy_many(think: str, points: list):
        """按像素坐标依次点击多个点，一次调用点完（如点选验证码的全部文字）。
        points 是坐标数组，如 [[648,218],[694,175],[561,222]]，最多 6 个，坐标以视口左上角为原点。
        用 look 看清所有目标的顺序与坐标后一次传进来，避免逐个点击的往返。"""
        if not isinstance(points, list) or not points:
            return _tool_response('points 需要是非空数组，如 [[648,218],[694,175]]')
        outs = []
        for p in points[:6]:
            try:
                x, y = int(p[0]), int(p[1])
            except (TypeError, ValueError, IndexError):
                outs.append("失败：points 项需要是 [x, y] 数字数组")
                continue
            ok, reason = await asyncio.wrap_future(h.dispatch(
                h.execute, "click_xy", think, {"x": x, "y": y}))
            outs.append(f"{'成功' if ok else '失败'}：{reason}")
            if h.stop:
                break
        if h.stop:
            return _tool_response("\n".join(outs) + f"\n执行已终止：{h.stop}", interrupted=True)
        return _tool_response("\n".join(outs) + "\n\n" + await _state_text())

    async def browser_drag(think: str, x: int, y: int, x2: int, y2: int):
        """从 (x,y) 按住拖拽到 (x2,y2)，自动分步模拟人手轨迹。用于滑块验证码：起点=手柄中心，终点=缺口位置。"""
        return await _run("drag", think, {"x": x, "y": y, "x2": x2, "y2": y2})

    async def browser_scroll(think: str, dy: int = 600):
        """滚动：dy>0 向下，dy<0 向上（像素，一次最多 2000）。滚的是鼠标指针所在的可滚动区域——
        指针悬停在列表/下拉面板上时滚的是那个列表；要滚整页先点一下页面空白处再滚动。
        元素清单里找不到需要的内容时先滚动再看最新状态。"""
        return await _run("scroll", think, {"dy": int(dy)})

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

    tools = [browser_goto, browser_click, browser_dblclick, browser_fill, browser_fill_many,
             browser_expect_text, browser_click_xy, browser_click_xy_many, browser_drag,
             browser_scroll, browser_save]
    if h.vision:
        tools.append(browser_look)
    return Toolkit(tools=[FunctionTool(f) for f in tools])


# ---------- 同步入口（门面，签名与返回结构对齐 ai_drive） ----------

def run_brain(page, goal: str, variables: dict, max_steps: int = DEFAULT_MAX_STEPS,
              run_id: str = "", shot_tag: str = "ai", on_step=None, engine: str = "",
              page_map: str = "", project_id: str = "", map_keys: set | None = None) -> dict:
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
            A._log_usage("agent-step", pt, ct, ok, model=model, run_id=run_id)
        except Exception:
            pass

    h = Harness(page, goal, variables, run_id, shot_tag, on_step, engine, page_map,
                project_id, vision=A.vision_enabled(), max_actions=max_steps,
                map_keys=map_keys)
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
    from agentscope.agent import Agent, ContextConfig, ReActConfig
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

    # token 治理（长表单执行实测 prompt 均值 ≈2.4 万/步，九成开销是历史全量重发）：
    # - trigger_ratio 0.8→0.3：压缩阈值从 ≈52k 降到 ≈20k，锯齿均值减半（压缩摘要走
    #   generate_structured_output 独立通道，不计入 llm_logs，越频繁单次越便宜）；
    # - max_image_num 5→1：_limit_context_images 在每步推理前执行，历史只留最近一张
    #   截图（每张估算 2000 token，验证码点选类执行 5 张就是 1 万/步的固定包袱）；
    # - reserve_ratio 0.1→0.06 / tool_result_limit 50k→12k：压缩后底座更矮，单条
    #   工具结果异常膨胀也有兜底（正常页面状态 ≤3k）。
    ctx_cfg = ContextConfig(trigger_ratio=0.3, reserve_ratio=0.06,
                            max_image_num=1, tool_result_limit=12000,
                            summary_schema=_SUMMARY_SCHEMA,
                            summary_template=_SUMMARY_TEMPLATE)
    agent = Agent(name="testdeck", system_prompt=_SYSTEM, model=model, toolkit=toolkit,
                  state=state, react_config=ReActConfig(max_iters=max(2, max_steps)),
                  context_config=ctx_cfg)
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
