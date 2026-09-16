"""Multi-Agent 防护的离线演示：方向跑偏 + 互斥循环。

零 API Key、零网络，直接 `python examples/guards_demo.py` 就能跑完。
演示三件事：
  1. 目标锚定       —— 原始目标如何被重注入回 system prompt
  2. 预算闸门       —— 超过步数上限后如何带着进展停下来
  3. 环检测 + 收敛  —— A↔B 踢皮球时，状态机如何把它拦下来并交出已完成部分
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许 `python examples/guards_demo.py` 直接跑（与 examples/memory_e2e.py 同一套约定）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import operator
from typing import Annotated, TypedDict

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agent_kit.guards import (
    ESCALATE,
    bump_hop,
    make_budget_guard,
    make_escalate_node,
    make_goal_anchor,
    route_after_handoff,
)
from agent_kit.multi_agent.handoffs import ALLOWED_TRANSITIONS, safe_next_step

GOAL = "帮客户办理一台笔记本的保修退款"


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _req(state: dict) -> ModelRequest:
    """构造一个最小 ModelRequest——就是中间件实际拿到的那个对象。"""
    return ModelRequest(
        model="fake",
        messages=[],
        system_message=SystemMessage(content="你是客服助手。"),
        state=state,
        tools=[],
    )


# ---------------------------------------------------------------------------
# 一、方向跑偏：目标锚定
# ---------------------------------------------------------------------------
def demo_goal_anchor() -> None:
    _banner("1 · 方向跑偏 —— 目标锚定（每次调用模型前重注入原始目标）")

    anchor = make_goal_anchor()
    out = anchor.wrap_model_call(_req({"original_goal": GOAL}), lambda r: r)
    print("注入前的 system prompt：\n  你是客服助手。")
    print("\n注入后的 system prompt：")
    for line in out.system_message.content.strip().splitlines():
        print(f"  {line}")

    print("\n若 state 里没有目标（现有模式都不带），中间件原样透传：")
    noop = anchor.wrap_model_call(_req({}), lambda r: r)
    print(f"  {noop.system_message.content}")


# ---------------------------------------------------------------------------
# 二、方向跑偏：预算闸门
# ---------------------------------------------------------------------------
def demo_budget_guard() -> None:
    _banner("2 · 方向跑偏 —— 预算闸门（步数 / 时间硬上限）")

    guard = make_budget_guard(max_model_calls=5, max_seconds=None)
    print(f"调用次数 1（未超限）：{guard.wrap_model_call(_req({'model_calls': 1}), lambda r: '正常放行')}")
    stopped = guard.wrap_model_call(_req({"model_calls": 7}), lambda r: "不应走到这里")
    print(f"调用次数 7（已超限）：{stopped.result[0].content}")


# ---------------------------------------------------------------------------
# 三、互斥循环：白名单 + 环检测 + 收敛
# ---------------------------------------------------------------------------
class PingPongState(TypedDict):
    messages: Annotated[list, operator.add]
    hop_count: int
    handoff_path: list
    next_agent: str
    partial_result: str
    escalated: bool


def _make_node(name: str, target: str, note: str):
    """一个「只会把球踢回去」的代理节点。"""

    def node(state: dict) -> dict:
        upd = dict(bump_hop(state, name))
        upd["messages"] = [AIMessage(content=f"[{name}] {note}")]
        upd["next_agent"] = target
        upd["partial_result"] = state.get("partial_result") or note
        return upd

    node.__name__ = name
    return node


def _route(state: dict) -> str:
    return route_after_handoff(state, max_hops=4, mode="handoffs")


def demo_pingpong() -> None:
    _banner("3 · 互斥循环 —— A↔B 踢皮球的拦截与收敛")

    graph = StateGraph(PingPongState)
    graph.add_node("agent_a", _make_node("agent_a", "agent_b", "我只负责登记，判定归 agent_b"))
    graph.add_node("agent_b", _make_node("agent_b", "agent_a", "我只负责判定，登记归 agent_a"))
    graph.add_node(ESCALATE, make_escalate_node())

    graph.add_edge(START, "agent_a")
    branch = {"agent_a": "agent_a", "agent_b": "agent_b", ESCALATE: ESCALATE}
    graph.add_conditional_edges("agent_a", _route, branch)
    graph.add_conditional_edges("agent_b", _route, branch)
    graph.add_edge(ESCALATE, END)

    app = graph.compile()
    result = app.invoke(
        {
            "messages": [HumanMessage(content=GOAL)],
            "hop_count": 0,
            "handoff_path": [],
            "next_agent": "",
            "partial_result": "",
            "escalated": False,
        }
    )

    for m in result["messages"]:
        if isinstance(m, HumanMessage):
            continue
        print(f"  {m.content}")

    print(f"\n  最终跳数：{result['hop_count']}    已收敛：{result['escalated']}")

    _banner("附 · 转移白名单：把「模型随意跳转」约束成状态机")
    for current, targets in ALLOWED_TRANSITIONS.items():
        print(f"  {current:10s} -> {sorted(targets)}")
    print(f"\n  合法 collect→classify    ：{safe_next_step('collect', 'classify')}")
    print(f"  非法 collect→resolve（跨步）：{safe_next_step('collect', 'resolve')}")


def main() -> None:
    demo_goal_anchor()
    demo_budget_guard()
    demo_pingpong()
    print("\n" + "=" * 78)
    print("  演示结束：三类防线均已在 agent_kit/guards.py 落地，可直接在业务图里复用。")
    print("=" * 78)


if __name__ == "__main__":
    main()
