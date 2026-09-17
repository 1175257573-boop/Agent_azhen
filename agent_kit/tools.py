"""工具集（Tool Calling 层）。

一个「全面」的 Agent 工具层应当覆盖这几类写法，本文件全部演示：

  1. 纯函数工具          —— @tool + 类型注解 + docstring（最常见）
  2. 带校验的工具        —— args_schema（枚举 / 范围约束由 Pydantic 兜底）
  3. 需要运行时信息的    —— 注入 ToolRuntime，读 state / context / store
  4. 有副作用的危险工具  —— 沙箱目录限制，交给 HumanInTheLoopMiddleware 拦截
  5. 可能失败的外部依赖  —— 主动抛 ToolException，由 ToolRetryMiddleware 兜底
"""

from __future__ import annotations

import ast
import math
import operator
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from langchain.agents.middleware.tool_error import ToolErrorMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import ToolException
from pydantic import BaseModel, Field

NOTES_DIR = Path(__file__).resolve().parent.parent / "notes"


def _now() -> datetime:
    """带时区的「现在」：UTC 取值后转本地时区，isoformat 会带 +08:00 这类偏移。

    直接用 datetime.now() 得到的是 naive 对象，一旦存进 PostgreSQL 就丢了时区，
    跨时区读取必然错乱。
    """
    return datetime.now(timezone.utc).astimezone()


# ---------------------------------------------------------------------------
# 1) 纯函数工具：安全计算器
# ---------------------------------------------------------------------------
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "log": math.log, "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}


