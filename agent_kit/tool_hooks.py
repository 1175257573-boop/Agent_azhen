"""工具调用钩子的「同步 + 异步」双实现支持。

**为什么需要这个文件**

LangChain 1.x 的 `@wrap_tool_call` 装饰器只为它装饰的那一种函数生成实现：
  · 装饰 `def fn(...)`   → 只有 `wrap_tool_call`
  · 装饰 `async def fn` → 只有 `awrap_tool_call`

而 LangGraph 的执行路径由**调用方式**决定：
  · `agent.stream()` / `invoke()`  → 走 `wrap_tool_call`
  · `agent.astream()` / `ainvoke()` → 走 `awrap_tool_call`

一旦缺对应实现，LangChain 会直接抛
`NotImplementedError: Asynchronous implementation of awrap_tool_call is not available.`

本项目两种调用方式都有（Web 端 MCP 必须异步，CLI 演示多为同步），
所以每个自定义工具钩子都要**同时**给出两个版本。这里提供一个小工具类来承载。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware


class _NamedMixin:
    """`AgentMiddleware.name` 是只读属性（默认取类名），所以名字要靠子类名来带。"""

    _display_name: str = "dual_hook"

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._display_name


class DualToolMiddleware(_NamedMixin, AgentMiddleware):
    """同一个中间件，同时提供 wrap_tool_call 与 awrap_tool_call。"""

    def __init__(self, sync_fn: Any, async_fn: Any, name: str | None = None) -> None:
        super().__init__()
        self._sync_fn = sync_fn
        self._async_fn = async_fn
        self._display_name = name or getattr(sync_fn, "__name__", "dual_tool_hook")

    # 同步链路：CLI / 同步 stream
    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        return self._sync_fn(request, handler)

    # 异步链路：astream / ainvoke（MCP 工具只有 ainvoke，必须走这条）
    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        return await self._async_fn(request, handler)


def dual(sync_fn: Any, async_fn: Any, name: str | None = None) -> DualToolMiddleware:
    """把一对 (同步实现, 异步实现) 打包成一个中间件实例。"""
    return DualToolMiddleware(sync_fn, async_fn, name=name)


class DualModelMiddleware(_NamedMixin, AgentMiddleware):
    """同理，`wrap_model_call` / `awrap_model_call` 也要成对提供。"""

    def __init__(self, sync_fn: Any, async_fn: Any, name: str | None = None) -> None:
        super().__init__()
        self._sync_fn = sync_fn
        self._async_fn = async_fn
        self._display_name = name or getattr(sync_fn, "__name__", "dual_model_hook")

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return self._sync_fn(request, handler)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await self._async_fn(request, handler)


def dual_model(
    sync_fn: Any,
    async_fn: Any,
    name: str | None = None,
    state_schema: type | None = None,
) -> DualModelMiddleware:
    """打包模型侧钩子；需要自定义 state_schema 时传进来（会动态生成子类）。"""
    if state_schema is None:
        return DualModelMiddleware(sync_fn, async_fn, name=name)

    class _TypedDualModel(DualModelMiddleware):
        pass

    _TypedDualModel.state_schema = state_schema
    return _TypedDualModel(sync_fn, async_fn, name=name)
