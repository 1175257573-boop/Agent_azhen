"""消息链修补的测试：tool_calls 与 ToolMessage 必须成对。

对应线上级 bug：checkpoint 里留下「AIMessage 带 tool_calls，后面没有 ToolMessage」
的半截状态时，DeepSeek / OpenAI 兼容接口直接 400，且用户除了清空会话无法自救。
这里把三种坏结构都钉死。
"""

from __future__ import annotations

import asyncio

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from agent_kit.memory import make_message_window, sanitize_tool_call_pairs
from agent_kit.middleware import tool_call_pair_guard


def _ai_call(cid: str, name: str = "search") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": cid, "name": name, "args": {}}])


def _tool(cid: str, text: str = "ok") -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=cid, name="search")


def _pairs_ok(msgs: list) -> bool:
    """独立校验：不复用被测代码，避免「自己验自己」。"""
    pending: list[str] = []
    for m in msgs:
        if isinstance(m, ToolMessage):
            if m.tool_call_id not in pending:
                return False
            pending.remove(m.tool_call_id)
            continue
        if pending:
            return False
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            pending = [c.get("id") for c in m.tool_calls]
    return not pending


def test_dangling_tool_calls_gets_a_response():
    """悬空 tool_calls：后面紧跟的是 HumanMessage，必须补一条 tool 消息。"""
    msgs = [
        HumanMessage(content="生成脚本"),
        _ai_call("call_1"),
        HumanMessage(content="为什么无输出"),
    ]
    fixed = sanitize_tool_call_pairs(msgs)

    assert fixed is not None
    assert _pairs_ok(fixed)
    # 补充的 ToolMessage 必须夹在 AI 与 Human 之间，顺序错了 API 照样 400
    assert isinstance(fixed[2], ToolMessage)
    assert fixed[2].tool_call_id == "call_1"
    assert isinstance(fixed[3], HumanMessage)


def test_trailing_dangling_at_the_end():
    """末尾悬空：本次会话最后一条就是未响应的 tool_calls。"""
    msgs = [HumanMessage(content="hi"), _ai_call("call_9")]
    fixed = sanitize_tool_call_pairs(msgs)

    assert fixed is not None
    assert _pairs_ok(fixed)
    assert isinstance(fixed[-1], ToolMessage)


def test_orphan_tool_message_is_dropped():
    """孤儿 ToolMessage：窗口裁剪把发起它的 AIMessage 丢了，只能丢弃。"""
    msgs = [
        _tool("call_gone", "旧结果"),
        HumanMessage(content="继续"),
    ]
    fixed = sanitize_tool_call_pairs(msgs)

    assert fixed is not None
    assert _pairs_ok(fixed)
    assert len(fixed) == 1
    assert isinstance(fixed[0], HumanMessage)


def test_multiple_calls_all_answered():
    """一次发多个 tool_calls：每条都要有响应，只补缺失的那条。"""
    msgs = [
        HumanMessage(content="一起查"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c1", "name": "a", "args": {}},
                {"id": "c2", "name": "b", "args": {}},
            ],
        ),
        _tool("c1"),
        HumanMessage(content="只回来一个"),
    ]
    fixed = sanitize_tool_call_pairs(msgs)

    assert fixed is not None
    assert _pairs_ok(fixed)
    ids = [m.tool_call_id for m in fixed if isinstance(m, ToolMessage)]
    assert ids == ["c1", "c2"]


def test_clean_history_is_untouched():
    """好历史必须原样返回 None —— 否则每次都写盘，白改 checkpoint。"""
    msgs = [
        SystemMessage(content="system"),
        HumanMessage(content="hi"),
        _ai_call("c1"),
        _tool("c1"),
        AIMessage(content="好了"),
    ]
    assert sanitize_tool_call_pairs(msgs) is None


def test_idempotent():
    """修一遍再修一遍：结果稳定，不会反复插 ToolMessage。"""
    msgs = [HumanMessage(content="hi"), _ai_call("c1"), HumanMessage(content="then")]
    once = sanitize_tool_call_pairs(msgs)
    assert once is not None
    twice = sanitize_tool_call_pairs(once)
    assert twice is None
    # 3 条原始消息 + 1 条补齐的 ToolMessage
    assert len(once) == 4


# ---------------------------------------------------------------------------
# 同步 / 异步两条链路都要能修补
# ---------------------------------------------------------------------------
CAPTURED: list[list] = []


class _CapturingModel(BaseChatModel):
    """把每次真正发给模型的 messages 存下来。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        CAPTURED.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="收到"))])

    @property
    def _llm_type(self) -> str:
        return "capturing"


# 两处 tool_calls 没有响应的坏历史，正是线上 400 的形态。
# 纯手工构造的最小样本（不来自任何真实会话），内容刻意取得含糊，
# 避免被误读成真实用户的聊天记录。
_DIRTY = [
    SystemMessage(content="你是助手"),
    HumanMessage(content="生成一个脚本"),
    AIMessage(content="", tool_calls=[{"id": "call_4f1c", "name": "t", "args": {}}]),
    HumanMessage(content="为什么无输出"),
    AIMessage(content="", tool_calls=[{"id": "call_636f", "name": "t", "args": {}}]),
    HumanMessage(content="好了吗"),
]


def _build():
    return create_agent(
        model=_CapturingModel(),
        tools=[],
        system_prompt="测试",
        middleware=[make_message_window(15), tool_call_pair_guard],
        checkpointer=InMemorySaver(),
        name="probe-sanitize",
    )


def test_middlewares_implement_both_sync_and_async():
    """两个消息中间件必须同时提供同步与异步实现。

    只写 @wrap_model_call 的话，走到 astream（MCP 场景）会抛
    `NotImplementedError: Asynchronous implementation of awrap_model_call is not available`。
    """
    import inspect

    for mw in (tool_call_pair_guard, make_message_window(15)):
        assert callable(getattr(mw, "wrap_model_call", None)), f"{mw} 缺同步实现"
        a = getattr(mw, "awrap_model_call", None)
        assert callable(a) and inspect.iscoroutinefunction(a), f"{mw} 缺异步实现"


def test_sync_invocation_gets_repaired_messages():
    graph = _build()
    cfg = {"configurable": {"thread_id": "t-sync"}}
    graph.invoke({"messages": _DIRTY}, cfg)
    CAPTURED.clear()
    graph.invoke({"messages": [HumanMessage(content="现在几点")]}, cfg)

    assert _pairs_ok(CAPTURED[-1]), "同步链路发给模型的消息仍不成对"


def test_async_invocation_gets_repaired_messages():
    """异步链路（astream / ainvoke，MCP 场景走的正是这条）也要修补。"""

    async def _run() -> list:
        graph = _build()
        cfg = {"configurable": {"thread_id": "t-async"}}
        await graph.ainvoke({"messages": _DIRTY}, cfg)
        CAPTURED.clear()
        await graph.ainvoke({"messages": [HumanMessage(content="现在几点")]}, cfg)
        return CAPTURED[-1]

    batch = asyncio.run(_run())
    assert _pairs_ok(batch), "异步链路发给模型的消息仍不成对"

