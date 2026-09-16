"""Router：用 LangGraph 图做分发，可串行也可并行。

和 Subagents 的本质区别：
  Subagents 的分派是**模型决定**的（模型选工具 = 选子代理），不确定性高
  Router  的分派是**代码决定**的（分类节点 → 路由边），可预测、可并行、可回放

案例（多源知识库路由）：
  用户提问 → classify 节点判断该查哪些知识源 → 并行 Send 到多个专家代理 → synthesize 汇总

两种路由：
  单一路由  —— Command(goto="github")，只给一个源
  并行多路由 —— return [Send(...), Send(...)]，并发派发，结果用 Annotated[list, add] 合并
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class Classification(TypedDict):
    source: str
    query: str


class AgentOutput(TypedDict):
    source: str
    result: str


class RouterState(TypedDict):
    """路由工作流的状态。

    results 用 Annotated[list, operator.add] —— 并行分支各自返回列表时会自动拼接，
    这是并行路由能成立的关键（否则后返回的会覆盖先返回的）。
    """

    query: str
    classifications: list[Classification]
    results: Annotated[list[AgentOutput], operator.add]
    final_answer: str


class AgentInput(TypedDict):
    query: str


# ---------------------------------------------------------------------------
# 通用构建器
# ---------------------------------------------------------------------------
def build_router_workflow(
    model: Any,
    sources: dict[str, tuple[str, list]],
    *,
    parallel: bool = True,
    synthesize_model: Any = None,
):
    """构建一个多源路由工作流。

    Args:
        model: 专家代理使用的模型
        sources: {源名: (该源的 system_prompt, 该源可用的工具列表)}
        parallel: True 用 Send 并行派发；False 只取第一个命中的源（串行）
        synthesize_model: 汇总用的模型，缺省用同一个

    Returns:
        编译好的 LangGraph
    """
    experts = {
        name: create_agent(
            model=model,
            tools=list(tools),
            system_prompt=prompt,
            name=f"{name}_expert",
        )
        for name, (prompt, tools) in sources.items()
    }

    # 注：Literal[tuple(sources)] 这种动态字面量类型目前没被用到，
    # 留着会成为无人引用的死变量，因此移除。

    def classify_query(state: RouterState) -> dict:
        """判断该查哪些知识源。

        真实项目里建议用带 structured output 的小模型做分类，成本更低也更稳定。
        这里用关键词兜底，保证零依赖可跑。
        """
        q = state["query"].lower()
        hits: list[Classification] = []

        keyword_map = {
            "code": ("代码", "函数", "仓库", "commit", "github", "code"),
            "docs": ("文档", "说明", "wiki", "规范", "notion", "doc"),
            "chat": ("讨论", "群", "消息", "slack", "会话", "记录"),
        }
        for source, words in keyword_map.items():
            if source in sources and any(w in q for w in words):
                hits.append({"source": source, "query": state["query"]})

        if not hits:  # 一个都没命中就全查，交由汇总阶段裁剪
            hits = [{"source": name, "query": state["query"]} for name in sources]
        return {"classifications": hits}

    def make_expert_node(name: str):
        def node(state: AgentInput) -> dict:
            result = experts[name].invoke(
                {"messages": [{"role": "user", "content": state["query"]}]}
            )
            last = result["messages"][-1]
            content = getattr(last, "content", "")
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            return {"results": [{"source": name, "result": content}]}

        node.__name__ = f"query_{name}"
        return node

    def route_to_agents(state: RouterState) -> Any:
        picks = state["classifications"]
        if not parallel:
            return picks[0]["source"]
        return [Send(c["source"], {"query": c["query"]}) for c in picks]

    def synthesize_results(state: RouterState) -> dict:
        joined = "\n\n".join(
            f"【{r['source']}】{r['result']}" for r in state.get("results", [])
        )
        agent = create_agent(
            model=synthesize_model or model,
            tools=[],
            system_prompt=(
                "你是汇总助手。下面是从多个知识源取回的结果，请去重、消解冲突，"
                "给出一份可直接使用的答案。不确定的地方要明确标注来源。"
            ),
            name="synthesizer",
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": f"原始问题：{state['query']}\n\n各源结果：\n{joined}"}]}
        )
        last = result["messages"][-1]
        content = getattr(last, "content", "")
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return {"final_answer": content}

    builder = StateGraph(RouterState)
    builder.add_node("classify", classify_query)
    for name in sources:
        builder.add_node(name, make_expert_node(name))
    builder.add_node("synthesize", synthesize_results)

    builder.add_edge(START, "classify")
    builder.add_conditional_edges("classify", route_to_agents, list(sources))
    for name in sources:
        builder.add_edge(name, "synthesize")
    builder.add_edge("synthesize", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# Custom workflow：把任意 create_agent 塞进手写图
# ---------------------------------------------------------------------------
def build_custom_workflow(agent: Any, *, state_keys: tuple[str, ...] = ("query", "answer")):
    """最简单的自定义工作流：START → agent → END。

    当你需要「在 Agent 外面再套一层固定流程」（前后置校验、审批、日志）时用这个。
    """

    class State(TypedDict):
        query: str
        answer: str

    def agent_node(state: State) -> dict:
        result = agent.invoke({"messages": [{"role": "user", "content": state["query"]}]})
        last = result["messages"][-1]
        content = getattr(last, "content", "")
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        return {"answer": content}

    workflow = StateGraph(State)
    workflow.add_node("agent", agent_node)
    workflow.add_edge(START, "agent")
    workflow.add_edge("agent", END)
    return workflow.compile()
