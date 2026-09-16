"""脚本化聊天模型：让整套 Agent 在**没有任何 API Key**的情况下也能真实跑起来。

为什么需要它？
    langchain_core 自带的 FakeMessagesListChatModel 有两个问题：
      1. 不支持 bind_tools（create_agent 内部会调用，直接 NotImplementedError）
      2. responses 是**循环**播放的，会导致 Agent 无限调用工具直到递归上限

    这里实现的 ScriptedChatModel 解决这两点：
      - bind_tools 返回自身副本并记录工具清单
      - 按脚本顺序消费，脚本播完自动返回「收尾回复」（无工具调用，保证收敛）

脚本项的两种形态：
      1. BaseMessage         —— 直接返回
      2. callable(msgs, tools) -> BaseMessage  —— 根据当前对话动态生成，
                                  适合「先看有没有 ToolMessage，再决定下一步」这类多步推理
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field

# 脚本项类型
ScriptStep = BaseMessage | Callable[[Sequence[BaseMessage], list], BaseMessage]


class _SharedIndex:
    """跨 model_copy(deep=True) 共享的调用游标。

    为什么需要它：create_agent 每次请求都会先 `model.bind_tools(...)`，
    而 bind_tools 必须**不改动原对象**（否则会污染外部持有的模型），所以返回副本。
    问题是 pydantic 的 model_copy(deep=True) 会把普通 int 字段一起拷走，
    导致脚本游标永远从 0 开始 → 同一个 step 反复执行 → 工具被无限调用。

    解法：用自定义对象持有游标，并让 __deepcopy__ 返回自身，副本与原对象共享同一个游标。
    """

    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __deepcopy__(self, memo: dict) -> _SharedIndex:
        return self


class ScriptedChatModel(BaseChatModel):
    """按脚本回放的聊天模型，支持工具调用与伪流式输出。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[Any] = Field(default_factory=list)
    terminal_reply: str = "（脚本播完）这是脚本模型的默认收尾回复。"
    index: Any = Field(default_factory=_SharedIndex, exclude=True)
    bound_tools: Any = Field(default=None, exclude=True)

    # ------------------------------------------------------------ bind_tools
    def bind_tools(self, tools: list, **kwargs: Any) -> ScriptedChatModel:
        """记录工具清单后返回自身副本。

        create_agent 每次请求都会对模型做一次 bind_tools，真实模型会在此绑定
        JSON Schema；假模型只需「记住」即可，工具 Schema 由后面的 Σ工具执行图负责。
        """
        clone: ScriptedChatModel = self.model_copy(deep=True)
        clone.bound_tools = tools
        return clone

    # ------------------------------------------------------------ 取下一条消息
    @property
    def current_step(self) -> int:
        """当前脚本游标（也便于外部断言「跑到了第几步」）。"""
        return self.index.value

    def reset(self) -> None:
        """把游标拨回起点，让同一份脚本可以重跑一遍。"""
        self.index.value = 0

    def _next_message(self, messages: Sequence[BaseMessage]) -> BaseMessage:
        idx = self.index.value
        self.index.value += 1

        if idx >= len(self.script):
            # 脚本耗尽：返回无工具调用的收尾消息，避免 RecursionError
            return AIMessage(content=self.terminal_reply)

        step = self.script[idx]
        if callable(step):
            return step(messages, self.bound_tools or [])
        return step

    # ------------------------------------------------------------ 同步生成
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    # ------------------------------------------------------------ 伪流式
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """把一条消息切成 token 级别 chunk，用于演示 stream(stream_mode="messages")。"""
        msg = self._next_message(messages)

        # 带工具调用时：先发一个承载 tool_call 的 chunk
        if getattr(msg, "tool_calls", None):
            # 注意：tool_call_chunks 的 args 必须是 **JSON 字符串**，
            # 传 dict 会让 AIMessageChunk 的 pydantic 校验直接失败
            # （真实模型的增量块也是这个约定，这里必须对齐）
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": tc["name"],
                            "args": tc["args"] if isinstance(tc.get("args"), str) else json.dumps(tc.get("args") or {}),
                            "id": tc.get("id"),
                        }
                        for tc in msg.tool_calls
                    ],
                )
            )
            return

        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        # 英文按空白词切分（贴合真实 token）；中文无空格，退化为按 2 字切分
        pieces = [p for p in re.split(r"(\s+)", content) if p]
        if len(pieces) <= 1 and content:
            pieces = [content[i : i + 2] for i in range(0, len(content), 2)]
        for idx, piece in enumerate(pieces):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=piece,
                    chunk_position="last" if idx == len(pieces) - 1 else None,
                )
            )

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"


# ---------------------------------------------------------------- 脚本辅助函数
def tool_call(name: str, args: dict | None = None, call_id: str = "call_1", content: str = "") -> AIMessage:
    """构造一条「调用工具」的模型回复。"""
    return AIMessage(content=content, tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


def has_tool_output(messages: Sequence[BaseMessage], tool_name: str | None = None) -> ToolMessage | None:
    """判断历史里是否已经出现某个工具的执行结果。"""
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and (tool_name is None or getattr(m, "name", None) == tool_name):
            return m
    return None


def message_text(message: Any) -> str:
    """把消息内容归一化成纯文本。

    LangChain 1.x 的 content 可能是 str、list[dict]（标准 Content Blocks）
    或 list[ContentBlock 对象]（MCP 工具返回值就是这种）。
    直接 str() 会把区块结构打出来，日志可读性很差，所以这里统一摊平。
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict):
                parts.append(blk.get("text") or blk.get("content") or str(blk))
            else:
                parts.append(str(getattr(blk, "text", blk)))
        return " ".join(p for p in parts if p)
    return "" if content is None else str(content)


def last_tool_result(messages: Sequence[BaseMessage]) -> str:
    """取最近一条工具结果的纯文本内容，供脚本里的 lambda 使用。"""
    found = has_tool_output(messages)
    return message_text(found) if found else ""


# ---------------------------------------------------------------- 默认脚本
def default_script() -> list[ScriptStep]:
    """默认演示脚本：先查时间 → 再回答。

    用 lambda 而非写死内容，是为了让不同场景都能复用同一个模型对象。
    """

    def step(messages: Sequence[BaseMessage], tools: list) -> BaseMessage:
        if has_tool_output(messages, "get_current_time"):
            return AIMessage(content=f"已拿到时间：{last_tool_result(messages)}。我可以继续为你工作了。")
        return tool_call("get_current_time", {})

    return [step]


def build_scripted_model(script: list[ScriptStep] | None = None) -> ScriptedChatModel:
    """便捷构造器；默认脚本 = 调一次时间工具后收尾。"""
    return ScriptedChatModel(script=script or default_script(), terminal_reply="（脚本播完）本次演示结束。")
