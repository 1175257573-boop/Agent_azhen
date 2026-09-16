"""MCP Server 演示：真实拉起 stdio 子进程，列出工具并调用。

和「直接 import 函数」的区别：
  这里走的是完整 MCP 协议链路（启动子进程 → 握手 → list_tools → call_tool），
  能验证「协议层是否真的通」。函数本身对不对由单元测试负责，那是两回事。

零 API Key，直接跑：
    python examples/mcp_servers_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_kit.mcp_client import ALL_SERVERS, MCPHub

# 每个 server 挑一个代表性工具做真实调用
CALLS: dict[str, tuple[str, dict]] = {
    "quality": ("project_code_stats", {"subdir": "."}),
    "git": ("git_status", {"subdir": "."}),
    "docs": ("check_anchors", {"subdir": "."}),
}


def _flatten(result: object) -> str:
    """MCP 返回的是内容块列表（[{type:'text', text:'...'}]），摊平成纯文本再打印。"""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in result
        )
    return str(result)


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


async def main() -> None:
    hub = MCPHub()

    _banner("1 · 逐个连接 MCP Server（stdio 子进程）")
    for name in ALL_SERVERS:
        before = len(hub.tools)
        await hub.connect([ALL_SERVERS[name]])
        names = sorted(t.name for t in hub.tools[before:])
        print(f"  {name:8} -> {len(names)} 个工具：{', '.join(names)}")

    print(f"\n  合计 {len(hub.tools)} 个工具已就绪")

    _banner("2 · 真实调用（走完整协议链路）")
    by_name = {t.name: t for t in hub.tools}
    for server, (tool_name, args) in CALLS.items():
        tool = by_name.get(tool_name)
        if tool is None:
            print(f"  [{server}] 未找到工具 {tool_name}")
            continue
        result = await tool.ainvoke(args)
        print(f"\n  [{server}] {tool_name}({args})")
        print(f"  {_flatten(result)[:400]}")

    _banner("3 · Skill 怎么调这些 MCP（方法论 → 取证）")
    from agent_kit.builtin_skills import PROJECT_ENGINEERING
    from agent_kit.multi_agent.skills import skills_summary

    print("  L1 常驻系统提示的内容（几十 token）：")
    print(f"  {skills_summary([PROJECT_ENGINEERING])}")
    print("\n  L2 是完整 SOP，模型调 load_skill('project_engineering') 才加载：")
    print(f"  共 {len(PROJECT_ENGINEERING['content'].splitlines())} 行，开头是：")
    print("  " + PROJECT_ENGINEERING["content"].strip().splitlines()[0])

    await hub.aclose()
    print("\n  已关闭全部 MCP 连接（stdio 子进程已回收）")


if __name__ == "__main__":
    asyncio.run(main())
