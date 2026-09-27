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

# 测试点验收：同一测试点的断言最多引擎执行次数（首次 + AI 复核纠正后的重试）。
# 通过只能由断言产生；AI 只能把"未通过"定性为缺陷(defect)或环境阻塞(blocked)。
MAX_VERIFY_ATTEMPTS = 3


def _max_seconds() -> int:
    """单次 AI 执行的总时长墙钟（TD_AI_MAX_SECONDS，默认 3600）：取消不打断模型推理，
    步数检查也在步间——没有墙钟时回退链 N 档 × 慢推理可把执行拖到小时级。"""
    try:
        return max(60, int(config.get("TD_AI_MAX_SECONDS") or 3600))
    except (TypeError, ValueError):
        return 3600


# ---------- 运行中插话（steering）：与取消同构的 run_id 注册表 ----------
# 文本在下一次工具结果末尾注入（不打断当前模型调用，也不插进工具调用与结果之间），
# 模型在下一个决策点看到并调整方向。借鉴编码 agent 的 steer 语义做领域适配。
_steers: dict[str, list[str]] = {}
_steers_lock = threading.Lock()


def steer(run_id: str, text: str) -> int:
    """向运行中的 AI 执行插话（如录制 AI 代劳时"停，改用手动"）。返回当前排队条数。"""
    text = (text or "").strip()
    if not text or not run_id:
        return 0
    with _steers_lock:
        _steers.setdefault(run_id, []).append(text[:500])
        return len(_steers[run_id])


def drain_steers(run_id: str) -> list[str]:
    """取走并清空该执行的待注入插话（工具包装层在每个动作结果里调用）。"""
    if not run_id:
        return []
    with _steers_lock:
        return _steers.pop(run_id, [])


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

