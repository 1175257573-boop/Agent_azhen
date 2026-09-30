"""统一运行层 —— CLI 与 Web 共用同一条执行链路。

以前 `ui.py`（终端）和 `server/service/agent_service.py`（HTTP）各写了一套
「跑图 → 翻译事件 → 落流水」的循环。两套实现必然漂移，实际已经漂了：

    * CLI 落会话流水，Web 不落（记忆 Phase 1 永远看不到 Web 对话）
    * 两边都没认出 LangGraph 的中断（见 extract_interrupts 的注释），
      一个不弹审批、一个不发审批卡片

这里把这件事收成一处。调用方只负责**渲染**：

    for event in AgentRuntime(app, source="cli").run(text, thread_id):
        ...

事件是协议，不是打印格式：

    token       逐字输出            data: str
    custom      自定义流            data: str
    tool_start  工具开始            data: {name, args}
    tool_end    工具结束            data: {name, output}
    interrupt   需要人工确认        data: [ {action_requests, review_configs}, ... ]
    done        本轮结束            data: {thread_id, interrupted, ...}
    error       异常                data: "TypeError: ..."

设计约束（都是踩过的坑）：
    * thread_id 每次显式传入，**绝不写回共享的 BuiltApp**——
      并发两个会话互相覆盖 thread_id 曾导致「A 的消息写进 B 的会话」
    * 中断识别只有一处实现，避免两边再次漂移
    * 落流水是运行层的职责，调用方不必记得调用它
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from agent_kit import memory as mem
from agent_kit import rollout
from agent_kit.logging_conf import get_logger
from agent_kit.streaming import astream_events, stream_events, text_of_message

log = get_logger("agent.runtime")

# ---------------------------------------------------------------- 事件协议
TOKEN = "token"
CUSTOM = "custom"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
INTERRUPT = "interrupt"
DONE = "done"
ERROR = "error"

_MODES = ("messages", "updates", "custom")


def extract_interrupts(data: Any) -> list[Any]:
    """从 updates 事件 / invoke 结果里挑出中断载荷。

    LangGraph 把中断放在 **chunk 顶层**：`{"__interrupt__": (Interrupt(...),)}`，
    值是 tuple 不是 dict。早期两边都只遍历 `data.values()` 并要求
    `isinstance(value, dict)`，整条被静默跳过——CLI 不弹审批、Web 不发审批卡片。
    顶层与「嵌套在节点下」两种形状都认。
    """
    if not isinstance(data, dict):
        return []

    raw = data.get("__interrupt__")
    if raw:
        return _unwrap_interrupts(raw)

    found: list[Any] = []
    for value in data.values():
        if isinstance(value, dict) and value.get("__interrupt__"):
            found.extend(_unwrap_interrupts(value["__interrupt__"]))
    return found


def _unwrap_interrupts(raw: Any) -> list[Any]:
    """Interrupt 对象 → 前端/CLI 能直接用的 dict。"""
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [getattr(item, "value", item) for item in items]


class AgentRuntime:
    """一次「跑图」的封装：装配物 + 会话来源 + 事件翻译。

    Args:
        app: build_app 的产物（图 / 配置 / 上下文都在这里）
        source: 会话来源，决定这条流水会不会进长期记忆（见 rollout 模块说明）
        record: 是否落会话流水；关掉用于「只想跑一下不想留痕」的场景
    """

    def __init__(self, app: Any, *, source: str = rollout.DEFAULT_SOURCE, record: bool = True) -> None:
        self.app = app
        self.source = source
        self.record = record

    # ------------------------------------------------------------ 配置
    def config(self, thread_id: str) -> dict:
        """本次执行用的线程配置。**每次现算，不写回 BuiltApp**。"""
        return mem.thread_config(thread_id)

    def context(self) -> dict:
        """运行时上下文（user_id / role / locale）。"""
        cfg = self.app.config
        return {"user_id": cfg.user_id, "role": cfg.role, "locale": "zh-CN"}

    def payload(self, text: str) -> dict:
        """用户输入 → 图的入参。router 这类自定义工作流吃 query，其余吃 messages。"""
        if getattr(self.app, "is_workflow", False):
            return {"query": text}
        return {"messages": [HumanMessage(content=text)]}

    def resume_payload(self, decisions: Sequence[dict]) -> Command:
        return Command(resume={"decisions": list(decisions)})

    # ------------------------------------------------------------ 同步执行
    def run(self, text: str, thread_id: str, *, extra: dict | None = None) -> Iterator[dict]:
        """流式跑一轮，yield 归一化事件。"""
        interrupted = False
        try:
            for kind, data in stream_events(
                self.app.graph,
                self.payload(text),
                modes=_MODES,
                config=self.config(thread_id),
                context=self.context(),
            ):
                for event in self._translate(kind, data):
                    if event["type"] == INTERRUPT:
                        interrupted = True
                    yield event
            yield self._done(thread_id, interrupted, extra)
        except Exception as exc:  # noqa: BLE001 - 异常要变成事件流里的 error，不能逃出生成器
            yield {"type": ERROR, "data": f"{type(exc).__name__}: {exc}"}
        finally:
            self.record_rollout(thread_id)

    def resume(self, decisions: Sequence[dict], thread_id: str, *, extra: dict | None = None) -> Iterator[dict]:
        """人工决策后继续执行。"""
        interrupted = False
        try:
            for kind, data in stream_events(
                self.app.graph,
                self.resume_payload(decisions),
                modes=_MODES,
                config=self.config(thread_id),
                context=self.context(),
            ):
                for event in self._translate(kind, data):
                    if event["type"] == INTERRUPT:
                        interrupted = True
                    yield event
            yield self._done(thread_id, interrupted, extra)
        except Exception as exc:  # noqa: BLE001
            yield {"type": ERROR, "data": f"{type(exc).__name__}: {exc}"}
        finally:
            self.record_rollout(thread_id)

    def invoke(self, text: str, thread_id: str) -> dict:
        """非流式跑一轮（CLI 的 /stream 关掉时走这条）。

        返回 {"result": 图结果, "interrupts": [...]}；中断不自动恢复，交给调用方问用户。
        """
        try:
            result = self.app.graph.invoke(
                self.payload(text), config=self.config(thread_id), context=self.context()
            )
        except Exception as exc:  # noqa: BLE001
            return {"result": None, "interrupts": [], "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self.record_rollout(thread_id)
        return {"result": result, "interrupts": extract_interrupts(result)}

    # ------------------------------------------------------------ 异步执行（MCP）
    async def arun(self, text: str, thread_id: str, *, extra: dict | None = None) -> AsyncIterator[dict]:
        """MCP 工具只有 ainvoke，整条链路必须异步；中断不在这里问用户。"""
        interrupted = False
        try:
            async for kind, data in astream_events(
                self.app.graph,
                self.payload(text),
                modes=_MODES,
                config=self.config(thread_id),
                context=self.context(),
            ):
                for event in self._translate(kind, data):
                    if event["type"] == INTERRUPT:
                        interrupted = True
                    yield event
            yield self._done(thread_id, interrupted, extra)
        except Exception as exc:  # noqa: BLE001
            yield {"type": ERROR, "data": f"{type(exc).__name__}: {exc}"}
        finally:
            self.record_rollout(thread_id)

    async def aresume(self, decisions: Sequence[dict], thread_id: str, *, extra: dict | None = None) -> AsyncIterator[dict]:
        interrupted = False
        try:
            async for kind, data in astream_events(
                self.app.graph,
                self.resume_payload(decisions),
                modes=_MODES,
                config=self.config(thread_id),
                context=self.context(),
            ):
                for event in self._translate(kind, data):
                    if event["type"] == INTERRUPT:
                        interrupted = True
                    yield event
            yield self._done(thread_id, interrupted, extra)
        except Exception as exc:  # noqa: BLE001
            yield {"type": ERROR, "data": f"{type(exc).__name__}: {exc}"}
        finally:
            self.record_rollout(thread_id)

    # ------------------------------------------------------------ 事件翻译
    def _translate(self, kind: str, data: Any) -> Iterator[dict]:
        """把 LangGraph 的原始事件翻译成运行层事件协议。"""
        if kind == "messages":
            chunk = data[0] if isinstance(data, tuple) else data
            piece = text_of_message(chunk)
            if piece:
                yield {"type": TOKEN, "data": piece}
            return

        if kind == "custom":
            yield {"type": CUSTOM, "data": str(data)}
            return

        if kind != "updates" or not isinstance(data, dict):
            return

        # 中断优先：这一轮停在这里等人工决策
        if interrupts := extract_interrupts(data):
            yield {"type": INTERRUPT, "data": interrupts}

        for value in data.values():
            if not isinstance(value, dict):
                continue
            for msg in value.get("messages", []) or []:
                if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                    for call in msg.tool_calls:
                        yield {
                            "type": TOOL_START,
                            "data": {"name": call.get("name", ""), "args": call.get("args", {})},
                        }
                elif isinstance(msg, ToolMessage):
                    content = msg.content
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False, default=str)
                    yield {
                        "type": TOOL_END,
                        "data": {"name": getattr(msg, "name", "") or "", "output": content[:4000]},
                    }

    @staticmethod
    def _done(thread_id: str, interrupted: bool, extra: dict | None) -> dict:
        data = {"thread_id": thread_id, "interrupted": interrupted}
        if extra:
            data.update(extra)
        return {"type": DONE, "data": data}

    # ------------------------------------------------------------ 流水
    def record_rollout(self, thread_id: str) -> None:
        """把本轮增量落进会话流水。失败绝不能影响对话本身。"""
        if not self.record:
            return
        try:
            rollout.sync_from_checkpoint(
                self.app.graph,
                thread_id,
                config=self.config(thread_id),
                source=self.source,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("会话流水落盘失败（已忽略）：%s: %s", type(exc).__name__, exc)
