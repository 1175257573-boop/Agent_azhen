"""中间件栈：这是 LangChain 1.x Agent 与旧版 AgentExecutor 最大的差别。

中间件的价值在于：**把「不涉及模型推理的横切逻辑」从提示词里剥离出来**，
既不占 token，也不会被模型「忘掉」。

这里分三类：
  A. 官方内置中间件（开箱即用）
  B. 自定义钩子中间件（@before_model / @wrap_tool_call / @after_model）
  C. 危险搬运品（HITL）—— 只在显式开启时加入
"""

from __future__ import annotations

import time
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
    after_model,
    before_model,
)
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import SystemMessage, ToolMessage

from agent_kit.config import AgentSettings
from agent_kit.logging_conf import get_logger
from agent_kit.tool_hooks import dual
from agent_kit.tools import DANGEROUS_TOOL_NAMES, error_handler

log = get_logger("agent.tool")


# =============================================================================
# B. 自定义钩子中间件
# =============================================================================
@before_model
def audit_guard(state: Any, runtime: Any) -> dict | None:
    """模型调用前：做准入检查。返回 dict 则更新状态，返回 None 则放行。

    签名必须是 (state, runtime) —— 这是 LangChain 1.x 对 before_model 钩子的约定，
    与 dynamic_prompt 不同（后者收的是 ModelRequest，必须从中再取 state / runtime）。

    这里演示：历史过长时追加一条系统消息让模型收束，而不是硬截断历史。
    """
    ctx = getattr(runtime, "context", None)
    role = getattr(ctx, "role", "user") if ctx else "user"

    msgs = state.get("messages", []) if isinstance(state, dict) else []
    if len(msgs) > 40 and role != "admin":
        return {"messages": [SystemMessage(content="上下文已超长，请立即给出结论，不要再调用工具。")]}
    return None


def _log_tool_ok(tool_name: str, elapsed_ms: float, result: Any) -> None:
    if isinstance(result, ToolMessage):
        preview = str(result.content)[:80].replace("\n", " ")
        log.info("[tool ok] %s (%.1fms) -> %s", tool_name, elapsed_ms, preview)
    else:
        log.info("[tool ok] %s (%.1fms)", tool_name, elapsed_ms)


def _sync_tool_logger(request: ToolCallRequest, handler: Any) -> ToolMessage:
    """包住每一次工具调用：计时、审计、异常转写。

    wrap_tool_call 必须用 handler(request) 显式调用后续环节——
    这也是它能做「重试、短路、结果改写」的原因。
    """
    tool_name = request.tool_call.get("name", "unknown")
    started = time.perf_counter()

    try:
        result = handler(request)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log.warning("[tool x] %s (%.1fms) 异常：%s: %s", tool_name, elapsed, type(exc).__name__, exc)
        raise

    _log_tool_ok(tool_name, (time.perf_counter() - started) * 1000, result)
    return result


async def _async_tool_logger(request: ToolCallRequest, handler: Any) -> ToolMessage:
    """异步版本：astream / ainvoke 走这条，handler 要 await。"""
    tool_name = request.tool_call.get("name", "unknown")
    started = time.perf_counter()

    try:
        result = await handler(request)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log.warning("[tool x] %s (%.1fms) 异常：%s: %s", tool_name, elapsed, type(exc).__name__, exc)
        raise

    _log_tool_ok(tool_name, (time.perf_counter() - started) * 1000, result)
    return result


tool_logger = dual(_sync_tool_logger, _async_tool_logger, name="tool_logger")


@after_model
def response_guard(state: Any, runtime: Any) -> dict | None:
    """模型输出后的检查钩子，签名同样是 (state, runtime)。

    这里只做「空回复检测」的告警。注意：**不要在 after_model 里无脑追加 AIMessage**
    ——那会让图重新走一遍模型节点，条件不变时极易形成死循环。
    真正需要中断时应使用 can_jump_to 显式跳转。
    """
    msgs = state.get("messages", []) if isinstance(state, dict) else []
    if not msgs:
        return None
    last = msgs[-1]
    if type(last).__name__ == "AIMessage" and not getattr(last, "content", None) and not getattr(last, "tool_calls", None):
        log.warning("[guard] 检测到空回复（无文本且无工具调用），生产环境应在此重试或跳转收尾")
    return None