# 测试设计形态（用例带预设测试点）的追加工作流：AI 负责"做"，引擎负责"判"
_POINTS_SYSTEM = (
    "\n\n测试点验收工作流（本用例带预设测试设计）：\n"
    "- 逐个测试点完成其「操作意图」，到达可验证状态后调用 browser_verify——断言由引擎执行，"
    "结果客观，与你的判断无关。\n"
    "- verify 未通过 ≠ 测试失败，先判断原因：页面未就绪/被弹窗遮挡/操作没到位 → 纠正状态后"
    "重新 verify（同一测试点最多 3 次）；确认是被测系统的问题（功能缺陷/数据不对）→ "
    "browser_conclude 定性 defect；环境或前置问题无法验证 → 定性 blocked。\n"
    "- 不得为了让断言通过而绕过测试意图，也不得把被测系统的问题说成 blocked。\n"
    "- 所有测试点处理完（通过或已定性）后调用 GenerateStructuredOutput 收尾；"
    "遗漏未验证的测试点会在收尾时被引擎兜底判定。"
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
                 vision: bool, max_actions: int, map_keys: set | None = None,
                 points: list | None = None, api_base: str = ""):
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
        self.points = list(points or [])    # 测试设计：[{name, intent, asserts}]，空 = 旧形态（AI 自判）
        self.api_base = api_base or ""      # expect_api 的相对路径基准（环境 Base URL）

        self.detail: list[dict] = []
        self.saved: dict[str, str] = {}
        self.pass_n = self.fail_n = 0
        self.idx = 0
        self.t0 = time.time()
        self.deadline = self.t0 + _max_seconds()   # 总时长墙钟，步间检查
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
        # 测试点验收状态：name -> {status: pending|passed|failed|blocked, attempts, results, note}
        self.point_state: dict[str, dict] = {
            p.get("name", f"测试点{i+1}"): {"status": "pending", "attempts": 0,
                                            "results": [], "note": ""}
            for i, p in enumerate(self.points)}

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

        # 总时长墙钟：模型推理期间无法中断，靠步间检查兜住整体时长
        if time.time() > self.deadline:
            self._interrupt("limit", f"超过最大执行时长（{_max_seconds() // 60} 分钟），已终止")
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

    def note_steer(self, text: str):
        """把已注入的插话记到刚执行完的步骤明细上（执行记录可见 AI 是在哪一步被改道的）。"""
        if text and self.detail:
            self.detail[-1]["steer"] = text[:200]
            self._emit()

    # ---------- 测试点验收（引擎判定；通过只能由断言产生） ----------

    def _point(self, name: str):
        return next((p for p in self.points if p.get("name") == name), None)

    def verify_point(self, name: str) -> dict:
        """引擎执行某测试点的断言（队列线程，Playwright 亲和）。返回给模型的客观结果。"""
        p = self._point(name)
        st = self.point_state.get(name)
        if p is None or st is None:
            return {"ok": False, "error": f"测试点不存在：{name}（可用：{list(self.point_state)}）"}
        if st["status"] == "passed":
            return {"ok": True, "already": True, "results": st["results"],
                    "note": "该测试点已通过"}
        if st["attempts"] >= MAX_VERIFY_ATTEMPTS:
            return {"ok": False, "exhausted": True,
                    "note": f"该测试点已达 {MAX_VERIFY_ATTEMPTS} 次验证上限，请调用 browser_conclude 定性"}
        st["attempts"] += 1
        from .assertions import verify_assertions
        http = None
        if any((a.get("type") == "expect_api") for a in p.get("asserts") or []):
            import httpx
            from .runner import verify_tls
            http = httpx.Client(timeout=15, verify=verify_tls(), trust_env=False)
        try:
            results = verify_assertions(p.get("asserts") or [], page=self.page,
                                        variables=self.variables, api_base=self.api_base,
                                        http=http)
        finally:
            if http is not None:
                http.close()
        st["results"] = results
        ok = bool(results) and all(r["ok"] for r in results)
        reason = "；".join(("✓" if r["ok"] else "✗") + r["assert"] +
                           (f"（{r['reason']}）" if r["reason"] else "") for r in results) or "（无断言）"
        self.idx += 1
        entry = {"idx": self.idx, "action": "verify", "target": name,
                 "pass": ok, "reason": reason[:500], "ms": 0,
                 "url": self.page.url if hasattr(self.page, "url") else "",
                 "selector": "", "value": ""}
        if ok:
            st["status"] = "passed"
            self.pass_n += 1
        else:
            st["status"] = "failed"
            self.fail_n += 1
        self.detail.append(entry)
        self._emit()
        return {"ok": ok, "results": results, "attempts": st["attempts"]}

    def conclude_point(self, name: str, verdict: str, reason: str) -> dict:
        """AI 对未通过的测试点定性：defect=被测系统缺陷；blocked=环境/前置问题导致无法验证。
        只影响展示与失败原因，不影响判定不变量：passed 永远来自引擎断言。"""
        p = self._point(name)
        st = self.point_state.get(name)
        if p is None or st is None:
            return {"ok": False, "error": f"测试点不存在：{name}"}
        if st["status"] == "passed":
            return {"ok": False, "error": "该测试点已通过，无需定性"}
        verdict = (verdict or "").strip().lower()
        if verdict not in ("defect", "blocked"):
            return {"ok": False, "error": "verdict 只能是 defect（被测系统缺陷）或 blocked（环境/前置问题）"}
        st["status"] = "failed" if verdict == "defect" else "blocked"
        st["note"] = (reason or "")[:300]
        self.idx += 1
        self.detail.append({"idx": self.idx, "action": "conclude", "target": name,
                            "pass": False, "ms": 0, "url": "", "selector": "", "value": "",
                            "reason": f"AI 定性：{'被测系统缺陷' if verdict == 'defect' else '环境/前置问题'}"
                                      f"——{st['note'] or '（未说明）'}"})
        self._emit()
        return {"ok": True}

    def finalize_points(self):
        """执行收尾兜底：模型没验证/没定性的测试点，引擎补跑一次断言（判定不依赖模型自觉）。"""
        from .assertions import verify_assertions
        for name, st in self.point_state.items():
            if st["status"] in ("passed", "blocked") or (
                    st["status"] == "failed" and st["attempts"] > 0):
                continue   # 已通过 / 已定性 / 已有失败事实：不再补跑
            p = self._point(name)
            http = None
            if any((a.get("type") == "expect_api") for a in p.get("asserts") or []):
                import httpx
                from .runner import verify_tls
                http = httpx.Client(timeout=15, verify=verify_tls(), trust_env=False)
            try:
                results = verify_assertions(p.get("asserts") or [], page=self.page,
                                            variables=self.variables, api_base=self.api_base,
                                            http=http)
            except Exception as e:
                results = [{"ok": False, "assert": "兜底验证", "reason": str(e)[:200]}]
            finally:
                if http is not None:
                    http.close()
            st["results"] = results
            st["attempts"] += 1
            ok = bool(results) and all(r["ok"] for r in results)
            reason = "；".join(("✓" if r["ok"] else "✗") + r["assert"] +
                               (f"（{r['reason']}）" if r["reason"] else "") for r in results) or "（无断言）"
            st["status"] = "passed" if ok else "failed"
            if not ok:
                st["note"] = "模型未验证，收尾兜底判定" if st["attempts"] == 1 else st["note"]
            self.idx += 1
            self.detail.append({"idx": self.idx, "action": "verify", "target": name,
                                "pass": ok, "reason": ("收尾兜底：" + reason)[:500], "ms": 0,
                                "url": "", "selector": "", "value": ""})
            if ok:
                self.pass_n += 1
            else:
                self.fail_n += 1
        if self.point_state:
            self._emit()

    def points_out(self) -> list[dict]:
        return [{"name": n, "status": s["status"], "attempts": s["attempts"],
                 "asserts": s["results"], "note": s["note"]}
                for n, s in self.point_state.items()]

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
        elif self.stop:
            summary, status = self.stop, "failed"
        elif self.idx >= self.max_actions:
            summary, status = f"超过最大步数 {self.max_actions}", "failed"
        else:
            # 无结论且未触发中断：模型跑完 reply 但没调结构化输出工具，别误报成"超过最大步数"
            summary, status = "模型未给出结论（未调用结构化输出工具）", "failed"
        if status == "failed" and summary != "用户取消":
            hint = _model_hint()
            if hint:
                summary += f"；建议：{hint}"
        out = {"status": status, "pass_n": self.pass_n, "fail_n": self.fail_n,
               "duration": round(time.time() - self.t0, 2), "detail": self.detail,
               "saved": self.saved, "summary": summary,
               "brain": "agentscope", "usage": list(self.usage)}
        if self.point_state:
            # 测试设计形态：判定权在引擎断言。done(passed) 降级为"操作完成"声明——
            # 状态与结论以测试点为准，通过只能由断言产生（AI 定性只解释失败，不能改判通过）
            pts = self.points_out()
            n_ok = sum(1 for p in pts if p["status"] == "passed")
            out["points"] = pts
            out["status"] = "passed" if n_ok == len(pts) else "failed"
            bad = [f"{p['name']}（{'环境阻塞' if p['status'] == 'blocked' else '未通过'}）"
                   for p in pts if p["status"] != "passed"]
            verdict_txt = f"模型操作结论：{summary[:120]}。" if summary else ""
            out["summary"] = (f"测试点 {n_ok}/{len(pts)} 通过；未过：{'、'.join(bad) or '无'}。"
                              + verdict_txt)[:400]
        return out


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
    try:
        model_timeout = max(30, int(config.get("TD_AI_MODEL_TIMEOUT") or 300))
    except (TypeError, ValueError):
        model_timeout = 300   # 单次模型请求超时（openai SDK 默认 600s，回退链多档叠加会拖到小时级）
    models = []
    for base, key, model in entries:
        kwargs = {"context_size": ctx} if ctx > 0 else {}
        models.append(OpenAIChatModel(
            credential=OpenAICredential(api_key=key, base_url=base),
            model=model,
            stream=False,               # 与旧循环一致：非流式，用量一次性拿到
            max_retries=1,              # 单档快速失败，交给回退链换档
            parameters=OpenAIChatModel.Parameters(temperature=0.1),
            client_kwargs={"timeout": model_timeout},
            **kwargs,
        ))
    return make_fallback_model(models, on_usage=on_usage)


