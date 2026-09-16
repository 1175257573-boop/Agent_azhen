"""流式输出：四种模式的统一封装。

| 模式 | 给谁看 | 数据形态 |
|---|---|---|
| updates | 前端步骤条 | 每个图节点完成后的状态增量（含完整工具调用） |
| messages | 打字机效果 | LLM token 增量 + 元数据（langgraph_node） |
| custom | 长任务进度 | 工具内部 writer("...") 推的自定数据 |
| 组合 | 生产 UI | 传 list 一次订阅多种 |

本项目统一用 `version="v2"`：所有 chunk 变成 `{type, ns, data}` 的 StreamPart 字典，
而不是旧版 `(mode, chunk)` 二元组——新格式自带命名空间，做多智能体时才分得清来源。

工具里推自定义事件用 `from langgraph.config import get_stream_writer`。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from langgraph.config import get_stream_writer

__all__ = ["MODES", "astream_events", "emit", "stream_events"]


MODES = ("updates", "messages", "custom")


def emit(data: Any) -> bool:
    """在工具内部推送一条自定义流事件。

    在流式上下文之外调用（比如直接 invoke）会静默失败——这不是 bug，
    而是 LangGraph 在没有订阅者时不生产事件的设计。
    """
    try:
        get_stream_writer()(data)
        return True
    except Exception:  # noqa: BLE001 —— 非流式上下文
        return False


def stream_events(
    agent: Any,
    payload: Any,
    *,
    modes: tuple[str, ...] = ("messages",),
    config: dict | None = None,
    context: dict | None = None,
    subgraphs: bool = False,
) -> Iterator[tuple[str, Any]]:
    """统一流式迭代器，产出 `(事件类型, 数据)`。

    Args:
        modes: updates / messages / custom 的任意组合
        config: 必须含 configurable.thread_id（有记忆时需要）
        context: 运行时上下文
        subgraphs: 多智能体场景下要开，否则看不到子代理内部过程
    """
    kwargs: dict[str, Any] = {
        "stream_mode": list(modes) if len(modes) > 1 else modes[0],
        "version": "v2",
    }
    if config:
        kwargs["config"] = config
    if context is not None:
        kwargs["context"] = context
    if subgraphs:
        kwargs["subgraphs"] = True

    for chunk in agent.stream(payload, **kwargs):
        # v2 下 chunk 是 StreamPart 字典；不同小版本可能回落成元组，两种都兼容
        if isinstance(chunk, dict):
            yield chunk.get("type", "unknown"), chunk.get("data")
        else:
            mode, data = chunk
            yield mode, data


async def astream_events(
    agent: Any,
    payload: Any,
    *,
    modes: tuple[str, ...] = ("messages",),
    config: dict | None = None,
    context: dict | None = None,
    subgraphs: bool = False,
):
    """`stream_events` 的异步版本。

    **什么时候必须用它**：MCP 工具只实现了 `ainvoke`
    （同步调用会抛 `StructuredTool does not support sync invocation`），
    所以只要工具集里含 MCP 工具，整条链路就必须走 astream。
    """
    kwargs: dict[str, Any] = {
        "stream_mode": list(modes) if len(modes) > 1 else modes[0],
        "version": "v2",
    }
    if config:
        kwargs["config"] = config
    if context is not None:
        kwargs["context"] = context
    if subgraphs:
        kwargs["subgraphs"] = True

    async for chunk in agent.astream(payload, **kwargs):
        if isinstance(chunk, dict):
            yield chunk.get("type", "unknown"), chunk.get("data")
        else:
            mode, data = chunk
            yield mode, data


def text_of_message(message: Any) -> str:
    """把一条消息的增量内容摊平成可读文本。

    LangChain 1.x 的消息内容可能是 str / content_blocks(list[dict]) / list[对象]，
    content_blocks 里还会有 `reasoning`（推理）、`tool_call`（工具调用 start/end）
    等非文本块，UI 层需要按块类型分拣。
    """
    blocks = getattr(message, "content_blocks", None)
    if blocks:
        parts: list[str] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                parts.append(blk.get("text") or "")
            elif btype == "reasoning":
                parts.append(f"[推理] {blk.get('reasoning') or ''}")
            elif btype == "tool_call":
                parts.append(f"[调用工具] {blk.get('name')}")
        return "".join(parts)

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for blk in content:
            if isinstance(blk, str):
                out.append(blk)
            elif isinstance(blk, dict):
                out.append(blk.get("text") or str(blk))
            else:
                out.append(str(getattr(blk, "text", blk)))
        return "".join(out)
    return "" if content is None else str(content)
