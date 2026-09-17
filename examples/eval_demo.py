"""Agent 效果评估的两种跑法。

    python main.py eval            # 离线自检：验证评估器本身判得准（进 CI，零 Key）
    python main.py eval --real     # 真机评估：真实模型，产出真实指标（需要 API Key）
    python main.py eval --real --save eval/baseline.json

离线自检跑的是「编排好的模型」，所以指标数字没有意义，
它的价值在于证明评估器能正确区分通过与不通过。
真机跑出来的数字才代表 Agent 的真实效果。
"""

from __future__ import annotations

import argparse
import uuid

from langchain_core.messages import AIMessage, HumanMessage

from agent_kit import memory as mem
from agent_kit.agent import build_agent
from agent_kit.config import AgentSettings
from agent_kit.evalset import (
    REAL_CASES,
    SELFTEST_CASES,
    EvalReport,
    collect_tool_calls,
    final_answer,
    judge,
)
from agent_kit.scripted_model import ScriptedChatModel, tool_call

ADMIN_CTX: dict = {"user_id": "eval", "role": "admin", "locale": "zh-CN"}

# 编排工具调用时要带上必需参数，否则工具会报「字段缺失」，
# 日志里一堆红字，也让自检看起来像失败了
_QUERY_TOOLS = {"search_knowledge", "search_notes"}


def _args_for(tool_name: str, query: str) -> dict:
    return {"query": query} if tool_name in _QUERY_TOOLS else {}


def run_offline() -> EvalReport:
    """离线自检：每条用例用编排好的模型跑一遍。"""
    from agent_kit.evalset import EvalReport as _Report

    report = _Report()
    for sc in SELFTEST_CASES:
        script = [tool_call(name, _args_for(name, sc.case.query)) for name in sc.scripted_tools]
        # 最后一轮给出最终回答；默认参数避免闭包捕获循环变量
        script.append(lambda msgs, tools, ans=sc.scripted_answer: AIMessage(content=ans))

        built = build_agent(
            AgentSettings(provider="fake"),
            model=ScriptedChatModel(script=script),
            include_write_tools=False,
        )
        result = built.graph.invoke(
            {"messages": [HumanMessage(content=sc.case.query)]},
            config=mem.thread_config(f"eval-{sc.case.id}-{uuid.uuid4().hex[:8]}"),
            context=ADMIN_CTX,
        )
        messages = result.get("messages", [])
        report.results.append(
            judge(sc.case, collect_tool_calls(messages), final_answer(messages))
        )
    return report


def run_real() -> EvalReport:
    """真机评估：用真实模型跑真机用例集。"""
    from agent_kit.config import require_api_key
    from agent_kit.evalset import EvalReport as _Report

    settings = AgentSettings()
    # 没配 Key 时 provider 会退化成 fake，此时跑出来的「评估报告」是假的——
    # 数字看着正常，实际测的是编排模型。必须在这里 fail fast。
    if settings.provider == "fake":
        raise SystemExit(
            "真机评估需要真实模型，但当前未检测到任何 API Key（provider 退化为 fake）。\n"
            "  设置 DASHSCOPE_API_KEY（或 OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY）后重开终端。\n"
            "  只想验证评估框架本身：python main.py eval"
        )
    require_api_key(settings)
    built = build_agent(settings, include_write_tools=False)
    report = _Report()
    for case in REAL_CASES:
        result = built.graph.invoke(
            {"messages": [HumanMessage(content=case.query)]},
            config=mem.thread_config(f"eval-{case.id}-{uuid.uuid4().hex[:8]}"),
            context=ADMIN_CTX,
        )
        messages = result.get("messages", [])
        report.results.append(judge(case, collect_tool_calls(messages), final_answer(messages)))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 效果评估")
    parser.add_argument("--real", action="store_true", help="真机评估（需要 API Key）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--show-all", action="store_true", help="列出全部用例而非仅失败项")
    parser.add_argument("--save", metavar="PATH", help="把结果存为基线 JSON")
    parser.add_argument("--compare", metavar="PATH", help="与基线对比，通过率下降则返回非零")
    args = parser.parse_args(argv)

    if args.real:
        print("真机评估：调用真实模型（需要 DASHSCOPE_API_KEY 等环境变量）")
        report = run_real()
    else:
        print("离线自检：验证评估器判定是否正确（不调用真实模型）")
        report = run_offline()

    print()
    print(report.to_text(show_all=args.show_all) or "（无结果）")

    if args.json:
        import json

        print()
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    if args.save:
        from pathlib import Path

        from agent_kit.evalset import save_report

        save_report(report, args.save)
        print(f"\n基线已保存：{args.save}")

    exit_code = 0
    if args.compare:
        import json
        from pathlib import Path

        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        delta = report.pass_rate - base.get("pass_rate", 0.0)
        arrow = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")
        print(f"\n与基线对比：{base.get('pass_rate', 0):.1%} → {report.pass_rate:.1%}（{arrow} {delta:+.1%}）")
        if delta < 0:
            print("通过率下降，请检查本次改动。")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
