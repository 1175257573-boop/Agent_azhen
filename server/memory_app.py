"""记忆服务（独立进程）。

    python -m server.memory_app --port 8002

职责：长期偏好读写、记忆后端状态、会话清单。
Agent 执行服务不依赖它——会话存储是共享后端，各进程直连；
这个服务把「记忆域」的 HTTP API 收敛到一处，方便独立部署与伸缩。
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动时预热记忆后端，让「连不上 Redis/PG」在第一屏就暴露。"""
    from agent_kit import memory as mem
    from agent_kit.logging_conf import get_logger

    log = get_logger("memory.service")
    log.info("记忆层：短期=%s 长期=%s", mem.short_backend(), mem.long_backend())
    try:
        mem.build_checkpointer()
        mem.build_store()
    except Exception as exc:  # noqa: BLE001
        log.warning("记忆后端预热失败（将降级为内存）：%s", exc)
    yield


def create_memory_app() -> FastAPI:
    from server.errors import register_exception_handlers, register_request_logging
    from server.routers import memory

    app = FastAPI(title="Atlas Memory Service", version="1.0.0", lifespan=_lifespan)
    register_exception_handlers(app)
    register_request_logging(app)

    app.include_router(memory.router)

    @app.get("/api/health")
    def health(probe_backends: bool = True):
        """给网关聚合用：除了自报家门，也顺带探一下记忆后端。"""
        from agent_kit import memory as mem

        payload = {
            "ok": True,
            "service": "memory",
            "short_term": mem.RESOLVED.get("short") or mem.short_backend(),
            "long_term": mem.RESOLVED.get("long") or mem.long_backend(),
        }
        if probe_backends:
            try:
                probed = mem.probe()
            except Exception as exc:  # noqa: BLE001 —— 探针本身不能把健康检查拖垮
                payload["backends"] = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                payload["backends"] = probed
                payload["ok"] = all(v.get("alive") == "True" for v in probed.values())
        return payload

    return app


app = create_memory_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas 记忆服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("server.memory_app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
