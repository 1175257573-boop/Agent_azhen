"""Agent 组装：把模型、工具、中间件、记忆、结构化输出拼成一个对象。

LangChain 1.x 的标准入口只有一个 —— `create_agent`。
其余工作全部由`它通过 LangGraph 图编排完成（返回 CompiledStateGraph）：
    model → tool_calls? → tools → model → ... → END
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from agent_kit import memory as mem
from agent_kit.config import AgentSettings
from agent_kit.middleware import build_middleware_stack
from agent_kit.prompts import BASE_SYSTEM_PROMPT
from agent_kit.schemas import ResearchReport, UserContext, report_strategy
from agent_kit.tools import ALL_TOOLS, SAFE_TOOLS, WRITE_TOOLS


@dataclass
class BuiltAgent:
    """一次 build 的产出，方便场景代码直接取用。"""

    graph: Any                      # CompiledStateGraph
    settings: AgentSettings
    checkpointer: Any
    store: Any
    context: UserContext


def _summarizer_model(settings: AgentSettings) -> Any | None:
    """上下文压缩用的小模型。

    fake 模式下造一个「永远返回固定摘要」的脚本模型；
    真实模式下复用主模型（生产里建议换成更便宜的型号，如 gpt-4o-mini）。
    """
    if settings.provider == "fake":

        from agent_kit.scripted_model import ScriptedChatModel

        return ScriptedChatModel(
            script=[],
            terminal_reply=(
                "## SESSION INTENT\n用户在进行 LangChain Agent 演示。\n\n"
                "## SUMMARY\n此前已经多次调用工具获取时间、检索笔记并尝试落盘报告。\n\n"
                "## ARTIFACTS\n研究报告草稿（尚未确认写入）。\n\n"
                "## NEXT STEPS\n按用户要求继续，或直接给出结论。"
            ),
        )
    # 真实模式下压缩也走主模型；想省钱可在此替换为更便宜的模型
    return None


def build_agent(
    settings: AgentSettings | None = None,
    *,
    model: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
    include_write_tools: bool = True,
    structured_output: bool = False,
    enable_summarization: bool = False,
    enable_hitl: bool = False,
    readonly: bool = False,
    checkpointer: Any = None,
    store: Any = None,
    middleware_extra: Sequence[AgentMiddleware] = (),
    state_schema: Any = None,
    context_schema: Any = None,
    debug: bool = False,
    message_window: int | None = None,
    enable_goal_anchor: bool = True,
    budget: dict | None = None,
    escalate_on_failure: bool = False,
) -> BuiltAgent:
    """一站式组装 Agent。

    Args:
        settings: 全局配置（provider / 各种限制）
        enable_goal_anchor: 目标锚定（防跑偏）。默认开启，state 无 original_goal 时自动跳过
        budget: 预算闸门（防失控），形如 {"max_model_calls": 12, "max_seconds": 120}；
            默认 None 不启用，多智能体长任务建议开启
        model: 直接指定模型实例；不传则按 provider 构造
        tools: 覆盖默认工具集
        include_write_tools: 是否装载危险写工具
        structured_output: 是否要求最终输出符合 ResearchReport
        enable_summarization: 是否开启长对话自动压缩
        enable_hitl: 是否给写工具加人工确认
        readonly: 非 admin 一律禁止写工具
        checkpointer / store: 记忆组件，缺省自动创建
        middleware_extra: 追加到内置中间件之后的自定义/场景中间件
        state_schema: 自定义状态（TypedDict，需继承 AgentState）
        context_schema: 运行时上下文 schema，缺省用 UserContext
        debug: 打印图执行细节
        message_window: 短期记忆的消息窗口大小（最近 N 条进上下文）；
            传了就启用，并与摘要压缩互斥（窗口优先）
    """
    settings = settings or AgentSettings()

    if tools is None:
        tools = list(ALL_TOOLS) if include_write_tools else list(SAFE_TOOLS)
    if not include_write_tools:
        tools = [t for t in tools if t not in WRITE_TOOLS]

    if model is None:
        if settings.provider == "fake":
            from agent_kit.config import build_chat_model

            model = build_chat_model(settings)
        else:
            from agent_kit.config import build_chat_model, require_api_key

            require_api_key(settings)
            model = build_chat_model(settings)

    # 消息窗口与摘要压缩互斥：窗口优先，开了窗口就不再造摘要模型（省一次模型调用）
    summarizer = None if message_window else (_summarizer_model(settings) if enable_summarization else None)
    middleware: list[AgentMiddleware] = build_middleware_stack(
        settings,
        model_for_summary=summarizer,
        enable_hitl=enable_hitl,
        readonly=readonly,
        message_window=message_window,
        enable_goal_anchor=enable_goal_anchor,
        budget=budget,
        escalate_on_failure=escalate_on_failure,
    )
    # 场景中间件排在内置中间件之后：先过护栏，再走场景逻辑
    middleware.extend(middleware_extra)

    cp = checkpointer or mem.build_checkpointer()
    st = store or mem.build_store()

    kwargs: dict[str, Any] = {}
    if state_schema is not None:
        kwargs["state_schema"] = state_schema

    graph = create_agent(
        model=model,
        tools=list(tools),
        system_prompt=BASE_SYSTEM_PROMPT,
        middleware=middleware,
        context_schema=context_schema or UserContext,
        checkpointer=cp,
        store=st,
        response_format=report_strategy() if structured_output else None,
        name="AtlasAgent",
        debug=debug,
        **kwargs,
    )

    return BuiltAgent(graph=graph, settings=settings, checkpointer=cp, store=st, context=UserContext())


def default_plan() -> dict[str, Any]:
    """给 README / 演示用的一句话能力清单。"""
    return {
        "tools(SAFE)": [t.name for t in SAFE_TOOLS],
        "tools(WRITE)": [t.name for t in WRITE_TOOLS],
        "structured_output": ResearchReport.__name__,
        "context_schema": UserContext.__name__,
    }