def _thinking_safe_choice(tool_choice, tools):
    """思考模式模型兼容（B4 实测：DeepSeek 思考模式拒绝 tool_choice="none"，400）。

    - mode="none"（上下文压缩摘要用）→ 工具与 choice 一并去掉，等价纯文本补全；
    - 其余（None/auto/强制函数名）原样透传——强制结构化输出的降级交给
      AgentScope generate_structured_output 的策略梯队（forced → auto → no_think
      → none，触发条件是 BadRequestError；FallbackChatModel 已暴露该异常集）。
      之前在这里无条件把强制降级成 auto，会让非思考模型的 done 结论失去强制力。
    返回 (tool_choice, tools)。
    """
    mode = getattr(tool_choice, "mode", None) if tool_choice else None
    if mode == "none":
        return None, None
    return tool_choice, tools


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

        # 基类这两个方法默认返回空 tuple：不覆盖则重试与"强制结构化输出降级梯队"
        # （forced → auto → no_think → none，针对思考模式模型 400 拒绝 forced）全部失效
        @staticmethod
        def _get_retryable_exceptions() -> tuple:
            return OpenAIChatModel._get_retryable_exceptions()

        @staticmethod
        def _get_structured_output_fallback_exceptions() -> tuple:
            return OpenAIChatModel._get_structured_output_fallback_exceptions()

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

    def _steer_note() -> str:
        """取走排队中的用户插话，拼进本次工具结果（模型在下一个决策点看到并改道）。"""
        ss = drain_steers(h.run_id)
        if not ss:
            return ""
        text = "；".join(ss)
        h.note_steer(text)
        return f"\n\n【用户中途指示（优先于原目标执行）】{text}"

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
        return _tool_response(f"{text}\n\n{state}" + _steer_note())

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
        return _tool_response("\n".join(outs) + "\n\n" + await _state_text() + _steer_note())

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
        return _tool_response("\n".join(outs) + "\n\n" + await _state_text() + _steer_note())

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

    async def browser_verify(think: str, point: str):
        """验证测试点：引擎执行该测试点预设的断言（客观判定，结果不依赖你的判断）。
        到达该点的可验证状态后调用；同一测试点最多验证 3 次。"""
        if not h.point_state:
            return _tool_response("本用例没有预设测试点，直接完成目标即可")
        r = await asyncio.wrap_future(h.dispatch(h.verify_point, point))
        if r.get("error"):
            return _tool_response(r["error"])
        if r.get("already"):
            return _tool_response(f"测试点「{point}」已通过，无需重复验证")
        if r.get("exhausted"):
            return _tool_response(f"测试点「{point}」已达验证上限，请调用 browser_conclude 定性")
        lines = ["；".join((("✓" if a["ok"] else "✗") + a["assert"] +
                            (f"（{a['reason']}）" if a["reason"] else "")) for a in r["results"])]
        head = ("验证通过 ✓" if r["ok"] else
                f"断言未通过（客观事实，还不是最终判定）：\n{lines[0]}")
        guide = "" if r["ok"] else (
            "\n请判断未通过的原因：若是页面未就绪/被遮挡/你的操作未到达正确状态，纠正后重新调用本工具；"
            "若确认是被测系统的问题，调用 browser_conclude(point, verdict=\"defect\") 定性；"
            "若是环境/前置数据问题无法验证，定性 \"blocked\"。不要为了通过而绕过测试意图。")
        return _tool_response(f"{head}（第 {r['attempts']}/{MAX_VERIFY_ATTEMPTS} 次验证）"
                              f"{guide}\n\n" + await _state_text() + _steer_note())

    async def browser_conclude(think: str, point: str, verdict: str, reason: str):
        """对未通过的测试点给出定性结论。verdict 只能是：
        defect（被测系统缺陷：功能不符合预期/数据显示错误）或
        blocked（环境或前置问题导致无法验证：服务不可用/账号被锁/前置数据缺失）。
        reason 一句话说清依据。注意：已通过的测试点不能改判。"""
        if not h.point_state:
            return _tool_response("本用例没有预设测试点")
        r = await asyncio.wrap_future(h.dispatch(h.conclude_point, point, verdict, reason))
        if r.get("error"):
            return _tool_response(r["error"])
        return _tool_response(f"已记录对「{point}」的定性：{verdict}——{reason[:200]}。继续处理其余测试点，"
                              "全部处理完后调用 GenerateStructuredOutput 收尾。")

    async def browser_look(think: str):
        """拍摄当前视口截图并返回（附最新页面状态）。需要看图定位（验证码/图形元素）时使用。"""
        fut = h.dispatch(h.shot_b64)
        img = await asyncio.wrap_future(fut)
        state = await _state_text()
        if not img:
            return _tool_response(f"截图不可用（当前浏览器引擎不支持），依据文字状态操作。\n{state}" + _steer_note())
        return _tool_response(state + _steer_note(), image_b64=img)

    tools = [browser_goto, browser_click, browser_dblclick, browser_fill, browser_fill_many,
             browser_expect_text, browser_click_xy, browser_click_xy_many, browser_drag,
             browser_scroll, browser_save]
    if h.points:
        tools += [browser_verify, browser_conclude]
    if h.vision:
        tools.append(browser_look)
    return Toolkit(tools=[FunctionTool(f) for f in tools])