def _readonly_check(request: ToolCallRequest) -> ToolMessage | None:
    """非管理员 + 危险工具 → 返回一条短路用的 ToolMessage，否则返回 None。"""
    ctx = getattr(request.runtime, "context", None)
    role = getattr(ctx, "role", "user") if ctx else "user"
    tool_name = request.tool_call.get("name", "")

    if role != "admin" and tool_name in DANGEROUS_TOOL_NAMES:
        return ToolMessage(
            content=f"只读模式：角色 {role} 无权调用 {tool_name}。请先申请 admin 权限。",
            tool_call_id=request.tool_call.get("id"),
        )
    return None


def _sync_readonly_enforcer(request: ToolCallRequest, handler: Any):
    """演示「全局只读模式」：非管理员禁止调用写工具。"""
    denied = _readonly_check(request)
    if denied is not None:
        return denied          # 直接短路，工具根本不会被执行
    return handler(request)


async def _async_readonly_enforcer(request: ToolCallRequest, handler: Any):
    denied = _readonly_check(request)
    if denied is not None:
        return denied
    return await handler(request)


readonly_enforcer = dual(_sync_readonly_enforcer, _async_readonly_enforcer, name="readonly_enforcer")


# =============================================================================
# A. 内置中间件组装
# =============================================================================
def build_middleware_stack(
    settings: AgentSettings,
    *,
    model_for_summary: Any = None,
    enable_hitl: bool = False,
    readonly: bool = False,
    message_window: int | None = None,
) -> list[AgentMiddleware]:
    """按职责顺序装配中间件。

    顺序很重要：
      1. 限流 → 越早越好，省 token
      2. 自定义守卫（cosπt control）
      3. 工具重试 → 包住工具本身
      4. 上下文工程 → 模型调用前（消息窗口 / 摘要压缩，二选一）
      5. PII 脱敏 → 出入口

    message_window 与 model_for_summary 是解决同一个问题（上下文膨胀）的两种策略：
      · 消息窗口：只留最近 N 条原文，简单可控，丢掉的原文仍在 Redis 里可回溯；
      · 摘要压缩：把历史压成摘要，信息密度高，但要多花一次摘要模型的调用。
    两者同时开没有意义，所以这里**窗口优先**，给了窗口就跳过摘要。
    """
    stack: list[AgentMiddleware] = [
        # ---- 1. 稳定性护栏 -------------------------------------------------
        ModelCallLimitMiddleware(
            run_limit=settings.model_call_limit,
            exit_behavior="end",  # 超限就正常收尾，而不是抛异常炸掉整个流程
        ),
        ToolCallLimitMiddleware(
            thread_limit=settings.tool_call_limit_per_run,
            exit_behavior="continue",
        ),
        ToolCallLimitMiddleware(
            tool_name="write_report",
            run_limit=2,
            exit_behavior="continue",
        ),

        # ---- 2. 工具可靠性 -------------------------------------------------
        ToolRetryMiddleware(
            max_retries=2,
            backoff_factor=2.0,
            initial_delay=0.5,
            max_delay=10.0,
            jitter=True,
            on_failure="continue",   # 重试仍失败就带着错误信息继续，让模型自己绕路
        ),
        error_handler,               # ToolException → ToolMessage

        # ---- 3. 自定义钩子 -------------------------------------------------
        audit_guard,
        tool_logger,
        response_guard,
    ]

    if readonly:
        stack.append(readonly_enforcer)

    # ---- 4. 上下文工程：消息窗口 / 摘要压缩（二选一） ----------------------
    if message_window:
        from agent_kit.memory import make_message_window

        stack.append(make_message_window(message_window))
        if model_for_summary is not None:
            log.warning("已启用消息窗口（%d 条），与摘要压缩二选一，本次跳过 SummarizationMiddleware", message_window)
    elif model_for_summary is not None:
        stack.append(
            SummarizationMiddleware(
                model=model_for_summary,
                trigger=("messages", settings.summarize_trigger),
                keep=("messages", settings.summarize_keep),
                trim_tokens_to_summarize=4000,
            )
        )

    # ---- 5. 数据合规：邮箱脱敏 --------------------------------------------
    stack.append(
        PIIMiddleware(
            "email",
            strategy="redact",
            apply_to_input=True,
            apply_to_output=True,
            apply_to_tool_results=True,
        )
    )

    # ---- 6. 人工介入（危险操作二次确认） ----------------------------------
    if enable_hitl:
        stack.append(
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "write_report": True,   # True = 允许 approve / edit / reject 三种决策
                },
                description_prefix="⚠️ 即将修改文件，需要人工确认",
            )
        )

    return stack