def _safe_eval(node: ast.AST) -> float:
    """只放行白名单 AST 节点，杜绝 eval() 的代码注入风险。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCS:
        return _ALLOWED_FUNCS[node.func.id](*[_safe_eval(a) for a in node.args])
    raise ValueError(f"不允许的表达式片段：{ast.dump(node)[:60]}")


@tool
def calculator(expression: str) -> str:
    """计算一个数学表达式，如 'sqrt(16) + 2**10'。只支持数值运算，拒绝字符串与变量。

    Args:
        expression: 数学表达式字符串
    """
    try:
        value = _safe_eval(ast.parse(expression, mode="eval"))
    except Exception as exc:  # noqa: BLE001 —— 错误信息要回传给模型，让它自己纠正
        return f"计算失败：{exc}。请换一个只含数值与数学函数的表达式。"
    return f"{expression} = {value:g}"


@tool
def get_current_time() -> str:
    """获取当前时间，格式 2026-09-16 18:30:00 +0800。用于所有与时间相关的判断。"""
    # 先取 UTC 再转本地，保证带 offset——naive datetime 会让模型误判时区
    return _now().strftime("%Y-%m-%d %H:%M:%S %z").strip()


# ---------------------------------------------------------------------------
# 2) 带校验的工具：本地知识库检索（把枚举约束交给 args_schema）
# ---------------------------------------------------------------------------
class SearchNotesInput(BaseModel):
    """args_schema 的用途：给参数加「模型看得懂、也强制校验」的约束。"""

    query: str = Field(description="检索关键词，1~3 个词最佳")
    top_k: int = Field(default=3, ge=1, le=10, description="返回条数上限")
    strategy: Literal["keyword", "phrase"] = Field(
        default="keyword", description="keyword=逐词命中计分；phrase=整串精确匹配优先"
    )


def _load_notes() -> list[tuple[str, str]]:
    if not NOTES_DIR.exists():
        return []
    return [(p.stem, p.read_text(encoding="utf-8")) for p in sorted(NOTES_DIR.glob("*.md"))]


@tool(args_schema=SearchNotesInput)
def search_notes(query: str, top_k: int = 3, strategy: str = "keyword") -> str:
    """在本地笔记库中检索相关内容（离线知识库，无需联网）。

    Args:
        query: 检索关键词
        top_k: 返回条数上限
        strategy: keyword 或 phrase
    """
    notes = _load_notes()
    if not notes:
        return "笔记库为空。"

    scored: list[tuple[int, str, str]] = []
    q = query.strip().lower()
    for title, body in notes:
        low = body.lower()
        title_low = title.lower()
        if strategy == "phrase":
            score = low.count(q) * 5 + title_low.count(q) * 10
        else:
            score = sum(low.count(w) * 2 for w in re.split(r"\s+", q) if w) + title_low.count(q) * 10
        if score > 0:
            scored.append((score, title, body))

    if not scored:
        return f"笔记库中没有与「{query}」相关的内容。"

    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, title, body in scored[:top_k]:
        snippet = re.sub(r"\s+", " ", body)[:220]
        out.append(f"[相关性 {score}] {title}\n{snippet}...")
    return "\n\n---\n\n".join(out)


@tool
def list_notes() -> str:
    """列出本地笔记库中所有笔记的标题。检索前应先用它确认有哪些主题。"""
    notes = _load_notes()
    return "\n".join(f"- {t}" for t, _ in notes) if notes else "笔记库为空。"


# ---------------------------------------------------------------------------
# 2.5) 语义检索（RAG）：与上面的字面检索形成对比
# ---------------------------------------------------------------------------
_INDEX_CACHE: dict[str, object] = {}


def _get_index() -> tuple[object, object]:
    """惰性建索引并缓存。

    返回 (index, choice)。降级状态一并返回——调用方要能告诉用户
    「你现在用的是离线向量，效果会差一些」，而不是默默给出结果。
    """
    from agent_kit.retrieval import build_index, get_embedder, load_docs

    cached = _INDEX_CACHE.get("index")
    if cached is not None:
        return cached, _INDEX_CACHE["choice"]

    choice = get_embedder()
    index = build_index(load_docs(NOTES_DIR), choice.embedder)
    _INDEX_CACHE["index"] = index
    _INDEX_CACHE["choice"] = choice
    return index, choice


@tool
def search_knowledge(query: str, top_k: int = 3) -> str:
    """在本地笔记库中做**语义检索**：说法不同但意思相近时也能命中。

    与 search_notes（字面关键词匹配）的区别：
    问「怎么复习才记得住」这种问法，字面检索搜不到标题叫「遗忘曲线」的笔记，
    而语义检索可以。不确定该用哪个时，先用这个。

    Args:
        query: 自然语言问题或关键词
        top_k: 返回条数上限
    """
    try:
        index, choice = _get_index()
        if len(index) == 0:
            return "笔记库为空，无法检索。"

        # 查询向量必须走与建库相同的后端，否则维度对不上
        qv = choice.embedder.embed([query])[0]
        hits = index.search(qv, top_k=top_k)
        if not hits:
            return f"笔记库中没有与「{query}」语义相关的内容。"

        lines = []
        for h in hits:
            # 正则里的反斜杠不能写进 f-string 表达式：
            # Python 3.10 不允许，本地 3.12 能跑而 CI 的 3.10 会直接 SyntaxError。
            snippet = re.sub(r"\s+", " ", h.text)[:220]
            lines.append(f"[相似度 {h.score:.3f}] {h.doc_id}\n{snippet}...")
        head = ""
        if choice.degraded:
            head = f"（当前为离线向量降级模式：{choice.reason}，语义相似度仅供参考）\n\n"
        return head + "\n\n---\n\n".join(lines)
    except Exception as exc:
        raise ToolException(f"语义检索失败：{exc}") from exc


@tool
def rebuild_knowledge_index(backend: str = "auto") -> str:
    """重建语义检索索引。笔记内容有变动后调用它，否则检索用的还是旧索引。

    Args:
        backend: auto（有 Key 用真实向量）/ dashscope / hashing
    """
    from agent_kit.retrieval import build_index, get_embedder, load_docs

    try:
        choice = get_embedder(backend)
        index = build_index(load_docs(NOTES_DIR), choice.embedder)
        _INDEX_CACHE.clear()
        _INDEX_CACHE["index"] = index
        _INDEX_CACHE["choice"] = choice
        note = f"（降级：{choice.reason}）" if choice.degraded else ""
        return f"索引已重建：{len(index)} 个片段，后端 {choice.backend}。{note}"
    except Exception as exc:
        raise ToolException(f"重建索引失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 3) 需要运行时信息的工具：长期记忆读写（依赖 ToolRuntime）
# ---------------------------------------------------------------------------
@tool
def remember_preference(
    key: str,
    value: str,
    runtime: ToolRuntime,
) -> str:
    """把用户的偏好写入长期记忆（跨会话保存）。

    Args:
        key: 偏好名，如 'language'、'tone'、'domain'
        value: 偏好值
    """
    store = runtime.store
    if store is None:
        return "未启用 store，无法写入长期记忆。"

    user_id = getattr(runtime.context, "user_id", "anonymous") if runtime.context else "anonymous"
    store.put(("preferences", user_id), key, {"value": value, "updated_at": _now().isoformat()})
    return f"已记住：{key} = {value}（用户 {user_id}）"


@tool
def recall_preferences(runtime: ToolRuntime) -> str:
    """读取当前用户的全部长期偏好。开始回答问题前建议先调用一次。"""
    store = runtime.store
    if store is None:
        return "未启用 store，无长期记忆。"
    user_id = getattr(runtime.context, "user_id", "anonymous") if runtime.context else "anonymous"
    items = store.search(("preferences", user_id))
    if not items:
        return f"用户 {user_id} 暂无长期偏好记录。"
    return "\n".join(f"- {it.key}: {it.value['value']}" for it in items)


# ---------------------------------------------------------------------------
# 4) 危险工具：写文件（沙箱 + 交给人工确认）
# ---------------------------------------------------------------------------
@tool
def write_report(filename: str, content: str, runtime: ToolRuntime) -> str:
    """把报告写入沙箱目录 runs/，禁止路径穿越。这是唯一的写操作入口。

    Args:
        filename: 文件名，只允许字母数字、下划线与中划线，不含路径分隔符
        content: 正文内容
    """
    if re.search(r"[\\/:*?\"<>|]", filename) or filename in {".", ".."}:
        raise ToolException(f"非法文件名：{filename}")

    root: Path = Path(__file__).resolve().parent.parent
    target_dir = root / "runs"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / f"{filename}.md").resolve()

    if target_dir.resolve() not in target.parents:
        raise ToolException("检测到路径穿越，已拒绝写入。")

    target.write_text(content, encoding="utf-8")
    return f"已写入 {target}（{len(content)} 字符）"


# ---------------------------------------------------------------------------
# 5) 外部依赖工具：抓取网页（失败时抛 ToolException 让重试中间件处理）
# ---------------------------------------------------------------------------
@tool
def fetch_url(url: Annotated[str, "必须是 http/https 开头的完整 URL"], max_chars: int = 1500) -> str:
    """抓取网页正文并转成纯文本。可能失败（超时/被拒），失败会自动重试。

    Args:
        url: 目标网页地址
        max_chars: 返回正文的最大字符数
    """
    if not url.startswith(("http://", "https://")):
        raise ToolException(f"非法 URL：{url}")
    try:
        import requests

        resp = requests.get(url, timeout=10, headers={"User-Agent": "AtlasAgent/1.0"})
        resp.raise_for_status()
    except Exception as exc:
        raise ToolException(f"抓取失败：{exc}") from exc

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", resp.text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] or "页面无正文内容。"


# ---------------------------------------------------------------------------
# 工具清单：按用途分组，便于按需装载
# ---------------------------------------------------------------------------
SAFE_TOOLS = [
    calculator,
    get_current_time,
    search_notes,
    search_knowledge,
    rebuild_knowledge_index,
    list_notes,
    remember_preference,
    recall_preferences,
    fetch_url,
]
"""只读 / 低风险工具，任何场景都可以给模型。"""

WRITE_TOOLS = [write_report]
"""有副作用，应由 HumanInTheLoopMiddleware 拦截确认。"""

ALL_TOOLS = SAFE_TOOLS + WRITE_TOOLS

DANGEROUS_TOOL_NAMES = {"write_report"}
"""会真正修改磁盘的工具——这里的名单同时喂给 HITL 与工具限流中间件。"""

# ---------------------------------------------------------------------------
# 工具异常兜底：把异常转成 ToolMessage 交还给模型，而不是让整条执行链路崩掉
# ---------------------------------------------------------------------------
def _format_tool_error(exc: Exception, call: ToolCallRequest) -> str:
    """on_error 回调：返回字符串 → 变成 status="error" 的 ToolMessage；返回 None → 异常上抛。"""
    tool_name = call.tool_call.get("name", "unknown_tool")
    return f"工具 {tool_name} 执行出错（{type(exc).__name__}）：{exc}。请修正参数后重试，或换一条路线。"


error_handler = ToolErrorMiddleware(on_error=_format_tool_error)
