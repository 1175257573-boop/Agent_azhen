"""请求 / 响应 DTO —— 对标 Java 里的 DTO 包。

只放数据形状，不放业务逻辑；FastAPI 会用它做入参校验和 OpenAPI 文档。
"""

from __future__ import annotations

from typing import Any, Literal

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


class QueueIn(BaseModel):
    """把一条用户输入排进队列（Agent 忙碌时用）。"""

    thread_id: str = "atlas-main"
    message: str = Field(..., description="要排队发送的内容")
    item_id: str | None = Field(default=None, description="编辑已有排队项时传它的 id")


class QueuedOut(BaseModel):
    """一条排队中的消息。"""

    id: str
    thread_id: str
    text: str
    seq: int = 0
    created_at: float = 0.0
    preview: str = ""


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


# ---------------------------------------------------------------- 凭据（API Key）
# 这一组 DTO 有个额外约束：**任何字段都不允许把完整密钥带出去**。
# 唯一的例外是 RevealOut.api_key，它只在"用户二次确认后主动查看"这一个动作里返回。
class CredentialIn(BaseModel):
    """保存一个 provider 的密钥。

    `repr=False` 不是装饰性的：pydantic 打印模型、FastAPI 校验失败回显、
    调试时随手 `print(request)` 走的都是 repr——一个没关掉 repr 的密钥字段，
    就是把密钥写进日志最快的捷径。
    """

    model_config = {"extra": "forbid"}

    provider: str = Field(..., min_length=2, max_length=32, description="openai / deepseek / dashscope / anthropic")
    api_key: str = Field(..., min_length=8, max_length=512, repr=False, description="密钥明文，仅本机传输")
    remember: bool = Field(
        default=False,
        description="是否记住到本机（写入 ATLAS_HOME/credentials.json）。默认 false = 只存在于本次进程内存",
    )


class CredentialOut(BaseModel):
    """单个 provider 的凭据状态。**只有掩码，没有明文。**"""

    provider: str
    label: str
    env_name: str
    configured: bool
    masked: str = Field(default="", description="形如 sk-1***cdef，仅用于展示")
    length: int = Field(default=0, description="密钥长度，用于确认是否复制完整")
    origin: Literal["env", "runtime", "none"] = Field(
        default="none", description="env=启动前就有的环境变量；runtime=本次运行由界面注入；none=未配置"
    )
    persistent: bool = Field(default=False, description="是否已写入本机凭据文件")
    shadowed: bool = Field(default=False, description="界面输入是否覆盖了原本存在的环境变量")


class CredentialState(BaseModel):
    """密钥面板的完整快照。"""

    items: list[CredentialOut] = Field(default_factory=list)
    active_provider: str = Field(default="fake", description="当前自动选中的 provider")
    storage_path: str = ""
    file_exists: bool = False
    storage_note: str = ""
    notes: list[str] = Field(default_factory=list, description="保密说明，文案由服务端统一维护")


class RevealIn(BaseModel):
    """显式查看某个 provider 的完整密钥。

    刻意用 POST + 请求体而不是 GET + 路径参数：密钥不能出现在 URL 里，
    否则它会同时进入浏览器历史、访问日志和任何中间代理的记录。
    """

    model_config = {"extra": "forbid"}

    provider: str = Field(..., min_length=2, max_length=32)


class RevealOut(BaseModel):
    """完整密钥的一次性返回。前端必须限时展示、不得写入任何浏览器存储。"""

    provider: str
    api_key: str = Field(..., repr=False)
    masked: str = ""
    expires_in: int = Field(default=15, description="建议前端自动隐藏的秒数")


class VerifyIn(BaseModel):
    """对已配置的密钥做一次真实连通性探测。"""

    model_config = {"extra": "forbid"}

    provider: str = Field(..., min_length=2, max_length=32)
    model_name: str = Field(default="", description="留空则用该 provider 的默认型号")


class VerifyOut(BaseModel):
    """连通性探测结果。"""

    ok: bool
    provider: str
    model: str = ""
    message: str = ""
    latency_ms: int = 0
    sample: str = Field(default="", description="模型回显片段，用于确认链路真的通了")
