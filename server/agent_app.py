"""Agent 执行服务（独立进程）。

    python -m server.agent_app --port 8001

职责只有一个：**跑图**。对话流式执行、人工确认恢复、排队消息、会话历史、
MCP 工具、凭据（密钥要落在这个进程的环境里，否则装配出来的模型鉴权失败）。

记忆偏好 / 状态这些不归它管——那是 server/memory_app 的事；
会话存储（checkpointer / store）是共享后端，两边各自直连即可。
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.deps import get_agent_service


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # 凭据文件坏了只该让用户重填一次，不该拦住服务启动
    try:
        from agent_kit import credentials as creds

        loaded = creds.store.load_persisted()
        if loaded:
            from agent_kit.logging_conf import get_logger

            get_logger("agent.service").info("已载入本机凭据：%s", "、".join(loaded))
    except Exception:  # noqa: BLE001
        pass

    yield

    # MCP 会拉起 stdio 子进程，不关就会残留
    try:
        await get_agent_service().aclose_mcp()
    except Exception:  # noqa: BLE001
        pass


def create_agent_app() -> FastAPI:
    from server.errors import register_exception_handlers, register_request_logging
    from server.routers import chat, credentials, system

    app = FastAPI(title="Atlas Agent Service", version="1.0.0", lifespan=_lifespan)
    register_exception_handlers(app)
    register_request_logging(app)

    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(credentials.router)
    return app


app = create_agent_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas Agent 执行服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("server.agent_app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
