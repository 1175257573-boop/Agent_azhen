"""自定义状态（对标 Java 里的「扩展 RequestContext / ThreadLocal」）。

LangChain 1.x 有两个概念容易混淆：

  state           —— **对话级**的可变数据，每轮都会被 checkpointer 持久化。
                     典型用法：当前步骤、已加载技能、任务进度。
  context (runtime) —— **请求级**的只读依赖（用户身份、租户、配置），
                     不进对话历史，通常也不落盘。典型用法：user_id、locale、权限。

自定义 state 必须是 **TypedDict 且继承 AgentState**（1.0 起不再接受 Pydantic / dataclass）。
两种挂法：
  1) 中间件类属性 `state_schema`（推荐，作用域清晰，谁用谁声明）
  2) create_agent(state_schema=...)  （快速，但全局生效，新项目不推荐）
"""

from __future__ import annotations

from langchain.agents import AgentState


class TaskState(AgentState):
    """演示用的扩展状态。

    注意必须继承 AgentState，否则图会缺 messages 等内建键。
    """

    # 工作流当前处于哪一步（Handoffs 与自定义 workflow 用）
    current_step: str
    # 已被按需加载的技能名（Skills 渐进式披露用）
    skills_loaded: list[str]
    # 是否已完成身份认证（动态工具可见性用）
    authenticated: bool
    # 用户要求「深度思考」时置 True，动态模型据此切到强模型
    deep_think: bool


class SupportState(AgentState):
    """Handoffs 场景使用的状态：收集保修信息 → 问题分类。"""

    current_step: str
    warranty_status: str


def default_state() -> dict:
    """初始状态值，避免 state.get(...) 到处写默认值。"""
    return {
        "current_step": "collect",
        "skills_loaded": [],
        "authenticated": False,
        "deep_think": False,
    }
