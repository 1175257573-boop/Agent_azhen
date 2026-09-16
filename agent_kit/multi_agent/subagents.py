"""Subagents：把子代理包装成主代理的工具。

为什么不用一个大模型干到底：
  1. **上下文隔离** —— 子任务的中间过程（检索出的 20 条候选、写废的草稿）不污染主对话
  2. **专项提示词** —— 每个子代理有独立 system_prompt，比一份万能提示词好调
  3. **并行** —— 子代理之间无依赖时可以并发（见 router.py 的 Send 方案）

两种工具模式：
  Tool per agent   —— 每个子代理一个 @tool，参数可控，推荐
  Single dispatch  —— 一个 task(agent_name, description)，加子代理时不用动工具签名
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


# ---------------------------------------------------------------------------
# 通用：把任意 CompiledGraph 包成一个同步工具
# ---------------------------------------------------------------------------
def wrap_agent_as_tool(
    subagent: Any,
    name: str,
    description: str,
    *,
    pass_full_context: bool = False,
) -> BaseTool:
    """把一个子代理包装成主代理可以调用的 @tool。

    Args:
        subagent: create_agent 返回的对象
        pass_full_context: True 时会把主对话里第一条用户消息一并交给子代理。
                           子任务需要背景时才开，默认关闭以换取上下文隔离。
    """

    @tool(name, description=description)
    def call_subagent(request: str, runtime: ToolRuntime) -> str:
        """执行一个子任务。

        Args:
            request: 交给子代理的任务描述，越具体越好
        """
        prompt = request
        if pass_full_context:
            msgs = runtime.state.get("messages", []) if isinstance(runtime.state, dict) else []
            humans = [m for m in msgs if getattr(m, "type", None) == "human"]
            if humans:
                original = humans[0].content
                prompt = f"用户的原始问题：\n{original}\n\n本次子任务：\n{request}"

        result = subagent.invoke({"messages": [{"role": "user", "content": prompt}]})
        last = result["messages"][-1]
        content = getattr(last, "content", "")
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        return content or "(子代理未返回文本)"

    return call_subagent


def build_tool_per_agent(
    model: BaseChatModel,
    specs: dict[str, tuple[str, str, Sequence[BaseTool]]],
    **agent_kwargs: Any,
) -> list[BaseTool]:
    """模式一：一代理一工具。

    Args:
        specs: {名字: (工具名, 工具描述, 该子代理可用的工具列表)}
    """
    tools: list[BaseTool] = []
    for key, (tool_name, tool_desc, sub_tools) in specs.items():
        sub = create_agent(
            model=model,
            tools=list(sub_tools),
            system_prompt=f"你是负责「{tool_name}」的专项代理。只回答与本职责相关的内容，输出简洁。",
            name=f"{key}_agent",
            **agent_kwargs,
        )
        tools.append(wrap_agent_as_tool(sub, tool_name, tool_desc))
    return tools


def build_single_dispatch(
    model: BaseChatModel,
    registry: dict[str, tuple[str, Sequence[BaseTool]]],
    tool_name: str = "task",
    **agent_kwargs: Any,
) -> Callable:
    """模式二：单一派发工具，用一个 task(agent_name, description) 调所有子代理。

    优点：新增子代理只需改注册表，主工具的签名与提示词都不用动。
    缺点：参数约束弱，模型可能填错 agent_name（用 Enum 可以缓解）。
    """
    agents = {
        key: create_agent(
            model=model,
            tools=list(sub_tools),
            system_prompt=system_prompt,
            name=f"{key}_agent",
            **agent_kwargs,
        )
        for key, (system_prompt, sub_tools) in registry.items()
    }
    available = ", ".join(agents) or "无"

    @tool(
        tool_name,
        description=f"把任务派发给指定的专项代理，由它完成后返回结果。可用代理：{available}",
    )
    def task(agent_name: str, description: str) -> str:
        """派发子任务给专项代理并返回其结论。

        Args:
            agent_name: 代理名称，必须是可用代理之一
            description: 交给该代理的任务描述，越具体越好
        """
        agent = agents.get(agent_name)
        if agent is None:
            return f"未找到代理 {agent_name}。可用：{available}"
        result = agent.invoke({"messages": [{"role": "user", "content": description}]})
        last = result["messages"][-1]
        content = getattr(last, "content", "")
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return content or "(子代理未返回文本)"

    return task
