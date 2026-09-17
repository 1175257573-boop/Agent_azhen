"""评估模块测试。

重点不是「评估器能通过用例」，而是「评估器能抓到失败用例」——
一个永远判通过的评估器比没有评估更危险。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_kit.evalset import (
    REAL_CASES,
    SELFTEST_CASES,
    CaseResult,
    EvalCase,
    EvalReport,
    collect_tool_calls,
    evaluate,
    final_answer,
    judge,
    selftest_cases,
)


# ---------------------------------------------------------------------------
# 判定逻辑
# ---------------------------------------------------------------------------
def test_judge_passes_when_everything_matches():
    case = EvalCase("c1", "几点了", ("get_current_time",), ("时间",))
    r = judge(case, ["get_current_time"], "当前时间是 12 点")
    assert r.passed
    assert r.tool_hit == 1.0
    assert r.keyword_hit == 1.0


def test_judge_fails_when_expected_tool_not_called():
    case = EvalCase("c2", "算一下", ("calculator",), ("等于",))
    r = judge(case, ["get_current_time"], "结果是 42")
    assert not r.passed
    assert r.tool_hit == 0.0


def test_judge_partial_tool_hit_is_fractional():
    case = EvalCase("c3", "q", ("a", "b"), ())
    r = judge(case, ["a"], "")
    assert r.tool_hit == pytest.approx(0.5)


def test_judge_without_expect_tools_skips_tool_scoring():
    case = EvalCase("c4", "q", (), ("要点",))
    r = judge(case, [], "这是要点")
    assert r.tool_hit == 1.0
    assert r.passed


def test_judge_keyword_is_case_insensitive():
    case = EvalCase("c5", "q", (), ("Redis",))
    assert judge(case, [], "用 redis 存的").keyword_hit == 1.0


def test_judge_keyword_threshold_applies():
    case = EvalCase("c6", "q", (), ("甲", "乙"))
    # 只命中一半，未达到 0.5 阈值以上视为通过需要 >= 0.5
    r = judge(case, [], "只有甲", keyword_threshold=0.5)
    assert r.keyword_hit == pytest.approx(0.5)
    assert r.passed
    assert not judge(case, [], "只有甲", keyword_threshold=0.9).passed


# ---------------------------------------------------------------------------
# 消息解析
# ---------------------------------------------------------------------------
def test_collect_tool_calls_dedupes_and_keeps_order():
    msgs = [
        AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
        ToolMessage(content="r", tool_call_id="1"),
        AIMessage(content="", tool_calls=[{"name": "b", "args": {}, "id": "2"}]),
        AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "3"}]),
    ]
    assert collect_tool_calls(msgs) == ("a", "b")


def test_collect_tool_calls_ignores_messages_without_calls():
    assert collect_tool_calls([HumanMessage(content="hi"), AIMessage(content="ok")]) == ()


def test_final_answer_takes_last_non_empty_ai_message():
    msgs = [
        AIMessage(content="", tool_calls=[{"name": "a", "args": {}, "id": "1"}]),
        ToolMessage(content="tool result", tool_call_id="1"),
        AIMessage(content="最终答案"),
    ]
    assert final_answer(msgs) == "最终答案"


def test_final_answer_empty_when_no_ai_text():
    assert final_answer([HumanMessage(content="hi")]) == ""


# ---------------------------------------------------------------------------
# 整体评估
# ---------------------------------------------------------------------------
def test_evaluate_does_not_abort_on_single_case_error():
    def runner(query: str):
        raise RuntimeError("模型挂了")

    report = evaluate([EvalCase("x", "q", ("a",), ("k",))], runner)
    assert report.total == 1
    assert report.passed == 0
    assert report.results[0].error


def test_report_aggregates():
    results = [
        CaseResult("a", "q", ("t",), "ans", 1.0, 1.0, True),
        CaseResult("b", "q", (), "ans", 0.0, 1.0, False),
    ]
    report = EvalReport(results)
    assert report.total == 2
    assert report.passed == 1
    assert report.pass_rate == pytest.approx(0.5)
    assert report.tool_accuracy == pytest.approx(0.5)
    assert [r.case_id for r in report.failures] == ["b"]
    assert report.to_dict()["pass_rate"] == pytest.approx(0.5)


def test_report_to_text_lists_failures():
    report = EvalReport([CaseResult("b", "q", (), "ans", 0.0, 1.0, False)])
    assert "FAIL" in report.to_text()


# ---------------------------------------------------------------------------
# 用例集本身的合法性
# ---------------------------------------------------------------------------
def test_real_cases_are_well_formed():
    ids = [c.id for c in REAL_CASES]
    assert len(ids) == len(set(ids)), "用例 id 必须唯一"
    assert all(c.query.strip() for c in REAL_CASES)
    assert all(c.expect_tools or c.expect_keywords for c in REAL_CASES), "至少要有一种判据"


def test_selftest_cases_are_well_formed():
    assert len(SELFTEST_CASES) == len({sc.case.id for sc in SELFTEST_CASES})
    assert [c.id for c in selftest_cases()] == [sc.case.id for sc in SELFTEST_CASES]


def test_offline_selftest_has_both_pass_and_fail_cases():
    """最关键的一条：自检集必须同时包含会通过和不会通过的用例。

    如果哪天它变成全通过，说明评估器已经不会判错了。
    """
    from examples.eval_demo import run_offline

    report = run_offline()
    assert report.total >= 4
    assert report.passed >= 1, "至少要有一条能过，否则说明用例编错了"
    assert len(report.failures) >= 1, "至少要有一条判失败，否则评估器形同虚设"
    assert report.pass_rate < 1.0
