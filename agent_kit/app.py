"""应用装配层（对标 Java 的 @Configuration + 各种 @Bean 方法）。

这里按「运行模式」产出不同的 Agent，对外只暴露 build_app() 一个工厂函数。
每种模式对应 LangChain 1.x 的一块能力：

    chat        基础 ReAct + 工具 + 中间件 + 记忆
    structured  结构化输出（Pydantic）
    dynamic     动态模型 + 动态工具 + 自定义 state
    mcp         MCP 工具接入（异步构建）
    skills      Skills 渐进式披露
    handoffs    流程交接状态机
    subagents   主代理调度多个子代理
    router      多源路由工作流（LangGraph）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel

from agent_kit import memory as mem
from agent_kit.agent import build_agent
from agent_kit.config import AgentSettings, require_api_key
from agent_kit.dynamic_tools import DynamicToolMiddleware, make_state_based_tools
from agent_kit.logging_conf import get_logger
from agent_kit.models import make_dynamic_model
from agent_kit.policy import resolve
from agent_kit.state import TaskState
from agent_kit.tools import ALL_TOOLS, SAFE_TOOLS

log = get_logger("agent.app")

MODES = ("chat", "structured", "dynamic", "mcp", "skills", "handoffs", "subagents", "router")

MODE_HELP = {
    "chat": "基础对话 + 工具 + 记忆（默认）",
    "structured": "强制结构化输出 ResearchReport",
    "dynamic": "动态模型 + 动态工具 + 自定义 state",
    "mcp": "接入 MCP 工具（含进度/日志/引导输入）",
    "skills": "Skills 渐进式披露",
    "handoffs": "流程交接状态机（客服向导）",
    "subagents": "主代理调度多个专项子代理",
    "router": "多源知识库路由（可并行）",
}


@dataclass
class AppConfig:
    """一次运行的所有可调项。"""

    provider: str | None = None
    model_name: str = ""
    mode: str = "chat"
    # None = 未指定，交给 policy.py 按 atlas.toml / 环境变量 / 默认值决定
    enable_hitl: bool | None = None
    approval: str | None = None       # untrusted | on-failure | never
    sandbox: str | None = None        # read-only | workspace-write | danger-full-access
    enable_summarization: bool = True
    readonly: bool = False
    thread_id: str = "atlas-main"
    user_id: str = "demo"
    role: str = "admin"
    extra_tools: list = field(default_factory=list)
    message_window: int | None = None   # None = 取环境变量 MEMORY_WINDOW（默认 20 条）
    enable_mcp: bool = False            # 是否把 MCP 工具并入当前模式（含 chat）

    def settings(self) -> AgentSettings:
        return AgentSettings(provider=self.provider, model_name=self.model_name)

    @property
    def window(self) -> int:
        """短期记忆的消息窗口大小；与摘要压缩互斥，窗口优先。"""
        return self.message_window or mem.window_size()

    @property
    def needs_mcp(self) -> bool:
        """是否需要异步装配：MCP 工具只实现了 ainvoke，同步链路会炸。"""
        return self.mode == "mcp" or self.enable_mcp


@dataclass
class BuiltApp:
    """构建产物：图 + 运行所需的一切。"""

    graph: Any
    config: AppConfig
    settings: AgentSettings
    checkpointer: Any
    store: Any
    is_workflow: bool = False   # router 模式返回的是 LangGraph 而非 create_agent 产物
    is_mcp: bool = False        # 工具集里含 MCP 工具 → 必须走 astream
    mcp_hub: Any = None         # 持有 MCP 连接（stdio 子进程），防止被 GC 回收
    mcp_tool_names: list = field(default_factory=list)
    policy: Any = None         # 审批 / 沙箱策略，main.py info 会打印

    @property
    def thread_config(self) -> dict:
        return mem.thread_config(self.config.thread_id)

    @property
    def context(self) -> dict:
        return {"user_id": self.config.user_id, "role": self.config.role, "locale": "zh-CN"}


def require_real_model(settings: AgentSettings, explicit: bool) -> None:
    """真实模型门槛：**没有 Key 就明确失败**，不再静默降级到假模型。"""
    if settings.provider == "fake":
        if explicit:
            return  # 用户显式选了 fake
        raise RuntimeError(
            "尚未配置任何模型的 API Key，无法真实运行。\n"
            "  请设置系统环境变量（新开终端后生效）：\n"
            "    setx DASHSCOPE_API_KEY \"你的密钥\"     # 阿里云百炼\n"
            "    setx LLM_PROVIDER dashscope\n"
            "  或：setx OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY\n"
            "  自查：python main.py check --ping\n"
            "  仍想离线演示：python main.py chat --provider fake"
        )
    require_api_key(settings)


def build_app(cfg: AppConfig) -> BuiltApp:
    """按模式构建应用（同步）。MCP 模式请用 build_app_async()。"""
    settings = cfg.settings()
    require_real_model(settings, explicit=bool(cfg.provider))

    from agent_kit.config import build_chat_model

    model: BaseChatModel = build_chat_model(settings)

    checkpointer = mem.build_checkpointer()
    store = mem.build_store()
    mem.seed_long_term_memory(store, user_id=cfg.user_id)

    # 审批 / 沙箱策略：一处解析，全程复用（旧的 --role / --no-hitl 仍作为最高优先级覆盖）
    policy = resolve(
        cli_approval=cfg.approval,
        cli_sandbox=cfg.sandbox,
        cli_hitl=cfg.enable_hitl,
        role=cfg.role,
    )
    for warning in policy.warnings:
        log.warning("[policy] %s", warning)

    if cfg.mode == "handoffs":
        from agent_kit.multi_agent.handoffs import build_handoff_agent

        graph = build_handoff_agent(model, checkpointer=checkpointer)
        return BuiltApp(graph, cfg, settings, checkpointer, store, policy=policy)

    if cfg.mode == "router":
        from agent_kit.multi_agent.router import build_router_workflow

        sources = {
            "docs": ("你是文档专家，负责从文档库里找依据。", []),
            "code": ("你是代码专家，负责解释代码实现。", []),
            "chat": ("你是沟通记录专家，负责从讨论记录里找结论。", []),
        }
        graph = build_router_workflow(model, sources)
        return BuiltApp(graph, cfg, settings, checkpointer, store, is_workflow=True, policy=policy)

    if cfg.mode == "skills":
        from agent_kit.builtin_skills import PROJECT_ENGINEERING
        from agent_kit.multi_agent.skills import Skill, SkillMiddleware

        skills = [
            PROJECT_ENGINEERING,
            Skill(
                name="knowledge_search",
                description="从本地知识库检索事实依据",
                content="1. 先用 search_notes 检索\n2. 每条结论都要附来源\n3. 检索不到就明说",
            ),
            Skill(
                name="data_calc",
                description="做数值计算与单位换算",
                content="1. 用 calculator 计算\n2. 保留 3 位有效数字\n3. 标注计算式",
            ),
            Skill(
                name="report_writing",
                description="产出结构化研究报告",
                content="1. 先列关键结论\n2. 每条附来源\n3. 结尾写置信度与待确认项",
            ),
        ]
        graph = build_agent(
            settings,
            model=model,
            tools=list(SAFE_TOOLS) + cfg.extra_tools,
            include_write_tools=policy.write_tools,
            middleware_extra=[SkillMiddleware(skills=skills)],
            checkpointer=checkpointer,
            store=store,
            state_schema=TaskState,
            message_window=cfg.window,
        ).graph
        return BuiltApp(graph, cfg, settings, checkpointer, store, policy=policy)

    if cfg.mode == "subagents":
        from agent_kit.multi_agent.subagents import build_tool_per_agent

        specs = {
            "research": ("research", "研究一个主题并给出带来源的结论。", SAFE_TOOLS),
            "calc": ("calculate_task", "做数值计算任务并给出计算式。", [ALL_TOOLS[0]]),
        }
        sub_tools = build_tool_per_agent(model, specs)
        graph = build_agent(
            settings,
            model=model,
            tools=sub_tools,
            checkpointer=checkpointer,
            store=store,
            message_window=cfg.window,
        ).graph
        return BuiltApp(graph, cfg, settings, checkpointer, store, policy=policy)

    if cfg.mode == "structured":
        graph = build_agent(
            settings,
            model=model,
            tools=list(SAFE_TOOLS),
            structured_output=True,
            checkpointer=checkpointer,
            store=store,
            message_window=cfg.window,
        ).graph
        return BuiltApp(graph, cfg, settings, checkpointer, store, policy=policy)

    if cfg.mode == "dynamic":
        # 同一 provider 下的「轻/重」两个模型：轻模型省钱，重模型兜底
        heavy = _heavy_model(settings)
        middleware_extra = [
            make_dynamic_model(light_model=model, heavy_model=heavy, message_threshold=6),
            make_state_based_tools(),
            DynamicToolMiddleware(tools=[]),
        ]
        graph = build_agent(
            settings,
            model=model,
            tools=list(ALL_TOOLS) + cfg.extra_tools,
            middleware_extra=middleware_extra,
            checkpointer=checkpointer,
            store=store,
            state_schema=TaskState,
            message_window=cfg.window,
        ).graph
        return BuiltApp(graph, cfg, settings, checkpointer, store, policy=policy)

    # ---------- 默认：chat ----------
    graph = build_agent(
        settings,
        model=model,
        tools=list(ALL_TOOLS) + cfg.extra_tools,
        include_write_tools=policy.write_tools,
        enable_hitl=policy.hitl,
        enable_summarization=cfg.enable_summarization,
        readonly=policy.readonly or cfg.readonly,
        escalate_on_failure=policy.escalate_on_failure,
        checkpointer=checkpointer,
        store=store,
        message_window=cfg.window,
    ).graph
    return BuiltApp(graph, cfg, settings, checkpointer, store, policy=policy)


async def build_app_async(cfg: AppConfig) -> BuiltApp:
    """异步装配。

    两种场景会走到这里：
      1. `mode == "mcp"`：工具集以 MCP 工具为主；
      2. `enable_mcp=True`：**在 chat（或其它模式）上叠加 MCP 能力**，
         本地工具与 MCP 工具一起交给模型选择。

    之所以必须是 async：MCP 工具只实现了 `ainvoke`，
    同步调用会抛 `StructuredTool does not support sync invocation`。
    """
    # 不需要 MCP 时，直接退回同步装配（handoffs / router 等模式依赖它）
    if not cfg.needs_mcp:
        return build_app(cfg)

    settings = cfg.settings()
    require_real_model(settings, explicit=bool(cfg.provider))

    from agent_kit.config import build_chat_model

    model: BaseChatModel = build_chat_model(settings)

    checkpointer = mem.build_checkpointer()
    store = mem.build_store()
    mem.seed_long_term_memory(store, user_id=cfg.user_id)

    policy = resolve(
        cli_approval=cfg.approval,
        cli_sandbox=cfg.sandbox,
        cli_hitl=cfg.enable_hitl,
        role=cfg.role,
    )
    for warning in policy.warnings:
        log.warning("[policy] %s", warning)

    from agent_kit.mcp_client import MCPHub, make_arg_injector, mcp_audit

    hub = MCPHub()
    mcp_tools = list(await hub.connect_default())
    middleware_extra = [
        mcp_audit,
        make_arg_injector({"user_id": cfg.user_id, "source": "atlas"}),
    ]

    if cfg.mode == "mcp":
        tools = list(SAFE_TOOLS) + mcp_tools
        include_write = False          # mcp 模式只演示只读工具，写工具不并入
    else:
        tools = list(ALL_TOOLS) + list(cfg.extra_tools) + mcp_tools
        include_write = policy.write_tools

    built = build_agent(
        settings,
        model=model,
        tools=tools,
        include_write_tools=include_write,
        enable_hitl=policy.hitl,
        enable_summarization=cfg.enable_summarization,
        readonly=policy.readonly or cfg.readonly,
        escalate_on_failure=policy.escalate_on_failure,
        middleware_extra=middleware_extra,
        checkpointer=checkpointer,
        store=store,
        message_window=cfg.window,
    )
    return BuiltApp(
        built.graph, cfg, settings, checkpointer, store,
        is_mcp=True, mcp_hub=hub, mcp_tool_names=[t.name for t in mcp_tools], policy=policy,
    )


async def connect_mcp_tool_names(user_id: str = "demo") -> list[str]:
    """只连接一次 MCP 并把工具名列出来（给前端开关展示用）。"""
    from agent_kit.mcp_client import MCPHub

    hub = _MCP_HUBS.get(user_id)
    if hub is None:
        hub = MCPHub()
        _MCP_HUBS[user_id] = hub
        await hub.connect_default()
    tools = getattr(hub, "tools", None) or []
    return [t.name for t in tools]


_MCP_HUBS: dict[str, Any] = {}


def _heavy_model(settings: AgentSettings) -> Any:
    """动态模型用的「强模型」：同一 provider 下的高配型号。"""
    from copy import copy

    heavy_map = {
        "dashscope": "qwen-max",
        "openai": "gpt-4o",
        "deepseek": "deepseek-chat",
        "anthropic": "claude-sonnet-4-5",
    }
    s = copy(settings)
    s.model_name = heavy_map.get(settings.provider, settings.model_name)
    from agent_kit.config import build_chat_model

    return build_chat_model(s)
