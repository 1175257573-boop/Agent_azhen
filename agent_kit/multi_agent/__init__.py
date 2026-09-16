"""多智能体：Subagents / Handoffs / Skills / Router / Custom workflow。

多智能体架构常见的四种形态，什么时候用哪个：

| 架构 | 控制中枢 | 适用场景 | 本项目实现 |
|---|---|---|---|
| Subagents | 主代理把子代理当工具调 | 任务能明确切分，子任务上下文隔离 | subagents.py |
| Handoffs | 状态机，工具返回 Command 改当前步骤 | 固定 SOP 流程（客服向导、审核流） | handoffs.py |
| Skills | 渐进式披露，按需加载能力 | 能力多但大部分轮次用不上（省 token） | skills.py |
| Router | LangGraph 图做分发，可并行 | 多源异构知识库、需要并行召回 | router.py |
| Custom workflow | 手写图 | 以上都不合适时兜底 | workflow.py |

注意一个共同点：**前三种都要给主/子图配 checkpointer**，
否则多轮之间状态丢失，ho 五花八门的 bug 会浪费你半天。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentSpec:
    """描述一个子代理的元信息，方便统一打印与路由。"""

    name: str
    description: str
    model_role: str = "default"   # default | light | heavy，用于动态模型
    tags: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []
