"""动态工具：运行时决定「给模型看哪些工具」。

三种典型诉求：
  A. 按身份/状态过滤已注册工具 —— 未登录只给 public_* 工具
  B. 运行时往请求里追加新工具   —— 中间件 + wrap_tool_call 兜底执行
  C. 工具注册表 —— 让模型自己按需注册工具（本文件的自研扩展）

要点：`request.override(tools=...)` 改的是**本次请求**能看到的工具，
不许直接改全局工具列表；新增的工具因为没有执行器，必须在 wrap_tool_call 里
用 `request.override(tool=...)` 补上实现。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ToolCallRequest
from langchain_core.tools import BaseTool

from agent_kit.state import TaskState
from agent_kit.tool_hooks import dual_model


# ---------------------------------------------------------------------------
# A. 按 state 过滤工具
# ---------------------------------------------------------------------------
def make_state_based_tools(require_auth_prefix: str = "public_", flag: str = "authenticated"):
    """未认证时只暴露指定前缀的工具。

    典型的权限收口：把「能不能调」这件事放在中间件里做，
    而不是指望模型在提示词约束下自觉。
    """

    def _filter(request: ModelRequest) -> ModelRequest:
        state = request.state or {}
        authenticated = bool(state.get(flag, False)) if isinstance(state, dict) else False
        if authenticated:
            return request
        visible = [t for t in request.tools if getattr(t, "name", "").startswith(require_auth_prefix)]
        return request.override(tools=visible)

    def state_based_tools(request: ModelRequest, handler):
        return handler(_filter(request))

    async def astate_based_tools(request: ModelRequest, handler):
        return await handler(_filter(request))

    return dual_model(state_based_tools, astate_based_tools,
                      name="state_based_tools", state_schema=TaskState)


# ---------------------------------------------------------------------------
# B. 运行时注册新工具（中间件持有工具对象，同时负责执行）
# ---------------------------------------------------------------------------
class DynamicToolMiddleware(AgentMiddleware):
    """把额外工具注入每次请求，并接管它们的执行。

    为什么要两个钩子都实现：
      wrap_model_call 负责「让模型看见」，模型调用后执行节点找不到该工具实现会报错，
      因此还需要 wrap_tool_call 用 request.override(tool=...) 提供真正的可调用对象。
    """

    def __init__(self, tools: list[BaseTool], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.extra_tools: list[BaseTool] = list(tools)

    def register(self, tool: BaseTool) -> None:
        """运行期再追加一个工具。"""
        self.extra_tools.append(tool)

    def _with_extra(self, request: ModelRequest) -> ModelRequest:
        if not self.extra_tools:
            return request
        return request.override(tools=[*request.tools, *self.extra_tools])

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._with_extra(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._with_extra(request))

    def _override_for(self, request: ToolCallRequest) -> ToolCallRequest:
        name = request.tool_call.get("name")
        for tool in self.extra_tools:
            if getattr(tool, "name", None) == name:
                return request.override(tool=tool)
        return request

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        return handler(self._override_for(request))

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        return await handler(self._override_for(request))


# ---------------------------------------------------------------------------
# C. 工具注册表：让模型自己「按需取工具」
# ---------------------------------------------------------------------------
class ToolRegistry:
    """一个最简单的运行时工具注册表。

    和上面的 DynamicToolMiddleware 配合：模型调用 register_tool(name) 之后，
    该工具才会出现在它后续的可见列表里（渐进式披露的工具版）。
    """

    def __init__(self, catalog: dict[str, BaseTool] | None = None) -> None:
        self.catalog: dict[str, BaseTool] = dict(catalog or {})

    def add(self, tool: BaseTool) -> None:
        self.catalog[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self.catalog.get(name)

    def names(self) -> list[str]:
        return list(self.catalog)

    def as_factory_tools(self) -> list[Callable]:
        """返回 list_tools / register_tool 两个让模型自服务的 @tool。"""
        from langchain.tools import tool

        registry = self

        @tool
        def list_available_tools() -> str:
            """列出所有可按需注册的工具名称与用途。想要某个工具前先调用它。"""
            if not registry.catalog:
                return "注册表为空。"
            lines = []
            for name, t in registry.catalog.items():
                lines.append(f"- {name}: {(t.description or '').splitlines()[0] if t.description else ''}")
            return "\n".join(lines)

        @tool
        def register_tool(tool_name: str) -> str:
            """把一个工具注册为当前会话可用。调用前先用 list_available_tools 看名字。

            Args:
                tool_name: 要注册的工具名
            """
            got = registry.get(tool_name)
            if got is None:
                return f"未找到工具 {tool_name}。可用：{', '.join(registry.names()) or '无'}"
            return f"已注册 {tool_name}，现在可以直接调用它。"

        return [list_available_tools, register_tool]
