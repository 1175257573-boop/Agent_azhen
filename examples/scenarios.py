"""八个可直接运行的演示场景。

每个场景都围绕 LangChain 1.x 的一个核心能力设计，
并且全部能在 `provider=fake`（零 API Key）下真实跑完。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent_kit import memory as mem
from agent_kit.agent import build_agent
from agent_kit.config import AgentSettings
from agent_kit.scripted_model import (
    ScriptedChatModel,
    has_tool_output,
    last_tool_result,
    tool_call,
)

# 运行时上下文：既可以传 UserContext 实例，也可以直接传 dict（推荐，更少的序列化噪音）。
# 字段名必须与 schemas.UserContext 一致，框架会自动做一次结构化转换。
ADMIN_CTX: dict = {"user_id": "demo", "role": "admin", "locale": "zh-CN"}
GUEST_CTX: dict = {"user_id": "guest", "role": "user", "locale": "zh-CN"}


# ---------------------------------------------------------------------------
# 打印工具
# ---------------------------------------------------------------------------
def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _dump_messages(result: dict[str, Any], max_len: int = 160) -> None:
    for m in result.get("messages", []):
        role = type(m).__name__.replace("Message", "")
        content = m.content if isinstance(m.content, str) else str(m.content)
        if getattr(m, "tool_calls", None):
            calls = ", ".join(f"{tc['name']}({tc['args']})" for tc in m.tool_calls)
            print(f"  [{role}] <调用工具> {calls}")
        else:
            print(f"  [{role}] {content[:max_len]}{'...' if len(content) > max_len else ''}")


# ---------------------------------------------------------------------------
# 场景 1：最小 ReAct 循环
# ---------------------------------------------------------------------------
def scenario_basic() -> None:
    """提问 → 模型决定调工具 → 拿到结果 → 回答。这是所有 Agent 的最小骨架。"""
    _banner("场景 1 · 基础 ReAct 循环（工具调用）")

    script = [
        tool_call("get_current_time", {}),
        # 第二轮：看到工具结果后才给出最终答复
        lambda msgs, tools: AIMessage(content=f"我查到了当前时间是 {last_tool_result(msgs)}，可以开始工作了。"),
    ]
    built = build_agent(AgentSettings(provider="fake"), model=ScriptedChatModel(script=script))

    result = built.graph.invoke(
        {"messages": [HumanMessage(content="现在几点了？")]},
        config=mem.thread_config("s1"),
        context=ADMIN_CTX,
    )
    _dump_messages(result)


# ---------------------------------------------------------------------------
# 场景 2：多步推理（检索 + 计算 + 合成）
# ---------------------------------------------------------------------------
def scenario_multi_tool() -> None:
    """演示工具链：先检索事实，再做计算，最后把两条证据合成一句话答案。"""
    _banner("场景 2 · 多步推理（检索 → 计算 → 合成）")

    script = [
        tool_call("search_notes", {"query": "LangChain 中间件", "top_k": 3, "strategy": "keyword"}),
        tool_call("calculator", {"expression": "sqrt(16) + 2**8"}),
        lambda msgs, tools: AIMessage(
            content="检索与计算都已完成：中间件负责横切逻辑，sqrt(16)+2**8 = 260。"
        ),
    ]
    built = build_agent(AgentSettings(provider="fake"), model=ScriptedChatModel(script=script))

    result = built.graph.invoke(
        {"messages": [HumanMessage(content="查一下中间件的资料，顺便算 sqrt(16)+2^8")]},
        config=mem.thread_config("s2"),
        context=ADMIN_CTX,
    )
    _dump_messages(result)


# ---------------------------------------------------------------------------
# 场景 3：流式输出
# ---------------------------------------------------------------------------
def scenario_stream() -> None:
    """token 级流式。stream_mode='messages' 会把模型增量输出逐步推送过来。"""
    _banner("场景 3 · 流式输出（stream_mode='messages'）")

    text = "流式输出演示：这一段话会被拆成多个小片段，逐个推送到终端。"
    built = build_agent(
        AgentSettings(provider="fake"),
        model=ScriptedChatModel(script=[AIMessage(content=text)]),
    )

    print("  >> 实时输出：", end="", flush=True)
    chunk_count = 0
    for chunk, _meta in built.graph.stream(
        {"messages": [HumanMessage(content="给我演示一下流式输出")]},
        config=mem.thread_config("s3"),
        stream_mode="messages",
        context=ADMIN_CTX,
    ):
        piece = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if piece:
            chunk_count += 1
            print(piece, end="", flush=True)
    print(f"\n  （共收到 {chunk_count} 个流式片段，说明确实是增量推送而非一次性返回）\n")


# ---------------------------------------------------------------------------
# 场景 4：结构化输出
# ---------------------------------------------------------------------------
def scenario_structured() -> None:
    """强制模型最终产出符合 ResearchReport 的结构化对象，而不是自由文本。"""
    _banner("场景 4 · 结构化输出（ToolStrategy + Pydantic）")

    report_args = {
        "title": "LangChain 1.x 中间件机制调研",
        "summary": "中间件把横切逻辑从提示词里剥离，官方已内置十余种。",
        "key_findings": [
            {"fact": "限流类中间件应最先执行以降低 token 消耗", "source": "search_notes"},
            {"fact": "上下文压缩中间件可在长对话中自动替换历史", "source": "search_notes"},
        ],
        "open_questions": ["HITL 在异步场景下的最佳时机"],
        "confidence": 0.72,
    }

    def step(msgs, tools):
        # ToolStrategy 会把 schema 注册成同名工具：ResearchReport
        names = {getattr(t, "name", "") for t in tools}
        if any("response_format" in n or n == "ResearchReport" for n in names):
            return tool_call("ResearchReport", report_args, call_id="so_1")
        return tool_call("search_notes", {"query": "middleware"}, call_id="so_0")

    built = build_agent(
        AgentSettings(provider="fake"),
        model=ScriptedChatModel(script=[step]),
        structured_output=True,
    )

    result = built.graph.invoke(
        {"messages": [HumanMessage(content="调研一下中间件机制，输出结构化报告")]},
        config=mem.thread_config("s4"),
        context=ADMIN_CTX,
    )

    report = result.get("structured_response")
    if report is None:
        print("  [!] 未拿到结构化结果，原始消息如下：")
        _dump_messages(result)
        return

    print(f"  标题      : {report.title}")
    print(f"  摘要      : {report.summary}")
    print(f"  置信度    : {report.confidence:.0%}")
    print("  关键结论  :")
    for item in report.key_findings:
        print(f"     · {item.fact}  ← 来源：{item.source}")
    print(f"  待办问题  : {', '.join(report.open_questions) or '无'}")


# ---------------------------------------------------------------------------
# 场景 5：短期记忆 + 长期记忆
# ---------------------------------------------------------------------------
def scenario_memory() -> None:
    """短期：同 thread_id 自动续接；长期：store 里的偏好跨会话、跨 Agent 实例共享。"""
    _banner("场景 5 · 记忆（短期 thread_id / 长期 store）")

    settings = AgentSettings(provider="fake")
    store = mem.build_store()
    mem.seed_long_term_memory(store, user_id="demo")

    def script_factory(reply: str) -> ScriptedChatModel:
        # 先读长期记忆，再回答——模拟「开口之前先回忆」
        def step(msgs, tools):
            if has_tool_output(msgs, "recall_preferences"):
                return AIMessage(content=f"我记得你的偏好：{last_tool_result(msgs)[:60]}。{reply}")
            return tool_call("recall_preferences", {})

        return ScriptedChatModel(script=[step, step])  # 两轮脚本：① 调工具 ② 合成回答

    # ---- 短期记忆：同一个 thread_id 连续两轮 ----
    built = build_agent(
        settings,
        model=script_factory("第一轮回答完毕。"),
        checkpointer=mem.build_checkpointer(),
        store=store,
    )
    cfg = mem.thread_config("s5-demo")

    r1 = built.graph.invoke({"messages": [HumanMessage(content="记住我的偏好了吗？")]}, config=cfg, context=ADMIN_CTX)
    print("  第 1 轮：", r1["messages"][-1].content[:100])

    # 第二轮复用同一个 thread_id：checkpointer 会自动把历史塞回去
    built2 = build_agent(
        settings,
        model=script_factory("第二轮回答完毕，且我记得上一轮说过的话。"),
        checkpointer=built.checkpointer,
        store=store,
    )
    r2 = built2.graph.invoke({"messages": [HumanMessage(content="刚才我们聊到哪了？")]}, config=cfg, context=ADMIN_CTX)
    print("  第 2 轮：", r2["messages"][-1].content[:100])
    print(f"  短期记忆条数：{len(r2['messages'])}（含第 1 轮历史 → 说明 thread_id 生效）")

    # ---- 长期记忆：换一个全新 thread 与全新 Agent，store 仍然共享 ----
    r3 = build_agent(
        settings,
        model=script_factory("跨会话回答。"),
        store=store,      # 关键：store 复用
    ).graph.invoke(
        {"messages": [HumanMessage(content="我偏好什么？")]},
        config=mem.thread_config(f"s5-new-{uuid.uuid4().hex[:6]}"),
        context=ADMIN_CTX,
    )
    print("  新会话  ：", r3["messages"][-1].content[:100])


# ---------------------------------------------------------------------------
# 场景 6：人工确认（Human-in-the-Loop）
# ---------------------------------------------------------------------------
def scenario_hitl() -> None:
    """写文件前中断 → 人工 approve/edit/reject → 用 Command(resume=...) 恢复执行。"""
    _banner("场景 6 · 人工确认 HITL（interrupt → Command resume）")

    def script(msgs, tools):
        if has_tool_output(msgs, "write_report"):
            return AIMessage(content=f"文件已按审批结果处理：{last_tool_result(msgs)[:60]}")
        return tool_call("write_report", {"filename": "hitl-report", "content": "# 演示报告\n\n内容 A"}, call_id="hitl_1")

    built = build_agent(
        AgentSettings(provider="fake"),
        # 同一份脚本要消费两次：第一次触发工具调用，resume 后合成答复
        model=ScriptedChatModel(script=[script, script]),
        enable_hitl=True,
        include_write_tools=True,
    )
    cfg = mem.thread_config("s6")

    result = built.graph.invoke(
        {"messages": [HumanMessage(content="把这份报告写进 runs/")]},
        config=cfg,
        context=ADMIN_CTX,
    )

    interrupts = result.get("__interrupt__")
    if not interrupts:
        print("  [!] 未触发中断，工具可能已被其它中间件短路。")
        _dump_messages(result)
        return

    review = interrupts[0].value
    print("  ⏸ 已中断，等待人工决策")
    for action in review.get("action_requests", []):
        print(f"     · 待审批动作：{action['name']}  参数={action['args']}")
    print(f"     允许的决策：{review.get('review_configs', [{}])[0].get('allowed_decisions', [])}")

    # 决策支持四种：approve / edit / reject / respond。
    # resume 载荷必须是 dict：{"decisions": [...]}，且 decisions 数量要与被拦截动作数一致。
    decision: dict = {
        "type": "edit",   # 换成 "approve" 直接放行；换成 "reject" 则需带 "message" 说明理由
        "edited_action": {
            "name": "write_report",
            "args": {"filename": "hitl-report", "content": "# 演示报告\n\n人工改写后的内容 B"},
        },
    }
    print(f"  ▶ 人工决策：{decision['type']}（把正文改写为「内容 B」）")

    resumed = built.graph.invoke(Command(resume={"decisions": [decision]}), config=cfg, context=ADMIN_CTX)
    _dump_messages(resumed)


# ---------------------------------------------------------------------------
# 场景 7：护栏（只读模式 + 调用上限）
# ---------------------------------------------------------------------------
def scenario_guard() -> None:
    """两个护栏演示：非 admin 被禁止写文件；模型调用次数超限时优雅收尾而非崩溃。"""
    _banner("场景 7 · 护栏（只读拦截 / 调用次数上限）")

    # 7.1 只读模式：Interceptor 在工具执行前短路
    def try_write(msgs, tools):
        return tool_call("write_report", {"filename": "should-be-blocked", "content": "x"}, call_id="g1")

    built_ro = build_agent(
        AgentSettings(provider="fake"),
        model=ScriptedChatModel(script=[try_write, lambda m, t: AIMessage(content="好的，已跳过该文件操作。")]),
        readonly=True,
        include_write_tools=True,
    )
    res = built_ro.graph.invoke(
        {"messages": [HumanMessage(content="帮我写个文件")]},
        config=mem.thread_config("s7-ro"),
        context=GUEST_CTX,  # 非 admin
    )
    print("  只读拦截：")
    for m in res["messages"]:
        if type(m).__name__ == "ToolMessage":
            print(f"     · 工具返回 → {m.content[:80]}")

    # 7.2 模型调用上限：脚本足够长，必然触发 ModelCallLimitMiddleware
    settings = AgentSettings(provider="fake", model_call_limit=3)
    endless = ScriptedChatModel(
        script=[lambda msgs, tools, i=i: tool_call("get_current_time", {}, call_id=f"loop_{i}") for i in range(20)]
    )
    built_limit = build_agent(settings, model=endless)
    res2 = built_limit.graph.invoke(
        {"messages": [HumanMessage(content="不停地调工具试试")]},
        config=mem.thread_config("s7-limit"),
        context=ADMIN_CTX,
    )
    calls = sum(1 for m in res2["messages"] if type(m).__name__ == "ToolMessage")
    print(f"  调用上限：模型调用被限制在 {settings.model_call_limit} 次，实际工具执行 {calls} 次后流程正常结束。")


# ---------------------------------------------------------------------------
# 场景 8：长对话上下文压缩
# ---------------------------------------------------------------------------
def scenario_summarization() -> None:
    """历史消息超过阈值时，SummarizationMiddleware 自动把旧对话替换为摘要。"""
    _banner("场景 8 · 上下文压缩（SummarizationMiddleware）")

    settings = AgentSettings(provider="fake", summarize_trigger=8, summarize_keep=4)

    def reply(msgs, tools):
        return AIMessage(content="收到，继续保持。")

    built = build_agent(settings, model=ScriptedChatModel(script=[reply]), enable_summarization=True)
    cfg = mem.thread_config("s8")

    # 先灌 10 轮历史，触发阈值
    history = []
    for i in range(5):
        history.extend([HumanMessage(content=f"第 {i} 条无关紧要的历史消息，用于撑长上下文。" * 3),
                        AIMessage(content=f"第 {i} 条回答，同样用于占位。")])
        built.graph.invoke({"messages": history}, config=cfg, context=ADMIN_CTX)

    after = built.graph.invoke({"messages": [HumanMessage(content="现在上下文应该已经被压缩过了")]}, config=cfg, context=ADMIN_CTX)
    msgs = after["messages"]
    has_summary = "SESSION INTENT" in str(msgs[0].content) if msgs and getattr(msgs[0], "content", None) else False
    print(f"  当前消息条数：{len(msgs)}（阈值 {settings.summarize_trigger}，压缩后保留 {settings.summarize_keep} 条）")
    print(f"  首条是否为摘要：{has_summary}")
    if has_summary:
        print(f"  摘要预览：{str(msgs[0].content)[:120]}...")


# ---------------------------------------------------------------------------
# 场景注册表
# ---------------------------------------------------------------------------
SCENARIOS: dict[str, Callable[[], None]] = {
    "basic": scenario_basic,
    "multi_tool": scenario_multi_tool,
    "stream": scenario_stream,
    "structured": scenario_structured,
    "memory": scenario_memory,
    "hitl": scenario_hitl,
    "guard": scenario_guard,
    "summarization": scenario_summarization,
}

SCENARIO_HELP = {
    "basic": "最小 ReAct 循环",
    "multi_tool": "多步推理链路",
    "stream": "token 级流式输出",
    "structured": "结构化输出 Pydantic",
    "memory": "短期 thread_id + 长期 store",
    "hitl": "人工确认审批流",
    "guard": "只读拦截与调用限流",
    "summarization": "长对话上下文压缩",
}
