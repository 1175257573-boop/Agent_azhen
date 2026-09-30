"""服务拆分验证 —— 阶段三。

拆出三个进程之后要回答三个问题：

1. 跨进程请求：网关 → Agent 执行服务 / 记忆服务，请求真的过了 HTTP 边界
2. 流式转发：SSE 事件逐 chunk 穿过网关，不被缓冲
3. 服务故障处理：下游挂了，网关回 502 JSON，绝不「200 + 空响应体」

测试里 Agent 执行服务与记忆服务是**真实子进程**（不是 in-process ASGI），
保证验证的是跨进程语义；网关自身用 ASGI 直挂（它只是转发，没有自己的业务状态）。

零 API Key、零外部服务（SQLite 记忆后端），离线可复现。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# 子进程服务
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _service_env(home: Path) -> dict:
    env = os.environ.copy()
    env["ATLAS_HOME"] = str(home)
    env["MEMORY_STRICT"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"   # Windows 默认 GBK，中文日志会让子进程写挂
    for name in ("REDIS_URL", "PG_DSN", "POSTGRES_URL", "DATABASE_URL",
                 "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
                 "DASHSCOPE_API_KEY", "LLM_PROVIDER"):
        env.pop(name, None)
    return env


def _wait_up(proc: subprocess.Popen, port: int, log_file: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"服务进程提前退出（exit={proc.returncode}），日志：\n"
                + log_file.read_text(encoding="utf-8", errors="replace")[-3000:]
            )
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/api/health",
                             params={"probe_backends": "false"}, timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"服务 {port} 启动超时，日志：\n"
                       + log_file.read_text(encoding="utf-8", errors="replace")[-3000:])


@pytest.fixture(scope="module")
def services():
    """并行拉起 Agent 执行服务与记忆服务两个真实子进程。"""
    work = Path(tempfile.mkdtemp(prefix="split-svc-"))
    home = work / "atlas-home"
    env = _service_env(home)

    agent_port = _free_port()
    memory_port = _free_port()

    def spawn(module: str, port: int) -> tuple[subprocess.Popen, Path]:
        log_file = work / f"{module}.log"
        fh = log_file.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", str(port)],
            env=env, cwd=str(PROJECT_ROOT), stdout=fh, stderr=subprocess.STDOUT,
        )
        proc._log_fh = fh  # type: ignore[attr-defined]  # 留给 teardown 关
        return proc, log_file

    agent_proc, agent_log = spawn("server.agent_app", agent_port)
    memory_proc, memory_log = spawn("server.memory_app", memory_port)
    try:
        # 两个进程并行预热（langchain 导入要几秒，串行等会翻倍）
        _wait_up(agent_proc, agent_port, agent_log)
        _wait_up(memory_proc, memory_port, memory_log)
        yield {"agent": agent_port, "memory": memory_port, "home": home}
    finally:
        for proc in (agent_proc, memory_proc):
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc._log_fh.close()  # type: ignore[attr-defined]


def _gateway(monkeypatch, agent: int | None, memory: int | None):
    """组装一个指向指定下游的网关。传 None 表示该下游「没起」（故障注入）。"""
    if agent is not None:
        monkeypatch.setenv("AGENT_SERVICE_URL", f"http://127.0.0.1:{agent}")
    else:
        monkeypatch.setenv("AGENT_SERVICE_URL", f"http://127.0.0.1:{_free_port()}")
    if memory is not None:
        monkeypatch.setenv("MEMORY_SERVICE_URL", f"http://127.0.0.1:{memory}")
    else:
        monkeypatch.setenv("MEMORY_SERVICE_URL", f"http://127.0.0.1:{_free_port()}")

    from server.gateway import create_gateway_app

    return create_gateway_app()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


# ---------------------------------------------------------------------------
# 1. 跨进程请求
# ---------------------------------------------------------------------------
def test_memory_roundtrip_crosses_process_boundary(services, monkeypatch):
    """写偏好 → 读偏好，两次请求穿过网关到达记忆服务子进程，落进共享存储。"""
    app = _gateway(monkeypatch, services["agent"], services["memory"])

    async def scenario():
        async with _client(app) as client:
            put = await client.post("/api/memory/preferences",
                                    json={"user_id": "split-user", "key": "style", "value": "结构化"})
            assert put.status_code == 200, put.text

            got = await client.get("/api/memory/preferences", params={"user_id": "split-user"})
            assert got.status_code == 200, got.text
            return got.json()

    items = asyncio.run(scenario())
    assert any(i["key"] == "style" and i["value"] == "结构化" for i in items), f"偏好没写进去：{items}"

    # 再绕开网关直接问记忆服务子进程——证明网关不是本地偷跑，是真的跨进程
    direct = httpx.get(f"http://127.0.0.1:{services['memory']}/api/memory/preferences",
                       params={"user_id": "split-user"}, timeout=5.0)
    assert any(i["key"] == "style" for i in direct.json())


# ---------------------------------------------------------------------------
# 2. 流式转发
# ---------------------------------------------------------------------------
def test_sse_events_stream_through_the_gateway(services, monkeypatch):
    """SSE 逐 chunk 转发：能收到多个 data 帧，且 token / done 都齐。"""
    app = _gateway(monkeypatch, services["agent"], services["memory"])
    payload = {"message": "你好", "thread_id": "split-sse", "mode": "chat",
               "provider": "fake", "user_id": "split", "role": "admin", "enable_mcp": False}

    async def scenario():
        frames: list[dict] = []
        async with _client(app) as client:
            async with client.stream("POST", "/api/chat/stream", json=payload, timeout=60.0) as resp:
                assert resp.status_code == 200, await resp.aread()
                assert "text/event-stream" in resp.headers.get("content-type", "")
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        frames.append(json.loads(line[len("data:"):].strip()))
        return frames

    frames = asyncio.run(scenario())
    types = [f["type"] for f in frames]
    assert len(frames) > 1, f"SSE 疑似被缓冲成了一块：只有 {len(frames)} 帧"
    assert "token" in types and types[-1] == "done", f"事件流不完整：{types}"

    # 顺路验证：Web 会话的流水也落了（阶段一的修复，跨进程后仍然成立）
    from agent_kit import rollout

    sessions = {s["thread_id"] for s in rollout.list_sessions(
        sources=("chat",), db_path=services["home"] / "atlas.db")}
    assert "split-sse" in sessions


def test_queue_endpoints_forward_through_the_gateway(services, monkeypatch):
    """排队 REST（增 / 查 / 删）也穿过网关打到 Agent 执行服务。"""
    app = _gateway(monkeypatch, services["agent"], services["memory"])

    async def scenario():
        async with _client(app) as client:
            add = await client.post("/api/chat/queue",
                                    json={"thread_id": "split-queue", "message": "稍后再说"})
            assert add.status_code == 200, add.text
            item_id = add.json()["id"]

            listed = await client.get("/api/chat/queue", params={"thread_id": "split-queue"})
            assert any(i["text"] == "稍后再说" for i in listed.json()), listed.text

            removed = await client.delete(f"/api/chat/queue/{item_id}",
                                          params={"thread_id": "split-queue"})
            assert removed.status_code == 200 and removed.json()["ok"] is True

            cleared = await client.get("/api/chat/queue", params={"thread_id": "split-queue"})
            return cleared.json()

    remaining = asyncio.run(scenario())
    assert remaining == [], f"撤回之后队列里还有残留：{remaining}"


# ---------------------------------------------------------------------------
# 3. 服务故障处理
# ---------------------------------------------------------------------------
def test_agent_down_returns_502_not_empty_200(services, monkeypatch):
    """Agent 执行服务挂了：REST 与 SSE 都必须 502，不能 200 + 空响应体。"""
    app = _gateway(monkeypatch, None, services["memory"])

    async def scenario():
        async with _client(app) as client:
            sse = await client.post("/api/chat/stream", json={
                "message": "你好", "thread_id": "x", "mode": "chat",
                "provider": "fake", "user_id": "s", "role": "admin", "enable_mcp": False,
            }, timeout=15.0)
            rest = await client.get("/api/chat/history", params={"thread_id": "x"}, timeout=15.0)
            mem_ok = await client.get("/api/memory/status", timeout=15.0)
            health = await client.get("/api/health", timeout=15.0)
            return sse, rest, mem_ok, health

    sse, rest, mem_ok, health = asyncio.run(scenario())

    assert sse.status_code == 502, f"SSE 下游挂了应该 502，实际 {sse.status_code}"
    assert "agent" in sse.json().get("detail", "")
    assert rest.status_code == 502
    assert mem_ok.status_code == 200, "Agent 挂了不该影响记忆服务"

    body = health.json()
    assert body["ok"] is False and body["agent"]["ok"] is False, f"健康聚合没报 Agent 故障：{body}"
    assert body["memory"]["ok"] is True


def test_memory_down_isolated_from_agent(services, monkeypatch):
    """记忆服务挂了：记忆接口 502，对话照常——故障被隔离在各自的域里。"""
    app = _gateway(monkeypatch, services["agent"], None)

    async def scenario():
        async with _client(app) as client:
            pref = await client.get("/api/memory/preferences", params={"user_id": "x"}, timeout=15.0)
            health = await client.get("/api/health", timeout=15.0)
            chat = await client.post("/api/chat/stream", json={
                "message": "你好", "thread_id": "mem-down", "mode": "chat",
                "provider": "fake", "user_id": "s", "role": "admin", "enable_mcp": False,
            }, timeout=60.0)
            return pref, health, chat

    pref, health, chat = asyncio.run(scenario())

    assert pref.status_code == 502
    body = health.json()
    assert body["ok"] is False and body["memory"]["ok"] is False
    assert body["agent"]["ok"] is True, f"记忆服务故障不该拖垮 Agent：{body}"
    assert chat.status_code == 200, "记忆服务挂了对话也要能跑（存储直连共享后端）"
