"""Agent 服务层 —— 对标 Java 的 @Service。

职责：装配 Agent、流式执行、读历史、处理人工介入。
Controller 只负责协议（HTTP/SSE），不碰 LangChain 细节。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from agent_kit.app import MODE_HELP, AppConfig, BuiltApp, build_app, build_app_async
from agent_kit.streaming import astream_events, stream_events, text_of_message

# 缓存键：(mode, provider, user_id, role, enable_mcp)
AppKey = tuple


class AgentService:
    """按 (mode, provider, user_id, role, enable_mcp) 缓存已装配好的应用。

    MCP 那一份必须缓存：每次装配都会拉起一个 MCP Server 子进程（stdio），
    不复用就是每问一句起一个进程。
    """

    def __init__(self) -> None:
        self._apps: dict[AppKey, BuiltApp] = {}
        self._locks: dict[AppKey, Any] = {}

    # ------------------------------------------------------------ 装配
    @staticmethod
    def _key(mode: str, provider: str | None, user_id: str, role: str, enable_mcp: bool) -> AppKey:
        return (mode, provider, user_id, role, enable_mcp)

    def _cfg(self, *, mode, provider, user_id, role, thread_id, enable_mcp) -> AppConfig:
        if mode not in MODE_HELP:
            raise ValueError(f"未知模式：{mode}，可选：{'、'.join(MODE_HELP)}")
        return AppConfig(
            provider=provider, mode=mode, user_id=user_id, role=role,
            thread_id=thread_id, enable_hitl=True, enable_mcp=enable_mcp,
        )

    def get(self, *, mode: str, provider: str | None, user_id: str, role: str, thread_id: str, enable_mcp: bool = False) -> BuiltApp:
        """同步装配，仅用于**不含 MCP 工具**的场景。"""
        key = self._key(mode, provider, user_id, role, enable_mcp)
        app = self._apps.get(key)
        if app is None:
            cfg = self._cfg(mode=mode, provider=provider, user_id=user_id, role=role, thread_id=thread_id, enable_mcp=enable_mcp)
            if cfg.needs_mcp:
                raise RuntimeError("该配置含 MCP 工具，请用 await aget() 异步装配")
            app = build_app(cfg)
            self._apps[key] = app
        app.config.thread_id = thread_id
        return app

    async def aget(self, *, mode: str, provider: str | None, user_id: str, role: str, thread_id: str, enable_mcp: bool = True) -> BuiltApp:
        """异步装配（含 MCP）。用锁避免并发时拉起多个 MCP Server 进程。"""
        import asyncio

        key = self._key(mode, provider, user_id, role, enable_mcp)
        app = self._apps.get(key)
        if app is not None:
            app.config.thread_id = thread_id
            return app

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._apps:
                cfg = self._cfg(mode=mode, provider=provider, user_id=user_id, role=role, thread_id=thread_id, enable_mcp=enable_mcp)
                self._apps[key] = await build_app_async(cfg)
        app = self._apps[key]
        app.config.thread_id = thread_id
        return app

    def apps(self) -> list[BuiltApp]:
        return list(self._apps.values())

    async def aclose_mcp(self) -> None:
        """关闭所有已连接的 MCP Hub（收掉 stdio 子进程）。"""
        for app in self._apps.values():
            hub = getattr(app, "mcp_hub", None)
            if hub is not None:
                await hub.aclose()
        self._apps.clear()

    def reload(self) -> None:
        """切换 provider / 改配置后清空缓存。"""
        self._apps.clear()
        self._locks.clear()

    def mcp_tools(self) -> list[str]:
        """当前**已连接**的 MCP 工具名（没连过就返回空，不主动拉起进程）。"""
        for app in self._apps.values():
            if getattr(app, "is_mcp", False):
                return list(getattr(app, "mcp_tool_names", []))
        return []

    async def amcp_tools(self, *, mode: str = "chat", provider: str | None = None,
                         user_id: str = "demo", role: str = "admin",
                         thread_id: str = "atlas-main", connect: bool = True) -> list[str]:
        """要拿工具清单就先把 MCP 连上（结果会缓存，后续对话直接复用）。"""
        if not connect:
            return self.mcp_tools()
        app = await self.aget(mode=mode, provider=provider, user_id=user_id, role=role,
                              thread_id=thread_id, enable_mcp=True)
        return list(getattr(app, "mcp_tool_names", []))

    # ------------------------------------------------------------ 执行
    def stream(self, req: Any) -> Iterator[dict[str, Any]]:
        """流式执行，产出一串给 SSE 用的事件字典。

        事件类型：
            token      模型增量文本
            tool_start 开始调用工具
            tool_end   工具返回
            custom     工具内部推的自定义进度
            interrupt  需要人工确认
            done       本轮结束
            error      出错
        """
        app = self.get(
            mode=req.mode,
            provider=req.provider,
            user_id=req.user_id,
            role=req.role,
            thread_id=req.thread_id,
        )
        payload = self._payload(app, req.message)

        try:
            for kind, data in stream_events(
                app.graph,
                payload,
                modes=("messages", "updates", "custom"),
                config=app.thread_config,
                context=app.context,
            ):
                if kind == "messages":
                    chunk = data[0] if isinstance(data, tuple) else data
                    piece = text_of_message(chunk)
                    if piece:
                        yield {"type": "token", "data": piece}
                elif kind == "custom":
                    yield {"type": "custom", "data": str(data)}
                elif kind == "updates":
                    yield from self._on_update(data)

            yield {"type": "done", "data": {"thread_id": req.thread_id}}
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}

    def resume(self, req: Any) -> Iterator[dict[str, Any]]:
        """人工确认后继续执行。"""
        app = self.get(
            mode=req.mode,
            provider=req.provider,
            user_id=req.user_id,
            role=req.role,
            thread_id=req.thread_id,
        )
        command = Command(resume={"decisions": req.decisions})
        try:
            for kind, data in stream_events(
                app.graph,
                command,
                modes=("messages", "updates", "custom"),
                config=app.thread_config,
                context=app.context,
            ):
                if kind == "messages":
                    chunk = data[0] if isinstance(data, tuple) else data
                    piece = text_of_message(chunk)
                    if piece:
                        yield {"type": "token", "data": piece}
                elif kind == "updates":
                    yield from self._on_update(data)
            yield {"type": "done", "data": {"thread_id": req.thread_id}}
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}

    def _on_update(self, data: Any) -> Iterator[dict[str, Any]]:
        """把图节点的状态增量翻译成前端看得懂的事件。"""
        if not isinstance(data, dict):
            return
        for value in data.values():
            if not isinstance(value, dict):
                continue
            # 人工介入：LangGraph 把中断放在 __interrupt__ 里
            if interrupts := value.get("__interrupt__"):
                payload = [getattr(i, "value", i) for i in interrupts]
                yield {"type": "interrupt", "data": payload}
            for msg in value.get("messages", []) or []:
                if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                    for call in msg.tool_calls:
                        yield {
                            "type": "tool_start",
                            "data": {"name": call.get("name", ""), "args": call.get("args", {})},
                        }
                elif isinstance(msg, ToolMessage):
                    content = msg.content
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False, default=str)
                    yield {
                        "type": "tool_end",
                        "data": {"name": getattr(msg, "name", "") or "", "output": content[:4000]},
                    }

    # ------------------------------------------------------------ 异步执行（MCP）
    async def astream(self, req: Any):
        """异步流式，MCP 场景专用（MCP 工具没有同步实现）。"""
        app = await self.aget(
            mode=req.mode, provider=req.provider, user_id=req.user_id,
            role=req.role, thread_id=req.thread_id, enable_mcp=getattr(req, "enable_mcp", True),
        )
        payload = self._payload(app, req.message)
        try:
            async for kind, data in astream_events(
                app.graph,
                payload,
                modes=("messages", "updates", "custom"),
                config=app.thread_config,
                context=app.context,
            ):
                if kind == "messages":
                    chunk = data[0] if isinstance(data, tuple) else data
                    piece = text_of_message(chunk)
                    if piece:
                        yield {"type": "token", "data": piece}
                elif kind == "custom":
                    yield {"type": "custom", "data": str(data)}
                elif kind == "updates":
                    for ev in self._on_update(data):
                        yield ev
            yield {"type": "done", "data": {"thread_id": req.thread_id, "mcp": True}}
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}

    async def aresume(self, req: Any):
        """MCP 场景下的人工确认恢复。"""
        app = await self.aget(
            mode=req.mode, provider=req.provider, user_id=req.user_id,
            role=req.role, thread_id=req.thread_id, enable_mcp=getattr(req, "enable_mcp", True),
        )
        command = Command(resume={"decisions": req.decisions})
        try:
            async for kind, data in astream_events(
                app.graph, command,
                modes=("messages", "updates", "custom"),
                config=app.thread_config, context=app.context,
            ):
                if kind == "messages":
                    chunk = data[0] if isinstance(data, tuple) else data
                    piece = text_of_message(chunk)
                    if piece:
                        yield {"type": "token", "data": piece}
                elif kind == "updates":
                    for ev in self._on_update(data):
                        yield ev
            yield {"type": "done", "data": {"thread_id": req.thread_id}}
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "data": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _payload(app: BuiltApp, text: str) -> Any:
        """router 工作流和其他模式的入参形状不同。"""
        if app.is_workflow:
            return {"query": text}
        return {"messages": [HumanMessage(content=text)]}

    # ------------------------------------------------------------ 历史
    def _find_cached(self, *, mode: str, provider: str | None, user_id: str, role: str, thread_id: str) -> BuiltApp | None:
        """优先复用已装配的应用（MCP 那份不能重建，否则会再起一个 Server 进程）。"""
        for enable_mcp in (True, False):
            app = self._apps.get(self._key(mode, provider, user_id, role, enable_mcp))
            if app is not None:
                app.config.thread_id = thread_id
                return app
        return None

    def _plain(self, *, mode: str, provider: str | None, user_id: str, role: str, thread_id: str) -> BuiltApp | None:
        """拿一个「非 MCP」的应用用于读状态；拿不到就返回 None。"""
        app = self._find_cached(mode=mode, provider=provider, user_id=user_id, role=role, thread_id=thread_id)
        if app is not None:
            return app
        try:
            return self.get(mode=mode, provider=provider, user_id=user_id, role=role, thread_id=thread_id, enable_mcp=False)
        except Exception:  # noqa: BLE001 —— mcp 模式无法同步装配
            return None

    def history(self, *, thread_id: str, mode: str, provider: str | None, user_id: str, role: str) -> list[dict[str, Any]]:
        app = self._plain(mode=mode, provider=provider, user_id=user_id, role=role, thread_id=thread_id)
        if app is None:
            return []
        try:
            state = app.graph.get_state(app.thread_config)
        except Exception:  # noqa: BLE001
            return []
        raw = (state.values or {}).get("messages", []) if state else []
        out: list[dict[str, Any]] = []
        for m in raw:
            role_name = "user" if isinstance(m, HumanMessage) else ("assistant" if isinstance(m, AIMessage) else "tool")
            text = text_of_message(m)
            # 只有工具调用、没有正文的 AI 消息，正文留给前端的工具 chip 表达，别重复渲染
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None) and text.startswith("[调用工具]"):
                text = ""
            item: dict[str, Any] = {"role": role_name, "content": text}
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
                item["tool_calls"] = [
                    {"name": c.get("name", ""), "args": c.get("args", {})} for c in m.tool_calls
                ]
            if isinstance(m, ToolMessage):
                item["tool_call_id"] = getattr(m, "tool_call_id", None)
                item["name"] = getattr(m, "name", None)
            out.append(item)
        return out

    def delete_thread(self, *, thread_id: str, mode: str, provider: str | None, user_id: str, role: str) -> bool:
        app = self._plain(mode=mode, provider=provider, user_id=user_id, role=role, thread_id=thread_id)
        if app is None:
            return False
        try:
            app.checkpointer.delete_thread(thread_id)
            return True
        except Exception:  # noqa: BLE001
            return False


def new_thread_id() -> str:
    return f"web-{uuid.uuid4().hex[:8]}"
