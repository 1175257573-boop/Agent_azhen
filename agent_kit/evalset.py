"""Agent 效果评估（最小可用版）。

为什么要这个模块
----------------
单元测试只能证明「代码没崩」，证明不了「Agent 干得好」。
改一版提示词、换一个模型、加一个工具之后，效果到底是变好还是变差了？
没有评估集就只能凭感觉——这是 Agent 工程里最容易被忽略、也最容易被问住的缺口。

两条运行路径
------------
1. **真机评估**（`--real`）：用真实模型跑，产出真实的工具选择准确率与关键词命中率。
   需要 API Key，是评估的真正用途。
2. **离线自检**（默认）：用 ScriptedChatModel 编排确定性的工具调用与回答，
   验证**评估器本身**判得准（包括故意造失败的用例）。
   不需要 Key，进 CI，防止评估框架自己腐化。

评估什么
--------
- 工具选择准确率：该调的工具调了没有（Agent 最容易错的一步）
- 关键词命中率：最终回答里有没有该有的信息
- 用例通过率：两项都过线才算通过

刻意不做：不给「Agent 智能程度」打分。那是主观的，而且没有对照组就没意义。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

# 判定阈值：工具必须全部命中（漏调一个就是错的），关键词允许部分命中
# （同一个意思有多种说法，要求全命中会误导）
DEFAULT_TOOL_THRESHOLD = 1.0
DEFAULT_KEYWORD_THRESHOLD = 0.5


@dataclass(frozen=True)
class EvalCase:
    """一条评估用例。

    expect_tools / expect_keywords 是**人工标注**的期望，不是模型生成的——
    用模型给自己打分等于没评。
    """

    id: str
    query: str
    expect_tools: tuple[str, ...] = ()
    expect_keywords: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ScriptedCase:
    """离线自检用例：附带「模型会怎么答」的编排，用于验证评估器判定正确。"""

    case: EvalCase
    scripted_tools: tuple[str, ...] = ()
    scripted_answer: str = ""


@dataclass
class CaseResult:
    case_id: str
    query: str
    called_tools: tuple[str, ...]
    final_answer: str
    tool_hit: float
    keyword_hit: float
    passed: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "query": self.query,
            "called_tools": list(self.called_tools),
            "final_answer": self.final_answer[:300],
            "tool_hit": round(self.tool_hit, 3),
            "keyword_hit": round(self.keyword_hit, 3),
            "passed": self.passed,
            "error": self.error,
        }


@dataclass
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def tool_accuracy(self) -> float:
        return _mean([r.tool_hit for r in self.results])

    @property
    def keyword_rate(self) -> float:
        return _mean([r.keyword_hit for r in self.results])

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 3),
            "tool_accuracy": round(self.tool_accuracy, 3),
            "keyword_rate": round(self.keyword_rate, 3),
            "results": [r.to_dict() for r in self.results],
        }

    def to_text(self, *, show_all: bool = False) -> str:
        lines = [
            f"用例：{self.total}    通过：{self.passed}    通过率：{self.pass_rate:.1%}",
            f"工具选择准确率：{self.tool_accuracy:.1%}    关键词命中率：{self.keyword_rate:.1%}",
        ]
        rows = self.results if show_all else self.failures
        if rows:
            lines.append("")
            lines.append("未通过：" if not show_all else "全部用例：")
            for r in rows:
                flag = "PASS" if r.passed else "FAIL"
                lines.append(
                    f"  [{flag}] {r.case_id} 工具 {r.tool_hit:.0%} / 关键词 {r.keyword_hit:.0%}"
                    f"  实际调用 {list(r.called_tools) or '无'}"
                )
                if r.error:
                    lines.append(f"        错误：{r.error}")
        return "\n".join(lines)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def collect_tool_calls(messages: Sequence[BaseMessage]) -> tuple[str, ...]:
    """从消息流里收集模型实际调用过的工具名（按出现顺序，去重）。"""
    names: list[str] = []
    for msg in messages:
        calls = getattr(msg, "tool_calls", None)
        if not calls:
            continue
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name and name not in names:
                names.append(name)
    return tuple(names)


def final_answer(messages: Sequence[BaseMessage]) -> str:
    """取最后一条有内容的 AI 消息作为最终回答。"""
    text = ""
    for msg in messages:
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str) and content.strip():
                text = content
            elif isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                if "".join(parts).strip():
                    text = "".join(parts)
    return text


def judge(
    case: EvalCase,
    called_tools: Sequence[str],
    answer: str,
    *,
    tool_threshold: float = DEFAULT_TOOL_THRESHOLD,
    keyword_threshold: float = DEFAULT_KEYWORD_THRESHOLD,
) -> CaseResult:
    """判定单条用例。

    工具项：期望工具没标注时不参与考核（算满分），标注了就按命中比例算。
    关键词项：大小写不敏感的包含匹配，允许部分命中。
    """
    if case.expect_tools:
        hit = {t for t in case.expect_tools if t in set(called_tools)}
        tool_hit = len(hit) / len(case.expect_tools)
    else:
        tool_hit = 1.0

    if case.expect_keywords:
        low = answer.lower()
        kw_hit = sum(1 for k in case.expect_keywords if k.lower() in low)
        keyword_hit = kw_hit / len(case.expect_keywords)
    else:
        keyword_hit = 1.0

    return CaseResult(
        case_id=case.id,
        query=case.query,
        called_tools=tuple(called_tools),
        final_answer=answer,
        tool_hit=tool_hit,
        keyword_hit=keyword_hit,
        passed=tool_hit >= tool_threshold and keyword_hit >= keyword_threshold,
    )


def evaluate(
    cases: Sequence[EvalCase],
    runner: Callable[[str], Mapping[str, Any]],
    *,
    tool_threshold: float = DEFAULT_TOOL_THRESHOLD,
    keyword_threshold: float = DEFAULT_KEYWORD_THRESHOLD,
) -> EvalReport:
    """跑完整评估集。runner 接收 query，返回含 messages 的结果。"""
    report = EvalReport()
    for case in cases:
        try:
            result = runner(case.query)
            messages = result.get("messages", []) if isinstance(result, Mapping) else []
            result_obj = judge(
                case,
                collect_tool_calls(messages),
                final_answer(messages),
                tool_threshold=tool_threshold,
                keyword_threshold=keyword_threshold,
            )
        except Exception as exc:  # noqa: BLE001 —— 单条失败不该中断整轮评估，这里必须兜住所有异常
            result_obj = CaseResult(
                case_id=case.id,
                query=case.query,
                called_tools=(),
                final_answer="",
                tool_hit=0.0,
                keyword_hit=0.0,
                passed=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        report.results.append(result_obj)
    return report


# ---------------------------------------------------------------------------
# 用例集
# ---------------------------------------------------------------------------
# 真机用例：期望由人工标注，跑真实模型。改提示词 / 换模型 / 加工具后重跑，
# 对比通过率就知道是变好还是变差。
REAL_CASES: tuple[EvalCase, ...] = (
    EvalCase("time-1", "现在几点了？", ("get_current_time",), ("时间",), "最简单的单工具调用"),
    EvalCase("calc-1", "计算 (17 + 25) * 8 等于多少", ("calculator",), ("336",), "算术必须走工具，不能靠模型心算"),
    EvalCase(
        "notes-1", "笔记库里都有哪些主题？", ("list_notes",), ("笔记",), "列目录类问题"
    ),
    EvalCase(
        "rag-1",
        "怎么复习才记得住？",
        ("search_knowledge",),
        ("复习",),
        "语义检索的核心场景：问法与笔记标题不重叠",
    ),
    EvalCase(
        "rag-2",
        "接入别人的 MCP 服务之前要先检查什么？",
        ("search_knowledge",),
        ("越界",),
        "语义检索：应命中 mcp-integration 笔记",
    ),
    EvalCase(
        "mcp-1", "这个项目有哪些 MCP 工具可用？", (), ("MCP",), "不考核具体工具，只看是否答到点上"
    ),
)

# 离线自检用例：scripted_* 是「模型会怎么答」的编排。
# 其中故意放了两条会失败的（tool-miss / kw-miss），用来证明评估器不是永远判过。
SELFTEST_CASES: tuple[ScriptedCase, ...] = (
    ScriptedCase(
        EvalCase("ok-time", "现在几点？", ("get_current_time",), ("时间",)),
        scripted_tools=("get_current_time",),
        scripted_answer="当前时间是 12:00。",
    ),
    ScriptedCase(
        EvalCase("ok-rag", "怎么复习？", ("search_knowledge",), ("复习", "间隔")),
        scripted_tools=("search_knowledge",),
        scripted_answer="建议按间隔重复安排复习。",
    ),
    ScriptedCase(
        EvalCase("tool-miss", "现在几点？", ("calculator",), ("时间",)),
        scripted_tools=("get_current_time",),
        scripted_answer="当前时间是 12:00。",
    ),
    ScriptedCase(
        EvalCase("kw-miss", "怎么复习？", ("search_knowledge",), ("量子力学",)),
        scripted_tools=("search_knowledge",),
        scripted_answer="建议按间隔重复安排复习。",
    ),
)


def selftest_cases() -> tuple[EvalCase, ...]:
    return tuple(sc.case for sc in SELFTEST_CASES)


def save_report(report: EvalReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
