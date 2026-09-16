"""带**完整 MCP 协议能力**的服务端：工具 + 资源 + 进度通知 + 日志 + 引导式输入。

进度通知（progress）—— 长任务定期回报 "2/5"，客户端可渲染进度条
日志记录（logging）  —— server 端 debug/info/warning/error，客户端按级别接收
引导式输入（elicit） —— 工具执行过程中反过来向用户要信息（比如「请补充邮箱」），
                        而不是让整个调用失败后再重来

启动：
    python agent_kit/mcp_server.py --stdio              # stdio（默认）
    python agent_kit/mcp_server.py --http --port 8000   # streamable-http
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastmcp import Context, FastMCP
from pydantic import BaseModel

NOTES_DIR = Path(__file__).resolve().parent.parent / "notes"

mcp = FastMCP("atlas-notes-mcp")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
@mcp.tool
def word_count(text: str) -> int:
    """统计文本词数（按空白切分）。

    Args:
        text: 待统计的文本
    """
    return len(text.split())


@mcp.tool
def current_utc() -> str:
    """返回当前 UTC 时间（ISO8601）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@mcp.tool
def list_note_files() -> list[str]:
    """列出知识库里所有笔记的文件名。"""
    if not NOTES_DIR.exists():
        return []
    return [p.name for p in sorted(NOTES_DIR.glob("*.md"))]


# ---------------------------------------------------------------------------
# 进度通知：ctx.report_progress(progress, total, message)
# ---------------------------------------------------------------------------
@mcp.tool
async def analyze_corpus(ctx: Context) -> str:
    """扫描整个知识库并统计词频，过程中会回报进度。

    演示长任务如何把进度推给客户端；客户端需注册 on_progress 回调才能收到。
    """
    files = list(NOTES_DIR.glob("*.md")) if NOTES_DIR.exists() else []
    total = float(max(len(files), 1))

    freq: dict[str, int] = {}
    for idx, path in enumerate(files, start=1):
        await ctx.report_progress(float(idx), total, f"正在分析 {path.name} ...")
        await asyncio.sleep(0.15)  # 模拟耗时，让进度可见
        for word in path.read_text(encoding="utf-8").lower().split():
            if len(word) > 3:
                freq[word] = freq.get(word, 0) + 1

    top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return f"共 {len(files)} 篇笔记；高频词：{', '.join(f'{w}({c})' for w, c in top)}"


# ---------------------------------------------------------------------------
# 日志记录：ctx.debug / info / warning / error
# ---------------------------------------------------------------------------
@mcp.tool
async def diagnose(ctx: Context, target: str) -> str:
    """对指定目标做一次体检，全程打日志。

    Args:
        target: 体检对象名
    """
    await ctx.debug("初始化检查器 ...")
    await ctx.info(f"开始检查：{target}")
    await asyncio.sleep(0.1)

    ok = True
    if not NOTES_DIR.exists():
        await ctx.warning(f"知识库目录不存在：{NOTES_DIR}")
        ok = False
    else:
        await ctx.info(f"知识库就绪：{len(list(NOTES_DIR.glob('*.md')))} 篇笔记")

    if not ok:
        await ctx.error("检查未通过")
        return f"{target} 检查未通过"

    await ctx.info("检查完成")
    return f"{target} 状态正常"


# ---------------------------------------------------------------------------
# 引导式输入 Elicitation：执行中途向用户索取结构化信息
# ---------------------------------------------------------------------------
class ContactInfo(BaseModel):
    """引导用户补充的信息结构。"""

    email: str
    urgent: bool = False


@mcp.tool
async def create_profile(name: str, ctx: Context) -> str:
    """为用户创建档案；缺少联系方式时会反过来问你要（Elicitation）。

    Args:
        name: 用户名
    """
    result = await ctx.elicit(
        message=f"请补充 {name} 的联系方式（邮箱），并告知是否加急：",
        response_type=ContactInfo,
    )

    if result.action == "accept" and result.data:
        flag = "加急" if result.data.urgent else "普通"
        return f"已创建档案：{name} <{result.data.email}>（{flag}）"
    if result.action == "decline":
        return "用户拒绝补充信息，档案创建中止。"
    return "用户取消了档案创建。"


# ---------------------------------------------------------------------------
# 资源（Resource）：把知识库说明暴露成 MCP 资源
# ---------------------------------------------------------------------------
@mcp.resource("notes://readme")
def notes_readme() -> str:
    """知识库说明（MCP 资源示例）。"""
    files = list(NOTES_DIR.glob("*.md")) if NOTES_DIR.exists() else []
    return f"本地知识库，共 {len(files)} 篇笔记：\n" + "\n".join(f"- {p.name}" for p in files)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = 8000
        for arg in args:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")
