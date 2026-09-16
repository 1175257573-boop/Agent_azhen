"""统一异常处理 + 请求日志 —— 对标 Spring 的 `@ControllerAdvice` + 拦截器。

解决的问题：
  1. 没有这层时，接口里漏掉的异常会变成 FastAPI 默认的纯文本 500，
     前端拿到的是一段 HTML，`JSON.parse` 直接崩。
  2. 各接口自己 try/except 会导致错误格式和状态码口径不一。

设计取舍：
  * **不改** `HTTPException` 的响应形状（仍是 `{"detail": ...}`）——
    前端依赖 detail 字段，改了就是破坏性变更；这里只补日志。
  * **未捕获异常**统一成 JSON，且**不回传堆栈**（防信息泄露），
    完整堆栈只进日志。
  * 已知的业务异常映射为语义化状态码，避免一律 500：
        ValueError / TypeError  → 400
        KeyError / LookupError  → 404
        PermissionError         → 403
        TimeoutError            → 504
"""

from __future__ import annotations

import time
import traceback
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_kit.logging_conf import get_logger

log = get_logger("web.http")

# 业务异常 → HTTP 状态码
STATUS_MAP: dict[type[BaseException], int] = {
    ValueError: 400,
    TypeError: 400,
    KeyError: 404,
    LookupError: 404,
    PermissionError: 403,
    TimeoutError: 504,
    ConnectionError: 502,
}


def _status_for(exc: BaseException) -> int:
    for cls, code in STATUS_MAP.items():
        if isinstance(exc, cls):
            return code
    return 500


def _request_id(request: Request) -> str:
    """优先沿用上游（网关/前端）传来的 X-Request-ID，方便串起全链路日志。"""
    return request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]


def _body(request: Request, exc: BaseException, status: int) -> dict[str, Any]:
    rid = _request_id(request)
    # 5xx 不回传具体异常信息，避免把内部实现细节暴露给调用方
    message = str(exc) if status < 500 else "服务内部错误，请查看服务端日志"
    return {
        "detail": message,
        "error": {
            "type": type(exc).__name__,
            "status": status,
            "request_id": rid,
            "path": request.url.path,
        },
    }


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 保持 {"detail": ...} 原样，只加日志；4xx 属于调用方问题，不必记堆栈
        log.warning("[%s] %s %s -> %s %s", _request_id(request), request.method,
                    request.url.path, exc.status_code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "error": {
                "type": "HTTPException",
                "status": exc.status_code,
                "request_id": _request_id(request),
                "path": request.url.path,
            }},
        )

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        log.warning("[%s] 入参校验失败 %s %s -> %s", _request_id(request),
                    request.method, request.url.path, exc.errors())
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors(), "error": {
                "type": "RequestValidationError",
                "status": 422,
                "request_id": _request_id(request),
                "path": request.url.path,
            }},
        )

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        status = _status_for(exc)
        rid = _request_id(request)
        # 堆栈只进日志，不回给客户端
        log.error(
            "[%s] 未捕获异常 %s %s -> %s: %s\n%s",
            rid, request.method, request.url.path,
            type(exc).__name__, exc, traceback.format_exc(),
        )
        return JSONResponse(status_code=status, content=_body(request, exc, status))


def register_request_logging(app: FastAPI) -> None:
    """记录每个请求的方法、路径、状态码与耗时（Spring 里的 HandlerInterceptor）。"""

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        rid = _request_id(request)
        try:
            response = await call_next(request)
        except Exception:
            cost = (time.perf_counter() - started) * 1000
            log.error("[%s] %s %s 异常 %.1fms", rid, request.method, request.url.path, cost)
            raise
        cost = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = rid
        log.info("[%s] %s %s %s %.1fms", rid, request.method, request.url.path,
                 response.status_code, cost)
        return response
