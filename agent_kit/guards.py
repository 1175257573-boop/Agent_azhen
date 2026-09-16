"""Multi-Agent 防护：方向跑偏（off-rail）与互斥循环（ping-pong）的拦截机制。

两类失控的根因不同，防护手段也不同，**不能用同一套机制硬套**：

  方向跑偏 —— 本质是**目标遗忘**：多轮之后原始任务被挤到上下文很远处，
              模型把「手段」当成了「目标」。
              对策：目标锚定（每步重注入原始目标）+ 预算闸门（步数/时间硬上限）。

  互斥循环 —— 本质是**转移无收敛性**：状态没有单调推进，
              A 认为该 B 做、B 认为该 A 做，来回踢皮球。
              对策：跳数上限 + 环检测 + 转移白名单 + 单调推进判定。

⚠️ 最容易踩的坑：A→B→A 在两种模式下**语义完全相反**
    · Handoffs  —— 控制权交接，A→B→A 是踢皮球，**必须拦**
    · Subagents —— 主代理调子代理后收回结果，A→B→A **完全正常**
  所以 `detect_pingpong` 必须按 mode 区分判据；用同一套要么误杀正常回调，要么放过死循环。

所有模型侧钩子都用 `agent_kit.tool_hooks.dual_model` 封装（同步 + 异步成对提供），
否则在 MCP 的异步执行路径上会报 `awrap_tool_call is not available`。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable

from langchain.agents import AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage

from agent_kit.logging_conf import get_logger
from agent_kit.tool_hooks import dual_model

log = get_logger("agent.guards")

# 默认阈值。放在这里是为了让调用方一眼能看到「上限是多少」，而不是散落在各处。
DEFAULT_MAX_HOPS = 4          # 最多交接几次
DEFAULT_MAX_MODEL_CALLS = 20  # 单轮最多调用几次模型
DEFAULT_MAX_SECONDS = 180.0   # 单轮最长跑多久

# 统一收敛出口的节点名：所有防线的终点都是它
ESCALATE = "escalate"


# ---------------------------------------------------------------------------
# State：防护所需的字段
# ---------------------------------------------------------------------------
class GuardState(AgentState):
    """防护相关的状态字段（谁用谁声明，不污染全局 state）。

    必须继承 AgentState，否则图会缺 messages 等内建键。
    """

    original_goal: str        # 原始任务，全程只读，目标锚定用
    hop_count: int            # 交接跳数
    handoff_path: list[str]   # 交接路径，环检测用
    model_calls: int          # 模型调用计数
    violations: int           # 非法转移次数
    stall_count: int          # 无进展交接次数
    escalated: bool           # 是否已进入收敛


def default_guard_state(goal: str = "") -> dict:
    """防护字段的初始值，避免到处写 `.get(k, default)`。"""
    return {
        "original_goal": goal,
        "hop_count": 0,
        "handoff_path": [],
        "model_calls": 0,
        "violations": 0,
        "stall_count": 0,
        "escalated": False,
    }


# ---------------------------------------------------------------------------
# 一、方向跑偏：目标锚定
# ---------------------------------------------------------------------------
def make_goal_anchor(goal_key: str = "original_goal", name: str = "goal_anchor"):
    """每次模型调用前，把原始目标重注入到 system prompt 末尾。

    成本几乎为零（一次字符串拼接），但能显著降低「跑偏」概率——
    多轮之后原始任务早已被挤到上下文很远处，模型只记得上一轮的工具结果。

    state 里没有该字段时自动跳过，因此**对现有模式无副作用**，可放心默认挂载。
    """

    def _anchor(request: ModelRequest) -> ModelRequest:
        state = request.state or {}
        goal = state.get(goal_key) if isinstance(state, dict) else None
        if not goal:
            return request

        cur = request.system_message
        if cur is None:
            base = ""
        elif isinstance(cur, str):
            base = cur
        else:
            base = getattr(cur, "content", "") or ""

        anchor = (
            f"\n\n[任务锚定] 本次任务的唯一目标：{goal}\n"
            "若你即将执行的操作与该目标无关，请立即停止并说明原因，不要继续执行。"
        )
        return request.override(system_message=SystemMessage(content=base + anchor))

    def sync_fn(request: ModelRequest, handler) -> ModelResponse:
        return handler(_anchor(request))

    async def async_fn(request: ModelRequest, handler) -> ModelResponse:
        return await handler(_anchor(request))

    return dual_model(sync_fn, async_fn, name=name)


# ---------------------------------------------------------------------------
# 二、方向跑偏：预算闸门
# ---------------------------------------------------------------------------
def make_budget_guard(
    *,
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
    max_seconds: float | None = DEFAULT_MAX_SECONDS,
    name: str = "budget_guard",
):
    """步数 / 时间的硬闸门，超限直接短路，不再调用模型。

    和 `ModelCallLimitMiddleware` 的区别：后者是「正常收尾」，
    这里是**带着已完成的进展**停下来并说明原因——对用户更友好，也便于排查。

    注意：墙钟时间从**装配时刻**开始计时；若要按每轮计时，请在每轮开始时重新装配
    （多智能体长任务场景建议按轮重挂）。
    """
    started = time.monotonic()

    def _reached(state: dict) -> str | None:
        if state.get("model_calls", 0) >= max_model_calls:
            return f"已超出单轮最大模型调用次数（{max_model_calls} 次）"
        if max_seconds and (time.monotonic() - started) > max_seconds:
            return f"已超出单轮最大耗时（{max_seconds:.0f} 秒）"
        return None

    def _stop(reason: str) -> ModelResponse:
        log.warning("预算闸门触发：%s", reason)
        return ModelResponse(
            result=[AIMessage(content=f"[已停止] {reason}。请根据以上已完成的进展继续，或缩小任务范围后重试。")]
        )

    def sync_fn(request: ModelRequest, handler) -> ModelResponse:
        state = request.state or {}
        reason = _reached(state) if isinstance(state, dict) else None
        return _stop(reason) if reason else handler(request)

    async def async_fn(request: ModelRequest, handler) -> ModelResponse:
        state = request.state or {}
        reason = _reached(state) if isinstance(state, dict) else None
        return _stop(reason) if reason else await handler(request)

    return dual_model(sync_fn, async_fn, name=name)


# ---------------------------------------------------------------------------
# 三、互斥循环：环检测（分模式）
# ---------------------------------------------------------------------------
def detect_pingpong(
    path: list[str],
    *,
    mode: str = "handoffs",
    max_repeat: int = 2,
) -> bool:
    """判定交接路径是否已经在兜圈子。

    Args:
        path: 交接/调用路径，如 ["A", "B", "A"]
        mode: "handoffs" | "subagents"，**两种模式判据不同**，见模块 docstring
        max_repeat: handoffs 模式下同一节点出现几次算异常

    早期版本用 `path[-1] == path[-3]`，会漏判 A→B→C→A（四步环），
    已改为按出现次数判定。
    """
    if len(path) < 2:
        return False

    if mode == "handoffs":
        return Counter(path).most_common(1)[0][1] >= max_repeat

    # subagents：只拦长度为 2 的重复周期（A B A B），主调子返回是正常的
    for i in range(len(path) - 3):
        if path[i] == path[i + 2] and path[i + 1] == path[i + 3]:
            return True
    return False


# ---------------------------------------------------------------------------
# 四、互斥循环：转移白名单
# ---------------------------------------------------------------------------
def guard_transition(
    current: str,
    target: str,
    allowed: dict[str, Iterable[str]],
    *,
    violations: int = 0,
    max_violations: int = 2,
) -> tuple[str, int]:
    """把「模型随意跳转」约束成状态机。

    Returns:
        (最终去处, 累计违规次数)
        合法 → (target, 0)；非法 → (current, violations+1)，连续违规达上限则转 ESCALATE
    """
    if target in set(allowed.get(current, ())):
        return target, 0

    new_violations = violations + 1
    log.warning("非法转移 %s -> %s（第 %d 次）", current, target, new_violations)
    if new_violations >= max_violations:
        return ESCALATE, new_violations
    # 留在当前步，让模型重新决策
    return current, new_violations


# ---------------------------------------------------------------------------
# 五、互斥循环：单调推进判定
# ---------------------------------------------------------------------------
def is_progress(old: dict, new: dict, keys: Iterable[str]) -> bool:
    """判断这一次交接是否带来了实质进展（有新字段被填上）。"""
    filled_old = {k for k in keys if old.get(k)}
    filled_new = {k for k in keys if new.get(k)}
    return bool(filled_new - filled_old)


def route_after_handoff(
    state: dict,
    *,
    mode: str = "handoffs",
    max_hops: int = DEFAULT_MAX_HOPS,
    next_key: str = "next_agent",
) -> str:
    """LangGraph 条件边：综合跳数、环检测决定下一步去向。"""
    path = state.get("handoff_path", []) or []
    if state.get("hop_count", 0) >= max_hops:
        return ESCALATE
    if detect_pingpong(path, mode=mode):
        return ESCALATE
    return state.get(next_key) or ESCALATE


# ---------------------------------------------------------------------------
# 六、统一收敛出口
# ---------------------------------------------------------------------------
def escalate_message(state: dict, reason: str, partial_key: str = "partial_result") -> AIMessage:
    """收敛节点的输出：交出进展 + 打印交接路径 + 给出下一步建议。

    **不能只报错**——否则前面消耗的算力全浪费了，读者也无从排查。
    """
    path = state.get("handoff_path", []) or []
    partial = state.get(partial_key) or "无"
    path_str = " → ".join(path) if path else "（无）"
    return AIMessage(
        content=(
            f"[已停止] {reason}\n"
            f"已完成部分：{partial}\n"
            f"交接路径：{path_str}\n"
            "建议：缩小任务范围后重试，或转人工处理。"
        )
    )


def make_escalate_node(partial_key: str = "partial_result"):
    """生成 LangGraph 的收敛节点函数。

    用法：graph.add_node(ESCALATE, make_escalate_node())
    """

    def escalate(state: dict) -> dict:
        reason = "多次交接未取得进展" if state.get("hop_count", 0) >= DEFAULT_MAX_HOPS else "检测到交接循环"
        return {
            "messages": [escalate_message(state, reason, partial_key)],
            "escalated": True,
        }

    escalate.__name__ = ESCALATE
    return escalate


def bump_hop(state: dict, agent: str) -> dict:
    """每次交接时调用：累加跳数并追加路径。"""
    path = list(state.get("handoff_path", []) or [])
    path.append(agent)
    return {"hop_count": state.get("hop_count", 0) + 1, "handoff_path": path}


__all__ = [
    "DEFAULT_MAX_HOPS",
    "DEFAULT_MAX_MODEL_CALLS",
    "DEFAULT_MAX_SECONDS",
    "ESCALATE",
    "GuardState",
    "bump_hop",
    "default_guard_state",
    "detect_pingpong",
    "escalate_message",
    "guard_transition",
    "is_progress",
    "make_budget_guard",
    "make_escalate_node",
    "make_goal_anchor",
    "route_after_handoff",
]
