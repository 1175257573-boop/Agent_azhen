"""MCP 工具接入演示（单独运行）。

    python mcp_demo.py

流程：
  1. 用 `langchain.mcp.MCPAdapter` 连接一个本地 stdio MCP Server
  2. `await adapter.list_tools()` 拿到 LangChain 形态的工具（异步工具）
  3. 把这些工具塞进 create_agent，用 ainvoke 跑一次

为什么单独成一是因为：MCP 工具是异步的，需要用 `ainvoke` / `astream`；
且它会真的 fork 子进程跑 MCP Server，速度比其它场景慢。
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain.agents import create_agent
from langchain.mcp import MCPAdapter
from langchain_core.messages import AIMessage, HumanMessage

from agent_kit.scripted_model import ScriptedChatModel, has_tool_output, last_tool_result, tool_call

SERVER = Path(__file__).resolve().parent / "agent_kit" / "mcp_server.py"


def text_of(message: Any) -> str:
    """把消息的 content 归一化成纯文本。

    LangChain 1.x 的消息 content 有三种形态：
      str                       —— 传统文本
      list[dict]                —— 标准 Content Blocks（MCP 工具返回常见）
      list[ContentBlock 对象]   —— pydantic 模型形态
    三种都要照顾到，否则日志里会打印出原始区块结构。
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict):
                parts.append(blk.get("text") or blk.get("content") or str(blk))
            else:
                parts.append(str(getattr(blk, "text", blk)))
        return " ".join(p for p in parts if p)
    return str(content)


async def main() -> None:
    # 1) 连接 MCP Server。
    #    注意：target 传字符串时会被当作 http(s) URL；要跑本地 stdio 服务必须传 Path 对象，
    #    否则会报 "must be an http or https URL"。这个约定是为了避免字符串被静默当子进程执行。
    adapter = MCPAdapter(SERVER)
    mcp_tools = await adapter.list_tools()
    print(f"✅ 已从 MCP Server 发现 {len(mcp_tools)} 个工具：{[t.name for t in mcp_tools]}")

    # 2) 组装 Agent
    def step(msgs, tools):
        for name in ("word_count", "current_utc", "upper_case"):
            if has_tool_output(msgs, name):
                return AIMessage(content=f"MCP 工具 {name} 返回：{last_tool_result(msgs)}")
        return tool_call("word_count", {"text": "langchain mcp adapter demo"}, call_id="mcp_1")

    agent = create_agent(
        model=ScriptedChatModel(script=[step, step]),
        tools=mcp_tools,
        system_prompt="你是一个演示 Agent，可用的工具来自 MCP Server。",
    )

    # 3) MCP 工具是异步的 → 必须用 ainvoke
    result = await agent.ainvoke({"messages": [HumanMessage(content="统计一下这段文字的词数")]})

    print("\n执行轨迹：")
    for m in result["messages"]:
        role = type(m).__name__.replace("Message", "")
        if getattr(m, "tool_calls", None):
            print(f"  [{role}] <调用> {', '.join(tc['name'] for tc in m.tool_calls)}")
        else:
            print(f"  [{role}] {text_of(m)[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
