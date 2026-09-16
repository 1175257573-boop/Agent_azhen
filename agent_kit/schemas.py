"""数据契约：运行时上下文 + 结构化输出 schema。

LangChain 1.x 的两个关键概念：
  context_schema —— 与「对话状态」区分开的**运行时依赖**（用户身份、租户、配置）。
                    它通过 invoke(input, context=...) 注入，工具内用 ToolRuntime 读取。
  response_format —— 强制模型最终输出符合某个 Pydantic 结构（而非自由文本）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 运行时上下文
@dataclass
class UserContext:
    """跨 UI / 请求透传的运行时上下文（不进大模型 prompt，除非你主动注入）。"""

    user_id: str = "anonymous"
    locale: str = "zh-CN"
    role: str = "user"          # user / admin：可用于工具级权限判断
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 结构化输出
class CitedFact(BaseModel):
    """一条带来源的结论。"""

    fact: str = Field(description="结论本身，一句话说完")
    source: str = Field(description="来源，工具名或 URL")


class ResearchReport(BaseModel):
    """Agent 的最终交付物：结构化调研报告。"""

    title: str = Field(description="报告标题")
    summary: str = Field(description="3 句话以内的摘要")
    key_findings: list[CitedFact] = Field(description="关键结论列表，每条都要带来源")
    open_questions: list[str] = Field(default_factory=list, description="仍未解决的问题")
    confidence: float = Field(ge=0.0, le=1.0, description="结论置信度 0~1")


def report_strategy() -> ToolStrategy:
    """为什么要用 ToolStrategy 而不是直接传 Pydantic 类？

    直接传类时框架自动推断策略（AutoStrategy）；显式使用 ToolStrategy 的意义是：
      1. 任何模型（哪怕不支持原生 structured output）都能通过「再调一次工具」拿到结构化结果
      2. 可以自定义校验失败时的兜底行为（这里是直接抛错，便于演示失败路径）
    """
    return ToolStrategy(
        schema=ResearchReport,
        tool_message_content="已生成结构化调研报告。",
        handle_errors=True,
    )
