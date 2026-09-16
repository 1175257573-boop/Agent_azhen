"""MCP 客户端与工具拦截器。

技术选型说明（重要）：

  LangChain 的 MCP 集成在 1.4.0 换过一代。官方从 1.4.0 起把 MCP 支持移入主包
  `langchain.mcp`（基于 FastMCP），取代独立包 `langchain-mcp-adapters`，
  MultiServerMCPClient 折叠为单个 MCPAdapter：
    · 迁移指南 https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters
    · 发布博客 https://www.langchain.com/blog/mcp-in-langchain-stateless-protocol-elicitation-and-more

  本地实测还撞到一个硬性冲突：独立包会把 `mcp` 依赖降到 1.x，而 fastmcp 4.x 的
  **server 端**必须依赖 mcp 2.x（`mcp.server.request_state`），装上后本地 MCP
  服务根本起不来。官方方向与本地实测指向同一结论。

  因此本项目用 **LangChain 1.4 内置的 `langchain.mcp.MCPAdapter`**（已实测连通 6 个工具）。
  注意该命名空间官方标注为 **beta**，导入会抛 LangChainBetaWarning，API 可能变化。

  旧包 `tool_interceptors`（ToolCallInterceptor 协议）在 langchain.mcp 里没有直接
  对应物，本项目用 LangChain 原生中间件等价实现：

    tool_interceptors        →  本项目的 @wrap_tool_call 中间件
      · 访问运行时上下文      →  request.runtime.{state,context,store,tool_call_id}
      · 改写工具参数          →  request.override(tool_call={...})
      · 短路不执行工具        →  直接 return ToolMessage(...)
      · 流程控制              →  返回 Command（跳转/改状态）

  进度通知、日志、Elicitation 是 **MCP 协议层**能力：
  本项目在服务端用 fastmcp 的 Context 实现（见 mcp_server.py 的
  ctx.report_progress / ctx.info / ctx.elicit）。
  客户端侧官方已提供 `langchain.mcp.elicitation`（以 LangGraph interrupt() 驱动
  人工回答），本项目尚未接线该 interrupt 循环。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agent_kit.tool_hooks import dual

log = logging.getLogger("agent.mcp")

SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"

# 其余 MCP Server：工程质量体检 / Git 只读 / 文档一致性审计
# 新增 server 只要往这里加一行，Agent 侧不用改代码——这正是 MCP 的价值所在。
SERVER_DIR = Path(__file__).resolve().parent / "mcp_servers"
ALL_SERVERS: dict[str, Path] = {
    "notes": SERVER_PATH,
    "quality": SERVER_DIR / "quality.py",
    "git": SERVER_DIR / "git_history.py",
    "docs": SERVER_DIR / "doc_audit.py",
}


# ---------------------------------------------------------------------------
# 拦截器（等价于 langchain-mcp-adapters 的 tool_interceptors）
# ---------------------------------------------------------------------------
def _audit_meta(request: ToolCallRequest) -> tuple[str, Any, Any]:
    name = request.tool_call.get("name", "?")
    runtime = request.runtime
    user_id = getattr(getattr(runtime, "context", None), "user_id", None)
    server = getattr(getattr(runtime, "server_info", None), "name", None)
    return name, user_id, server


def _sync_mcp_audit(request: ToolCallRequest, handler):
    name, user_id, server = _audit_meta(request)
    started = time.perf_counter()
    try:
        result = handler(request)
    except Exception as exc:
        log.warning("[mcp x] %s (%s) 异常：%s", name, user_id, exc)
        raise
    log.info("[mcp ok] %s server=%s user=%s %.1fms", name, server or "-", user_id or "-",
             (time.perf_counter() - started) * 1000)
    return result


async def _async_mcp_audit(request: ToolCallRequest, handler):
    """异步版：MCP 工具只有 ainvoke，astream 链路必须走这条。"""
    name, user_id, server = _audit_meta(request)
    started = time.perf_counter()
    try:
        result = await handler(request)
    except Exception as exc:
        log.warning("[mcp x] %s (%s) 异常：%s", name, user_id, exc)
        raise
    log.info("[mcp ok] %s server=%s user=%s %.1fms", name, server or "-", user_id or "-",
             (time.perf_counter() - started) * 1000)
    return result


# 拦截器 1：审计每一通 MCP 调用——谁调的、调什么、耗时多少
mcp_audit = dual(_sync_mcp_audit, _async_mcp_audit, name="mcp_audit")


def _tool_schema(tool: Any) -> dict[str, Any] | None:
    """拿到工具的入参 JSON Schema（StructuredTool 用 .args，Pydantic 用 model_json_schema）。"""
    if tool is None:
        return None
    schema = getattr(tool, "args", None) or getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        try:
            return schema.model_json_schema()
        except Exception:  # noqa: BLE001
            return None
    return None


def _schema_accepts(schema: dict[str, Any] | None, keys: set[str]) -> bool:
    """工具是否吃得了这些额外字段。

    很多工具（如 `current_utc`）声明了「不接受任何参数」，
    无脑注入会让 Pydantic 直接报 unexpected_keyword_argument。
    """
    if schema is None:
        return False
    if schema.get("additionalProperties") is True:
        return True
    props = schema.get("properties") or {}
    return bool(props) and keys.issubset(set(props))


def make_arg_injector(extra_args: dict[str, Any], *, only: Iterable[str] | None = None):
    """拦截器 2：给工具参数**注入**额外字段（用户身份、租户、追踪 ID）。

    典型场景：MCP 工具本身不关心 user_id，但服务端审计需要 ——
    在拦截器里补上，比改每一个工具的实现干净得多。

    Args:
        extra_args: 要注入的键值对。
        only: 可选，限定只对这些工具注入；不填则按 schema 自动判断
              （工具声明了这些字段、或允许额外属性才注入，否则跳过）。
    """
    keys = set(extra_args)
    only_set = set(only) if only else None

    def _maybe_inject(request: ToolCallRequest) -> ToolCallRequest:
        name = request.tool_call.get("name", "")
        if only_set is not None and name not in only_set:
            return request
        if only_set is None and not _schema_accepts(_tool_schema(getattr(request, "tool", None)), keys):
            return request
        new_args = {**request.tool_call.get("args", {}), **extra_args}
        return request.override(tool_call={**request.tool_call, "args": new_args})

    def sync_arg_injector(request: ToolCallRequest, handler):
        return handler(_maybe_inject(request))

    async def async_arg_injector(request: ToolCallRequest, handler):
        return await handler(_maybe_inject(request))

    return dual(sync_arg_injector, async_arg_injector, name="mcp_arg_injector")


def _short_circuit(request: ToolCallRequest, message: str) -> ToolMessage:
    return ToolMessage(content=message, tool_call_id=request.tool_call.get("id"))


def make_guard(denied: Iterable[str], message: str = "该操作被安全策略拦截。"):
    """拦截器 3：按工具名黑名单短路，工具根本不会被执行。"""
    denied_set = set(denied)

    def sync_guard(request: ToolCallRequest, handler):
        name = request.tool_call.get("name", "")
        if name in denied_set:
            return _short_circuit(request, f"{message}（{name}）")
        return handler(request)

    async def async_guard(request: ToolCallRequest, handler):
        name = request.tool_call.get("name", "")
        if name in denied_set:
            return _short_circuit(request, f"{message}（{name}）")
        return await handler(request)

    return dual(sync_guard, async_guard, name="mcp_guard")


def make_runtime_guard(predicate: Callable[[ToolCallRequest], bool], message: str):
    """拦截器 4：按**运行时状态**决定放行与否（比静态黑名单灵活）。

    例：state 未认证时拒绝所有写类工具。
    """

    def sync_runtime_guard(request: ToolCallRequest, handler):
        if not predicate(request):
            return _short_circuit(request, message)
        return handler(request)

    async def async_runtime_guard(request: ToolCallRequest, handler):
        if not predicate(request):
            return _short_circuit(request, message)
        return await handler(request)

    return dual(sync_runtime_guard, async_runtime_guard, name="mcp_runtime_guard")


# ---------------------------------------------------------------------------
# 客户端：连接并取回工具
# ---------------------------------------------------------------------------
class MCPHub:
    """管理一组 MCP 目标（本地脚本 / 远程 URL），产出可直接喂给 create_agent 的工具。

    **必须显式关闭**：每个本地目标都会拉起一个 stdio 子进程，
    不关的话进程会残留，退出时还会因析构顺序触发
    `RuntimeError: Event loop is closed`（fastmcp 的 StdioTransport.__del__）。
    """

    def __init__(self) -> None:
        self.tools: list = []
        self.adapters: list = []

    async def connect(self, targets: list[Path | str]) -> list:
        """连接所有目标并汇总工具。

        target 可以是：
          - Path：本地 .py 脚本，以 stdio 子进程方式启动
          - str ：http(s) URL，走 streamable-http
        """
        from langchain.mcp import MCPAdapter

        for target in targets:
            adapter = MCPAdapter(target)
            # 注意：不要 __aenter__ —— 它开的是 anyio 任务组，
            # 必须在**同一个 task** 里退出；而装配和关闭是两个不同 task，
            # 跨 task 退出会报「Attempted to exit cancel scope in a different task」。
            # 这里直接 list_tools（内部按需连接），关闭时用 client.close()。
            got = await adapter.list_tools()
            log.info("MCP 目标 %s 提供 %d 个工具", target, len(got))
            self.adapters.append(adapter)
            self.tools.extend(got)
        return self.tools

    async def connect_default(self) -> list:
        """连接本项目自带的笔记 MCP Server（保持单 server 行为不变）。"""
        return await self.connect([SERVER_PATH])

    async def connect_named(self, names: Iterable[str]) -> list:
        """按名字连接部分 server，例如 connect_named(["quality", "git"])。"""
        missing = [n for n in names if n not in ALL_SERVERS]
        if missing:
            raise KeyError(f"未知 MCP Server：{missing}。可用：{', '.join(ALL_SERVERS)}")
        return await self.connect([ALL_SERVERS[n] for n in names])

    async def connect_all(self) -> list:
        """连接全部已注册的 server。"""
        return await self.connect(list(ALL_SERVERS.values()))

    async def aclose(self) -> None:
        """逐个关闭 MCP 连接（收掉 stdio 子进程）。

        用 `client.close()` 而不是 `__aexit__`：后者要退出 anyio 任务组，
        而关闭往往发生在与连接不同的 task 里，会直接抛 RuntimeError。
        """
        while self.adapters:
            adapter = self.adapters.pop()
            try:
                await adapter.client.close()
            except Exception as exc:  # noqa: BLE001 —— 关闭阶段的异常不该盖掉主流程
                log.warning("关闭 MCP 连接时出错：%s: %s", type(exc).__name__, exc)
        self.tools = []


async def build_mcp_tools(use_remote: str | None = None) -> list:
    """便捷函数：拿到本项目的 MCP 工具列表。"""
    hub = MCPHub()
    if use_remote:
        await hub.connect([use_remote])
    else:
        await hub.connect_default()
    return hub.tools