# ---------- 同步入口（门面，签名与返回结构对齐 ai_drive） ----------

def run_brain(page, goal: str, variables: dict, max_steps: int = DEFAULT_MAX_STEPS,
              run_id: str = "", shot_tag: str = "ai", on_step=None, engine: str = "",
              page_map: str = "", project_id: str = "", map_keys: set | None = None,
              resume: dict | None = None, points: list | None = None,
              api_base: str = "") -> dict:
    """AgentScope 驱动的同步入口。须在执行队列线程调用（Playwright 线程亲和性）。

    返回结构与 ai_drive 完全一致：{status, pass_n, fail_n, duration, detail, saved, summary}，
    额外带 brain="agentscope" 与 usage（供 llm_logs 记账）。
    resume：断点续跑上下文（{start_url, done_summary, saved}）——注入"已完成步骤/变量，
    从断点继续"的先验，避免长执行失败后从零重来（浏览器会话与登录态需重建）。
    """
    if not A.llm_available():
        return {"status": "failed", "pass_n": 0, "fail_n": 1, "duration": 0.0,
                "detail": [{"idx": 1, "action": "error", "target": "", "pass": False,
                            "reason": "AI 测试需要配置大模型：请在「模型配置」页添加并启用，"
                                      "或在 .env 配置 TD_LLM_BASE_URL / TD_LLM_KEY", "ms": 0}],
                "saved": {}, "summary": "未配置 LLM"}

    resume = resume or {}
    merged_vars = dict(variables or {})
    merged_vars.update(resume.get("saved") or {})   # 断点前 save 的业务单号等变量直接可用

    def _on_usage(model, pt, ct, ok):
        try:
            A._log_usage("agent-step", pt, ct, ok, model=model, run_id=run_id)
        except Exception:
            pass

    h = Harness(page, goal, merged_vars, run_id, shot_tag, on_step, engine, page_map,
                project_id, vision=A.vision_enabled(), max_actions=max_steps,
                map_keys=map_keys, points=points, api_base=api_base)
    result_box: dict = {}

    def _agent_thread_main():
        try:
            asyncio.run(_agent_main(h, goal, merged_vars, max_steps, result_box, _on_usage,
                                    resume))
        except BaseException as e:   # 异常也要让队列线程的 pump 退出
            result_box.setdefault("error", f"{type(e).__name__}: {e}"[:300])
        finally:
            h.calls.put((None, None, None))   # 结束哨兵

    t = threading.Thread(target=_agent_thread_main, daemon=True,
                         name=f"brain-{run_id or 'ai'}")
    t.start()
    h.pump()          # 队列线程：泵送 Playwright 调用直到 agent 结束
    t.join()
    drain_steers(run_id)   # 执行已结束：未消费的插话直接丢弃，防止注册表泄漏
    h.finalize_points()    # 测试点兜底判定（队列线程，Playwright 亲和）：模型没验证的点引擎补跑断言

    if "error" in result_box:
        h._interrupt("error", f"Agent 执行异常：{result_box['error']}")
    return h.result(result_box.get("verdict"))   # 队列线程组装（done 截图线程亲和）


