"""网关到下游服务的 HTTP 转发。

为什么不用现成的反向代理（nginx / traefik）：网关还承担「下游挂了怎么办」
的职责——Agent 服务不可达时要回 **502 JSON**，而不是连接重置；SSE 流要
逐 chunk 转发、不能缓冲整段再吐给前端。这些规则写在这里，部署侧就少一层
配置，行为也进了测试。

启动方式与地址见 server/gateway.py 与 docker-compose.yml。
"""

from __future__ import annotations

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# connect 设短一点：下游挂了要**快速**失败，别让前端干等
UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)

# 逐跳头：content-length / host 由 httpx 按新请求重算，原样转发会错位
_HOP_HEADERS = {"host", "content-length", "connection", "keep-alive",
                "proxy-authenticate", "proxy-authorization", "transfer-encoding", "upgrade"}


def _headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}


def _url(base_url: str, path: str, query: str = "") -> str:
    url = f"{base_url.rstrip('/')}{path}"
    return f"{url}?{query}" if query else url


def _client() -> httpx.AsyncClient:
    """网关到下游的客户端。

    trust_env=False：本机/容器内网转发绝不能走 HTTP_PROXY——
    否则 127.0.0.1 也会被送去代理，下游一挂就变成代理的错误页而不是 502。
    """
    return httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, trust_env=False)


def upstream_error(name: str, exc: Exception) -> JSONResponse:
    """下游不可达的统一形状：502 + 说清楚是哪个服务、什么错。"""
    return JSONResponse(
        status_code=502,
        content={
            "ok": False,
            "upstream": name,
            "detail": f"下游服务不可达：{name}（{type(exc).__name__}: {exc}）",
        },
    )


async def forward(request: Request, base_url: str, path: str, *, upstream: str = "unknown") -> Response:
    """普通 REST 转发：读完请求体再发给下游，响应整体回传。"""
    body = await request.body()
    try:
        async with _client() as client:
            resp = await client.request(
                request.method,
                _url(base_url, path, request.url.query),
                content=body or None,
                headers=_headers(request),
            )
    except httpx.HTTPError as exc:
        return upstream_error(upstream, exc)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


async def forward_sse(request: Request, base_url: str, path: str, *, upstream: str = "unknown") -> Response:
    """SSE 流式转发：下游出一块就发一块，绝不缓冲整段。

    连不上下游时**在响应开始之前**返回 502——
    历史上最坑的错误形态是「200 + 空响应体」，前端永远转圈。
    """
    body = await request.body()
    client = _client()
    upstream_req = client.build_request(
        request.method, _url(base_url, path, request.url.query),
        content=body or None, headers=_headers(request),
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return upstream_error(upstream, exc)

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type") or "text/event-stream",
    )
