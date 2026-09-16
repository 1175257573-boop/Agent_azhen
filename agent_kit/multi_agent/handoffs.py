"""Handoffs：用「工具返回 Command」实现流程交接。

和 Subagents 的区别：
  Subagents —— 主代理把子任务**外包**出去，结果回到主代理手里，控制权没转移
  Handoffs  —— 控制权**交接**：A 完成后说「现在归 B 管」，后续由 B 的提示词/工具接管

实现思路（单代理 + 状态机）：
  1. state 里存 current_step
  2. 中间件按 current_step 换 system_prompt 与可见工具
  3. 每个步骤的工具返回 Command，更新 current_step（交接）

多子图版本（跨代理交接）用 Command(goto=..., update=..., graph=Command.PARENT)，
本文件末尾给出了对照实现。
"""

from __future__ import annotations

from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command

from agent_kit.state import SupportState
from agent_kit.tool_hooks import dual_model

# ---------------------------------------------------------------------------
# 步骤配置：每一步一套提示词 + 一套工具名
# ---------------------------------------------------------------------------
STEP_CONFIG: dict[str, dict[str, Any]] = {
    "collect": {
        "prompt": "你正在收集客户的保修信息。请询问并确认：产品型号、购买日期、是否在保修期内。",
        "tools": ["record_warranty_status"],
        "next": "classify",
    },
    "classify": {
        "prompt": "保修状态已记录为 {warranty_status}。现在请引导客户描述故障现象，并判断问题类别。",
        "tools": ["classify_issue"],
        "next": "resolve",
    },
    "resolve": {
        "prompt": "问题类别是 {issue_category}。请给出处理方案；若需人工，说明为何。",
        "tools": ["propose_resolution"],
        "next": "__end__",
    },
}

# ---------------------------------------------------------------------------
# 转移白名单：把「模型随意跳转」约束成状态机
# ---------------------------------------------------------------------------
# 当前三个步骤工具把 next 写死在 Command 返回值里，模型无法通过参数跳错步；
# 这张表的价值在于：若将来改成「模型指定 target」的派发模式（类似 subagents 的
# single dispatch），可直接用 safe_next_step 校验，业务代码不用改。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "collect": {"classify", "__end__"},
    "classify": {"resolve", "collect"},  # 允许回退一次以补充信息
    "resolve": {"__end__"},
}


def safe_next_step(current: str, target: str, *, violations: int = 0) -> tuple[str, int]:
    """校验一次步骤转移，非法则留在当前步让模型重新决策。

    Returns:
        (最终去处, 累计违规次数)；连续违规达上限会转到 ESCALATE 收敛节点。
    """
    from agent_kit.guards import guard_transition

    return guard_transition(current, target, ALLOWED_TRANSITIONS, violations=violations)


def _apply_step_config(request: ModelRequest) -> ModelRequest:
    """按当前步骤切换提示词与工具——这就是「交接」的落地方式。"""
    state = request.state or {}
    step = state.get("current_step", "collect") if isinstance(state, dict) else "collect"
    cfg = STEP_CONFIG.get(step, STEP_CONFIG["collect"])

    try:
        prompt = cfg["prompt"].format(**state) if isinstance(state, dict) else cfg["prompt"]
    except (KeyError, IndexError):
        prompt = cfg["prompt"]

    allowed = set(cfg.get("tools", []))
    visible = [t for t in request.tools if getattr(t, "name", "") in allowed] or list(request.tools)

    # 注意：用 system_message 而非 system_prompt，后者在 1.4 已标记 deprecated
    return request.override(system_message=SystemMessage(content=prompt), tools=visible)


def apply_step_config(request: ModelRequest, handler):
    return handler(_apply_step_config(request))


async def aapply_step_config(request: ModelRequest, handler):
    return await handler(_apply_step_config(request))


apply_step_config = dual_model(apply_step_config, aapply_step_config,
                               name="apply_step_config", state_schema=SupportState)


# ---------------------------------------------------------------------------
# 三个步骤工具：每个都通过 Command 交接给下一步
# ---------------------------------------------------------------------------
@tool
def record_warranty_status(
    status: Literal["in_warranty", "out_of_warranty"],
    runtime: ToolRuntime,
) -> Command:
    """记录保修状态，然后交接给「问题分类」步骤。

    Args:
        status: in_warranty（在保）或 out_of_warranty（过保）
    """
    return Command(
        update={
            "messages": [
                ToolMessage(content=f"保修状态已记录为：{status}", tool_call_id=runtime.tool_call_id)
            ],
            "warranty_status": status,
            "current_step": "classify",
        }
    )


@tool
def classify_issue(category: Literal["hardware", "software", "billing", "other"], runtime: ToolRuntime) -> Command:
    """记录问题类别，然后交接给「给出方案」步骤。

    Args:
        category: 问题类别
    """
    return Command(
        update={
            "messages": [
                ToolMessage(content=f"问题类别已记录为：{category}", tool_call_id=runtime.tool_call_id)
            ],
            "issue_category": category,
            "current_step": "resolve",
        }
    )


@tool
def propose_resolution(plan: str, need_human: bool, runtime: ToolRuntime) -> Command:
    """给出处理方案并结束流程。

    Args:
        plan: 处理方案
        need_human: 是否需要转人工
    """
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=f"方案已给出：{plan}；转人工={need_human}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
            "current_step": "__end__",
        }
    )


def build_handoff_agent(model: Any, checkpointer: Any = None, extra_middleware: list | None = None):
    """组装一个 Handoffs 工作流代理。"""
    middleware = [apply_step_config, *(extra_middleware or [])]
    return create_agent(
        model=model,
        tools=[record_warranty_status, classify_issue, propose_resolution],
        state_schema=SupportState,
        middleware=middleware,
        checkpointer=checkpointer,
        name="support_handoff_agent",
    )


# ---------------------------------------------------------------------------
# 对照：多子图版本的交接（跨代理），供进阶参考
# ---------------------------------------------------------------------------
def make_transfer_tool(target_agent: str, description: str):
    """跨代理交接工具：返回 Command(goto=..., graph=Command.PARENT)。

    注意：消息必须成对（AI 消息 + ToolMessage），否则父图状态会不一致。
    """
    from langchain_core.messages import AIMessage

    @tool(f"transfer_to_{target_agent}", description=description)
    def transfer(runtime: ToolRuntime) -> Command:
        """把会话交接给另一个代理。

        Returns:
            Command: 跳转到目标代理
        """
        last_ai = None
        for m in reversed(runtime.state.get("messages", []) if isinstance(runtime.state, dict) else []):
            if isinstance(m, AIMessage):
                last_ai = m
                break
        content = f"已转接给 {target_agent}。"
        transfer_message = ToolMessage(content=content, tool_call_id=runtime.tool_call_id)

        updates: dict[str, Any] = {
            "active_agent": target_agent,
            "messages": ([last_ai, transfer_message] if last_ai else [transfer_message]),
        }
        return Command(goto=target_agent, update=updates, graph=Command.PARENT)

    return transfer
