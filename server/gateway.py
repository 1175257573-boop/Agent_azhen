"""API 网关 —— 三进程部署里对外唯一入口。

    python -m server.gateway --port 8000

路由规则：
    /api/chat/stream, /api/chat/resume     → SSE 转发 → Agent 执行服务
    /api/chat/**, /api/credentials/**      → REST 转发 → Agent 执行服务
    /api/memory/**                         → REST 转发 → 记忆服务
    /api/health                            → 本地聚合（顺带探两个下游）
    /api/info, /api/modes                  → 本地（复用 system 路由）
    / 与 /static/**                        → 前端静态资源

下游地址从环境变量读（请求时现读，测试可以临时改）：
    AGENT_SERVICE_URL   默认 http://127.0.0.1:8001
    MEMORY_SERVICE_URL  默认 http://127.0.0.1:8002

服务故障处理：下游不可达时 REST 与 SSE 一律 502 JSON，
绝不允许「200 + 空响应体」——前端会在没有线索的情况下干转圈。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.proxy import forward, forward_sse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

DEFAULT_AGENT_URL = "http://127.0.0.1:8001"
DEFAULT_MEMORY_URL = "http://127.0.0.1:8002"


def agent_url() -> str:
    return os.getenv("AGENT_SERVICE_URL", DEFAULT_AGENT_URL)


def memory_url() -> str:
    return os.getenv("MEMORY_SERVICE_URL", DEFAULT_MEMORY_URL)


_ALL = ["GET", "POST", "PUT", "DELETE", "PATCH"]


def create_gateway_app() -> FastAPI:
    from server.errors import register_exception_handlers, register_request_logging
    from server.routers import system

    app = FastAPI(title="Atlas Gateway", version="1.0.0")
    register_exception_handlers(app)
    register_request_logging(app)

    # ---- 本地聚合的健康检查：必须在 system.router 之前注册，先注册先匹配 ----
    @app.get("/api/health")
    async def gateway_health():
        async def probe(name: str, base: str) -> dict:
            try:
                async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                    resp = await client.get(f"{base}/api/health", params={"probe_backends": "false"})
            except httpx.HTTPError as exc:
                return {"ok": False, "status": "down", "error": f"{type(exc).__name__}: {exc}"}
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return {"ok": resp.status_code == 200, "status": "up", "code": resp.status_code,
                    "detail": body.get("memory") or body.get("service") or None}

        agent = await probe("agent", agent_url())
        memory = await probe("memory", memory_url())
        return {
            "ok": bool(agent["ok"] and memory["ok"]),
            "gateway": "up",
            "agent": agent,
            "memory": memory,
        }

    # ---- info / modes 不依赖下游，直接复用 system 路由 ----
    app.include_router(system.router)

    # ---- Agent 执行服务 ----
    @app.api_route("/api/chat/{rest:path}", methods=_ALL)
    async def chat_proxy(request: Request, rest: str):
        if rest in ("stream", "resume"):
            return await forward_sse(request, agent_url(), f"/api/chat/{rest}", upstream="agent")
        return await forward(request, agent_url(), f"/api/chat/{rest}", upstream="agent")

    @app.api_route("/api/credentials", methods=_ALL)
    @app.api_route("/api/credentials/{rest:path}", methods=_ALL)
    async def credentials_proxy(request: Request, rest: str = ""):
        suffix = f"/{rest}" if rest else ""
        return await forward(request, agent_url(), f"/api/credentials{suffix}", upstream="agent")

    # ---- 记忆服务 ----
    @app.api_route("/api/memory", methods=_ALL)
    @app.api_route("/api/memory/{rest:path}", methods=_ALL)
    async def memory_proxy(request: Request, rest: str = ""):
        suffix = f"/{rest}" if rest else ""
        return await forward(request, memory_url(), f"/api/memory{suffix}", upstream="memory")

    # ---- 前端静态资源 ----
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/client", include_in_schema=False)
    def client_console():
        return FileResponse(str(STATIC_DIR / "client.html"))

    return app


app = create_gateway_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas API 网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("server.gateway:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