async def _agent_main(h: Harness, goal: str, variables: dict, max_steps: int,
                      result_box: dict, on_usage, resume: dict | None = None):
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
    if h.points:
        allowed += ["browser_verify", "browser_conclude"]
    perm = PermissionContext(mode=PermissionMode.DONT_ASK)
    for name in allowed:
        perm.allow_rules[name] = [PermissionRule(
            tool_name=name, rule_content=None,
            behavior=PermissionBehavior.ALLOW, source="testdeck")]
    state = AgentState(permission_context=perm)

    var_txt = "; ".join(f"${k}={v}" for k, v in (variables or {}).items()) or "无"
    prompt = f"测试目标：{goal}\n可用变量：{var_txt}"
    if h.points:
        lines = []
        for i, p in enumerate(h.points, 1):
            asserts = "；".join(
                f"{a.get('type')}(" + ", ".join(f"{k}={v}" for k, v in a.items() if k != "type") + ")"
                for a in p.get("asserts") or [])
            lines.append(f"{i}. {p.get('name', '')}｜操作意图：{p.get('intent', '') or '（按目标自行操作）'}"
                         f"｜验收断言：{asserts or '（无）'}")
        prompt += "\n\n本次测试的测试点（逐点执行并验证，验收断言由引擎执行）：\n" + "\n".join(lines)
    if resume and (resume.get("done_summary") or resume.get("saved")):
        saved_txt = "; ".join(f"${k}={v}" for k, v in (resume.get("saved") or {}).items()) or "无"
        prompt += (
            f"\n\n【断点续跑】这是从中断处的继续执行，浏览器与登录态已重置。此前已完成：\n"
            f"{(resume.get('done_summary') or '（无记录）')[:2000]}\n已保存变量：{saved_txt}\n"
            "要求：不要重复已完成的业务动作（已创建的单据/已提交的表单不要重建，单号在变量里）；"
            "若后续步骤需要登录态，先重新登录；从中断处继续完成剩余目标。")
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
    system = _SYSTEM + (_POINTS_SYSTEM if h.points else "")
    agent = Agent(name="testdeck", system_prompt=system, model=model, toolkit=toolkit,
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
