"""对话 Controller —— 对标 Java 的 @RestController。

只做协议转换：HTTP → Service → SSE。业务逻辑一律不写在这里。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from server.schemas import ChatRequest, MessageOut, QueuedOut, QueueIn, ResumeRequest, ThreadBrief
from server.service.agent_service import AgentService, new_thread_id

router = APIRouter(prefix="/api/chat", tags=["chat"])

# SSE 心跳间隔（秒）：MCP 首次连接要拉起子进程，前端得知道后端还活着
HEARTBEAT = 15.0


def _svc() -> AgentService:
    from server.app import get_agent_service

    return get_agent_service()


def _wants_mcp(req: Any) -> bool:
    """是否要走 MCP 链路：显式开关，或本身就是 mcp 模式。

    MCP 工具只实现了 ainvoke，同步调用会抛
    `NotImplementedError: StructuredTool does not support sync invocation`，
    所以这里必须分岔到 async generator。
    """
    return bool(getattr(req, "enable_mcp", False)) or getattr(req, "mode", "chat") == "mcp"


async def _sse(events: AsyncIterator[dict[str, Any]]):
    """把事件流包装成 SSE 文本，并定期发心跳。"""
    task = asyncio.ensure_future(events.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT)
            if not done:
                yield ": ping\n\n"
                continue
            try:
                event = task.result()
            except StopAsyncIteration:
                break
            task = asyncio.ensure_future(events.__anext__())
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        task.cancel()


@router.post("/stream")
async def stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式对话。前端用 fetch + ReadableStream 消费。

    MCP 开启时走 async generator（`astream`），否则沿用同步 `stream`。
    """
    svc = _svc()
    if _wants_mcp(req):
        return StreamingResponse(_sse(svc.astream(req)), media_type="text/event-stream")

    def gen() -> Any:
        for event in svc.stream(req):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/resume")
async def resume(req: ResumeRequest) -> StreamingResponse:
    """人工确认（approve / edit / reject / respond）后继续执行。"""
    svc = _svc()
    if _wants_mcp(req):
        return StreamingResponse(_sse(svc.aresume(req)), media_type="text/event-stream")

    def gen() -> Any:
        for event in svc.resume(req):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/mcp-tools")
async def mcp_tools(connect: bool = True, mode: str = "chat", provider: str | None = None,
                    user_id: str = "demo", role: str = "admin"):
    """列出 MCP Server 提供的工具。

    connect=false 时只返回**已经连上**的工具名，不会拉起新进程。
    """
    try:
        names = await _svc().amcp_tools(connect=connect, mode=mode, provider=provider,
                                        user_id=user_id, role=role)
    except Exception as exc:  # noqa: BLE001 —— MCP 连不上不能让整个页面挂掉
        raise HTTPException(status_code=502, detail=f"MCP 连接失败：{type(exc).__name__}: {exc}")
    return {"connected": bool(names), "tools": names}


# ---------------------------------------------------------------- 排队消息
# 存在的理由：Agent 一轮要跑几十秒，这期间用户的输入不能丢、也不能并发打断当前轮。
# 做法是「入队 → 本轮 done 后由服务端自动按序执行」，前端只负责展示与增删改。
@router.get("/queue", response_model=list[QueuedOut])
def queue_list(thread_id: str = "atlas-main"):
    """列出该会话中等待发送的消息。"""
    return _svc().queue_pending(thread_id=thread_id)


@router.post("/queue", response_model=QueuedOut)
def queue_add(req: QueueIn):
    """入队一条消息；带 item_id 时表示编辑已有的排队项。

    队列满（20 条）返回 429——宁可让前端明确提示，也不要静默丢弃用户的输入。
    """
    svc = _svc()
    try:
        if req.item_id:
            updated = svc.queue_update(thread_id=req.thread_id, item_id=req.item_id, text=req.message)
            if updated:
                return updated
        return svc.enqueue(thread_id=req.thread_id, text=req.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:  # QueueFullError
        raise HTTPException(status_code=429, detail=str(exc))


@router.delete("/queue/{item_id}")
def queue_remove(item_id: str, thread_id: str = "atlas-main"):
    """撤回一条还没发出的排队消息。"""
    ok = _svc().queue_remove(thread_id=thread_id, item_id=item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该排队项（可能已经发出）")
    return {"ok": True, "id": item_id}


@router.delete("/queue")
def queue_clear(thread_id: str = "atlas-main"):
    """清空该会话的排队消息（切会话 / 中止生成时用）。"""
    return {"ok": True, "cleared": _svc().queue_clear(thread_id=thread_id)}


@router.get("/history", response_model=list[MessageOut])
def history(thread_id: str, mode: str = "chat", provider: str | None = None, user_id: str = "demo", role: str = "admin"):
    return _svc().history(thread_id=thread_id, mode=mode, provider=provider, user_id=user_id, role=role)


@router.get("/threads", response_model=list[ThreadBrief])
def threads():
    from server.app import get_memory_service

    return [
        ThreadBrief(thread_id=t["thread_id"], message_count=t.get("message_count", 0))
        for t in get_memory_service().threads()
    ]


@router.post("/threads", response_model=ThreadBrief)
def new_thread():
    return ThreadBrief(thread_id=new_thread_id(), message_count=0)


@router.delete("/threads/{thread_id}")
def delete_thread(thread_id: str, mode: str = "chat", provider: str | None = None, user_id: str = "demo", role: str = "admin"):
    ok = _svc().delete_thread(thread_id=thread_id, mode=mode, provider=provider, user_id=user_id, role=role)
    if not ok:
        raise HTTPException(status_code=400, detail="删除失败：当前 checkpointer 可能不支持该操作")
    return {"ok": True, "thread_id": thread_id}
