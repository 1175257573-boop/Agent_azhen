"""请求 / 响应 DTO —— 对标 Java 里的 DTO 包。

只放数据形状，不放业务逻辑；FastAPI 会用它做入参校验和 OpenAPI 文档。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 请求
class ChatRequest(BaseModel):
    """发起一次对话。"""

    message: str = Field(..., description="用户输入")
    thread_id: str = Field(default="atlas-main", description="会话 ID，短期记忆的主键")
    mode: str = Field(default="chat", description="能力模式")
    provider: str | None = Field(default=None, description="模型 provider，不填自动探测")
    user_id: str = Field(default="demo", description="用户标识，长期记忆按此隔离")
    role: str = Field(default="admin", description="角色：admin 可写文件")
    stream: bool = Field(default=True, description="是否流式返回")
    enable_mcp: bool = Field(default=False, description="是否把 MCP 工具并入本次对话（会拉起 MCP Server 子进程）")


class ResumeRequest(BaseModel):
    """人工介入（HITL）后恢复执行。"""

    thread_id: str = "atlas-main"
    mode: str = "chat"
    provider: str | None = None
    user_id: str = "demo"
    role: str = "admin"
    enable_mcp: bool = False
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class PreferenceIn(BaseModel):
    """写一条长期偏好。"""

    user_id: str = "demo"
    key: str
    value: str


# ---------------------------------------------------------------- 响应
class MessageOut(BaseModel):
    """一条历史消息（扁平化之后给前端直接渲染）。"""

    role: str
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ThreadBrief(BaseModel):
    thread_id: str
    message_count: int = 0


class MemoryStatus(BaseModel):
    short_term: str
    long_term: str
    window: int
    redis_url: str
    pg_dsn: str
    strict: str
    backends: dict[str, str] = Field(default_factory=dict)


class ApiOk(BaseModel):
    ok: bool = True
    data: Any = None
