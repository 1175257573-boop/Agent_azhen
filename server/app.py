"""FastAPI 应用工厂 —— 对标 Spring Boot 里带 @SpringBootApplication 的启动类。

分层：
    server/app.py          应用装配 + 生命周期（≈ Application）
    server/routers/*.py    Controller
    server/service/*.py    Service
    agent_kit/*            Domain / Repository（tools、memory、middleware）
    server/schemas.py      DTO

启动：
    python main.py web
    uvicorn server.app:app --reload
    python -m server
"""

from __future__ import annotations

import warnings
from contextlib import asynccontextmanager
from pathlib import Path

warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.service.agent_service import AgentService
from server.service.memory_service import MemoryService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

# 进程级单例（≈ Spring 的 Bean 容器）
_agent_service = AgentService()
_memory_service = MemoryService()


def get_agent_service() -> AgentService:
    return _agent_service


def get_memory_service() -> MemoryService:
    return _memory_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时预热记忆后端，让「连不上 Redis/PG」在第一屏就暴露。"""
    from agent_kit import memory as mem
    from agent_kit.logging_conf import get_logger

    log = get_logger("web")
    log.info("记忆层：短期=%s 长期=%s", mem.short_backend(), mem.long_backend())
    try:
        mem.build_checkpointer()
        mem.build_store()
    except Exception as exc:  # noqa: BLE001
        log.warning("记忆后端预热失败（将降级为内存）：%s", exc)

    yield

    # 收尾：MCP 会拉起 stdio 子进程，不关就会残留
    try:
        await _agent_service.aclose_mcp()
    except Exception as exc:  # noqa: BLE001
        log.warning("关闭 MCP 连接失败：%s: %s", type(exc).__name__, exc)
    else:
        log.info("已退出，MCP 连接已释放")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Atlas · LangChain 1.4 Agent",
        description="LangChain 1.4 + LangGraph 全功能 Agent，短期记忆 Redis、长期记忆 PostgreSQL",
        version="1.0.0",
        lifespan=lifespan,
    )

    from server.errors import register_exception_handlers, register_request_logging
    from server.routers import chat, memory, system

    # 顺序有讲究：先装异常处理器/日志，再挂路由
    register_exception_handlers(app)
    register_request_logging(app)

    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(memory.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()
