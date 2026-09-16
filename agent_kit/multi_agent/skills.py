"""Skills：渐进式披露（progressive disclosure）。

解决的问题：一次性把所有「能力说明书」塞进 system prompt，会把上下文撑爆。
做法是三层：
  第 1 层 —— 系统提示里只放技能**名称与一句话描述**（几十个技能也才几百 token）
  第 2 层 —— 模型觉得需要某个技能时，调 load_skill(name) 拿完整说明
  第 3 层 —— 按技能说明再去调真正的工具/写代码

这就是所谓「按需加载」。本项目把它做成通用框架：
技能用 Python 数据结构定义（TypedDict），中间件负责把第 1 层注入系统提示。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command


class Skill(TypedDict):
    """一项技能的定义。

    name        唯一标识
    description 一句话说明（始终可见）
    content     完整说明（只有 load_skill 后才可见）
    """

    name: str
    description: str
    content: str


def skills_summary(skills: list[Skill]) -> str:
    """生成第 1 层：只含名称与描述的目录。"""
    if not skills:
        return "（无可用技能）"
    return "\n".join(f"- {s['name']}: {s['description']}" for s in skills)


# ---------------------------------------------------------------------------
# 基础版：load_skill 直接返回技能的完整正文
# ---------------------------------------------------------------------------
class SkillMiddleware(AgentMiddleware):
    """把技能目录追加到系统提示，并注册唯一的 load_skill 工具。"""

    def __init__(self, skills: list[Skill], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.skills = skills

    @property
    def skills_prompt(self) -> str:
        return skills_summary(self.skills)

    def _with_skills(self, request: ModelRequest) -> ModelRequest:
        addendum = (
            f"\n\n## 可用技能\n{self.skills_prompt}\n\n"
            "需要某项技能的完整说明时，调用 load_skill 工具加载，再按说明行事。"
        )
        new_blocks = list(request.system_message.content_blocks) + [{"type": "text", "text": addendum}]
        return request.override(system_message=SystemMessage(content=new_blocks))

    def wrap_model_call(self, request: ModelRequest, handler):
        return handler(self._with_skills(request))

    async def awrap_model_call(self, request: ModelRequest, handler):
        return await handler(self._with_skills(request))

    # load_skill 通过类属性注册，避免每个实例都创建一次
    @property
    def tools(self):  # type: ignore[override]
        skills = self.skills

        @tool
        def load_skill(skill_name: str) -> str:
            """加载一项技能的完整说明。不知道有哪些技能时先看系统提示里的「可用技能」列表。

            Args:
                skill_name: 技能名称
            """
            for s in skills:
                if s["name"] == skill_name:
                    return f"已加载技能 {skill_name}：\n\n{s['content']}"
            available = ", ".join(s["name"] for s in skills)
            return f"未找到技能 {skill_name}。可用：{available or '无'}"

        return [load_skill]


# ---------------------------------------------------------------------------
# 进阶版：把「已加载的技能」记进 state，用于硬约束
# ---------------------------------------------------------------------------
def make_skills_tools(skills: list[Skill]):
    """返回 (load_skill, get_loaded_skills)，load_skill 会往 state 里写记录。

    配合受限工具使用：某个高危工具可以要求「必须已加载 xxx 技能」才放行。
    """
    by_name = {s["name"]: s for s in skills}

    @tool
    def load_skill(skill_name: str, runtime: ToolRuntime) -> Command:
        """加载一项技能的完整说明，并记录到会话状态中。

        Args:
            skill_name: 技能名称
        """
        skill = by_name.get(skill_name)
        if skill is None:
            available = ", ".join(by_name)
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            content=f"未找到技能 {skill_name}。可用：{available or '无'}",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ]
                }
            )

        loaded = list(runtime.state.get("skills_loaded", []) or [])
        if skill_name not in loaded:
            loaded.append(skill_name)

        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"已加载技能 {skill_name}：\n\n{skill['content']}",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
                "skills_loaded": loaded,
            }
        )

    @tool
    def loaded_skills(runtime: ToolRuntime) -> str:
        """查询本次会话已经加载过哪些技能。"""
        loaded = runtime.state.get("skills_loaded", []) or []
        return ", ".join(loaded) if loaded else "尚未加载任何技能。"

    return [load_skill, loaded_skills]
