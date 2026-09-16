"""钉住 Multi-Agent 防护（agent_kit/guards.py）的行为。

这些用例防的是**回归**：
  · 环检测一旦改错，两种模式会同时出问题（误杀正常回调 / 放过死循环）
  · 双模钩子少一个，MCP 异步路径会报 awrap_tool_call is not available
"""

from __future__ import annotations

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from agent_kit.guards import (
    DEFAULT_MAX_HOPS,
    ESCALATE,
    bump_hop,
    default_guard_state,
    detect_pingpong,
    escalate_message,
    guard_transition,
    is_progress,
    make_budget_guard,
    make_goal_anchor,
    route_after_handoff,
)
from agent_kit.multi_agent.handoffs import ALLOWED_TRANSITIONS, safe_next_step


def _req(state: dict | None = None, system: str = "你是助手。") -> ModelRequest:
    return ModelRequest(
        model="fake",
        messages=[],
        system_message=SystemMessage(content=system),
        state=state or {},
        tools=[],
    )


# ---------------------------------------------------------------------------
# 环检测：两种模式判据不同
# ---------------------------------------------------------------------------
def test_pingpong_handoffs_catches_two_and_four_step_loops():
    """Handoffs 模式：A→B→A 与 A→B→C→A 都必须拦住。"""
    assert detect_pingpong(["A", "B", "A"], mode="handoffs") is True
    assert detect_pingpong(["A", "B", "C", "A"], mode="handoffs") is True


def test_pingpong_handoffs_allows_normal_forward_path():
    assert detect_pingpong(["A"], mode="handoffs") is False
    assert detect_pingpong(["A", "B"], mode="handoffs") is False
    assert detect_pingpong(["A", "B", "C"], mode="handoffs") is False


def test_pingpong_subagents_allows_main_calling_sub():
    """主代理调子代理后收回结果是正常的，不能误杀。"""
    assert detect_pingpong(["main", "sub"], mode="subagents") is False
    assert detect_pingpong(["main", "sub", "main"], mode="subagents") is False


def test_pingpong_subagents_catches_repeating_cycle():
    assert detect_pingpong(["A", "B", "A", "B"], mode="subagents") is True
    assert detect_pingpong(["A", "B", "A", "B", "A"], mode="subagents") is True


# ---------------------------------------------------------------------------
# 转移白名单
# ---------------------------------------------------------------------------
def test_guard_transition_allows_legal():
    target, violations = guard_transition("collect", "classify", ALLOWED_TRANSITIONS)
    assert target == "classify"
    assert violations == 0


def test_guard_transition_blocks_illegal_and_stays():
    """非法转移：留在当前步，违规计数 +1。"""
    target, violations = guard_transition("collect", "resolve", ALLOWED_TRANSITIONS, violations=0)
    assert target == "collect"
    assert violations == 1


def test_guard_transition_escalates_after_repeated_violations():
    target, violations = guard_transition("resolve", "collect", ALLOWED_TRANSITIONS, violations=1)
    assert target == ESCALATE
    assert violations == 2


def test_safe_next_step_mirrors_whitelist():
    assert safe_next_step("collect", "classify")[0] == "classify"
    assert safe_next_step("collect", "resolve")[0] == "collect"


# ---------------------------------------------------------------------------
# 预算闸门
# ---------------------------------------------------------------------------
def test_budget_guard_short_circuits_when_over_limit():
    guard = make_budget_guard(max_model_calls=3, max_seconds=None)
    fn = guard._sync_fn
    resp = fn(_req({"model_calls": 99}), lambda r: "不应走到这里")
    assert resp.result[0].content.startswith("[已停止]")


def test_budget_guard_passes_through_when_under_limit():
    guard = make_budget_guard(max_model_calls=3, max_seconds=None)
    fn = guard._sync_fn
    assert fn(_req({"model_calls": 1}), lambda r: "正常放行") == "正常放行"


def test_budget_guard_dual_mode_present():
    """双模必须成对，否则 MCP 异步路径会炸。"""
    guard = make_budget_guard()
    assert hasattr(guard, "wrap_model_call")
    assert hasattr(guard, "awrap_model_call")


# ---------------------------------------------------------------------------
# 目标锚定
# ---------------------------------------------------------------------------
def test_goal_anchor_appends_goal_to_system_message():
    anchor = make_goal_anchor()
    fn = anchor._sync_fn
    out = fn(_req({"original_goal": "查询北京天气"}), lambda r: r)
    assert "查询北京天气" in out.system_message.content
    assert "你是助手。" in out.system_message.content  # 原 prompt 不能被覆盖掉


def test_goal_anchor_noop_without_goal():
    """state 里没有目标时必须原样透传——这是能默认全局挂载的前提。"""
    anchor = make_goal_anchor()
    fn = anchor._sync_fn
    out = fn(_req({}), lambda r: r)
    assert out.system_message.content == "你是助手。"


def test_goal_anchor_dual_mode_present():
    anchor = make_goal_anchor()
    assert hasattr(anchor, "wrap_model_call")
    assert hasattr(anchor, "awrap_model_call")


# ---------------------------------------------------------------------------
# 收敛出口与状态推进
# ---------------------------------------------------------------------------
def test_escalate_message_keeps_progress_and_path():
    msg = escalate_message({"handoff_path": ["A", "B", "A"], "partial_result": "已收集型号"}, "测试原因")
    assert "测试原因" in msg.content
    assert "A → B → A" in msg.content          # 路径用于排查
    assert "已收集型号" in msg.content          # 进展不能丢
    assert "建议" in msg.content                 # 要给下一步


def test_bump_hop_accumulates():
    s = default_guard_state("goal")
    s = {**s, **bump_hop(s, "A")}
    s = {**s, **bump_hop(s, "B")}
    assert s["hop_count"] == 2
    assert s["handoff_path"] == ["A", "B"]


def test_route_after_handoff_escalates_on_max_hops():
    state = {"hop_count": DEFAULT_MAX_HOPS, "handoff_path": ["A", "B"], "next_agent": "C"}
    assert route_after_handoff(state) == ESCALATE


def test_route_after_handoff_escalates_on_pingpong():
    state = {"hop_count": 2, "handoff_path": ["A", "B", "A"], "next_agent": "B"}
    assert route_after_handoff(state) == ESCALATE


def test_route_after_handoff_returns_next_when_healthy():
    state = {"hop_count": 1, "handoff_path": ["A"], "next_agent": "B"}
    assert route_after_handoff(state) == "B"


def test_is_progress_detects_new_filled_field():
    keys = {"warranty_status", "issue_category"}
    assert is_progress({"warranty_status": "in"}, {"warranty_status": "in", "issue_category": "hw"}, keys) is True
    assert is_progress({"warranty_status": "in"}, {"warranty_status": "in"}, keys) is False
