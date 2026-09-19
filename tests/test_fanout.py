"""Fan-out 编排与成本闸门的测试。

编排层的 bug 有个特点：**它不会报错，只会默默多花钱或多花时间**。
所以这里的用例都在量「有没有浪费」而不是「跑没跑通」。
"""

from __future__ import annotations

import time
from typing import ClassVar

import pytest

from agent_kit.guards import CostMeter, _extract_usage, make_cost_guard
from agent_kit.multi_agent.fanout import (
    FanoutPlan,
    TaskResult,
    TaskSpec,
    dedupe_tasks,
    detect_conflicts,
    plan_fanout,
    run_fanout,
)


# ---------------------------------------------------------------------------
# 成本闸门
# ---------------------------------------------------------------------------
def test_cost_meter_accumulates_usage():
    meter = CostMeter()
    meter.add(_FakeResponse(usage={"prompt_tokens": 100, "completion_tokens": 20}))
    meter.add(_FakeResponse(usage={"prompt_tokens": 50, "completion_tokens": 5}))
    assert meter.calls == 2
    assert meter.prompt_tokens == 150
    assert meter.completion_tokens == 25
    assert meter.total_tokens == 175


def test_cost_meter_calls_without_usage():
    """拿不到 usage 时也至少要把「调了几次」记上，不能什么都不记。"""
    meter = CostMeter()
    meter.add(_FakeResponse(usage=None))
    assert meter.calls == 1
    assert meter.total_tokens == 0


class _FakeResponse:
    def __init__(self, usage: dict | None) -> None:
        self.response_metadata = {"token_usage": usage} if usage else {}


def test_extract_usage_prefers_usage_metadata():
    class _Resp:
        usage_metadata: ClassVar = {"input_tokens": 7, "output_tokens": 3}

    assert _extract_usage(_Resp()) == (7, 3)


def test_cost_guard_stops_when_prompt_budget_exhausted():
    """核心用例：只看调用次数会漏掉「一次调用吃掉整个预算」的情况。

    一次调用烧掉 120 个输入 token，只算「1 次」的话离上限 99 次远得很，
    但 token 预算 100 已经穿了 —— 下一次调用必须被拦住。
    """
    middleware, meter = make_cost_guard(max_prompt_tokens=100, max_model_calls=99)

    def handler(_request):
        return _FakeResponse(usage={"prompt_tokens": 120, "completion_tokens": 10})

    class _Req:
        state: ClassVar = {}

    first = middleware.wrap_model_call(_Req(), handler)   # type: ignore[attr-defined]
    assert meter.prompt_tokens == 120
    assert first is not None

    second = middleware.wrap_model_call(_Req(), handler)  # type: ignore[attr-defined]
    text = second.result[0].content
    assert "已停止" in text and "输入 token" in text
    assert meter.calls == 1      # 这一笔没有真的发出去


def test_cost_guard_reset_for_next_round():
    _, meter = make_cost_guard(max_model_calls=5)
    meter.add(_FakeResponse(usage={"prompt_tokens": 10, "completion_tokens": 1}))
    meter.reset()
    assert meter.as_dict()["calls"] == 0


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def test_overlapping_tasks_are_merged():
    tasks = [
        TaskSpec(name="a", goal="检查 UserService 的分层是否合理", inputs="UserService.java"),
        TaskSpec(name="b", goal="检查 UserService 的分层是否合理吗", inputs="UserService.java"),
        TaskSpec(name="c", goal="评估数据库索引设计", inputs="schema.sql"),
    ]
    kept, merged = dedupe_tasks(tasks)
    assert len(kept) == 2
    assert ("a", "b") in merged


def test_distinct_tasks_are_kept():
    tasks = [
        TaskSpec(name="a", goal="重构支付模块"),
        TaskSpec(name="b", goal="补充登录接口的单元测试"),
    ]
    kept, merged = dedupe_tasks(tasks)
    assert len(kept) == 2 and not merged


def test_short_goals_are_not_merged_for_lack_of_evidence():
    """证据不足时不合并。

    「任务0」「任务1」这类极短 goal 只抽得出 1 个共同特征词，
    Jaccard 会算出 1.0 —— 但那说明不了它们是同一件事。
    宁可多派一个专家，也不能把两件不同的事并成一件。
    """
    tasks = [TaskSpec(name=f"t{i}", goal=f"任务{i}") for i in range(4)]
    kept, merged = dedupe_tasks(tasks)
    assert len(kept) == 4
    assert merged == []


# ---------------------------------------------------------------------------
# 并行派发
# ---------------------------------------------------------------------------
def test_fanout_runs_in_parallel():
    """四个各睡 0.4 秒的专家，并发跑应该接近 0.4 秒而不是 1.6 秒。"""
    tasks = [TaskSpec(name=f"t{i}", goal=f"任务{i}") for i in range(4)]

    def slow(_task):
        time.sleep(0.4)
        return "done"

    started = time.perf_counter()
    results = run_fanout(tasks, slow, max_workers=4, timeout=10)
    elapsed = time.perf_counter() - started

    assert len(results) == 4
    assert all(item.ok for item in results)
    assert elapsed < 1.0, f"并发没生效，耗时 {elapsed:.2f}s"


def test_fanout_timeout_does_not_block_the_round():
    """一个专家卡住不能拖垮整轮 —— fan-out 的墙钟取决于最慢的那个。"""
    tasks = [TaskSpec(name="fast", goal="快的"), TaskSpec(name="slow", goal="慢的")]

    def worker(task):
        if task.name == "slow":
            time.sleep(5)
        return "ok"

    started = time.perf_counter()
    results = run_fanout(tasks, worker, max_workers=2, timeout=0.5)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, f"被慢专家拖住了，耗时 {elapsed:.2f}s"
    slow = next(item for item in results if item.name == "slow")
    assert slow.timed_out is True
    assert slow.ok is False
    fast = next(item for item in results if item.name == "fast")
    assert fast.ok is True


def test_fanout_round_budget_holds_when_serialized():
    """并发上限压到 1 时专家是串行的，但**整轮**仍不能超过 timeout。

    这一条专门盯 `with ThreadPoolExecutor(...)` 那个坑：它退出时会等所有任务跑完，
    超时省下的时间会在 shutdown 里原封不动等回去。
    """
    tasks = [TaskSpec(name=f"t{i}", goal=f"任务{i}") for i in range(4)]

    def worker(_task):
        time.sleep(1.0)
        return "ok"

    started = time.perf_counter()
    results = run_fanout(tasks, worker, max_workers=1, timeout=1.5)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, f"整轮预算没兜住，耗时 {elapsed:.2f}s"
    assert sum(item.ok for item in results) == 1
    assert sum(item.timed_out for item in results) == 3


def test_fanout_worker_exception_is_captured():
    """单个专家崩了不能带崩整轮，得作为回执交回主 agent。"""
    tasks = [TaskSpec(name="ok", goal="好"), TaskSpec(name="boom", goal="炸")]

    def worker(task):
        if task.name == "boom":
            raise RuntimeError("专家内部出错")
        return "fine"

    results = run_fanout(tasks, worker, max_workers=2, timeout=5)
    assert len(results) == 2
    boom = next(item for item in results if item.name == "boom")
    assert boom.ok is False and "RuntimeError" in boom.error


def test_fanout_empty_is_noop():
    assert run_fanout([], lambda _t: None) == []


# ---------------------------------------------------------------------------
# 冲突检测
# ---------------------------------------------------------------------------
def test_resource_conflict_is_detected():
    """A 说改 X、B 说删 X —— 专家互不通信，只有主 agent 能发现。"""
    results = [
        TaskResult(name="专家1", ok=True, output="我修改了 `service.py` 的实现。"),
        TaskResult(name="专家2", ok=True, output="建议删除 `service.py`。"),
    ]
    conflicts = detect_conflicts(results)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "resource"
    assert "service.py" in conflicts[0].detail


def test_no_conflict_when_they_touch_different_files():
    results = [
        TaskResult(name="专家1", ok=True, output="我修改了 `a.py`。"),
        TaskResult(name="专家2", ok=True, output="我修改了 `b.py`。"),
    ]
    assert detect_conflicts(results) == []


def test_failed_results_are_not_inspected():
    """失败/超时的回执没有结论可比对，不该参与冲突检测。"""
    results = [
        TaskResult(name="超时", ok=False, timed_out=True, output=None),
        TaskResult(name="专家", ok=True, output="修改了 `a.py`。"),
    ]
    assert detect_conflicts(results) == []


# ---------------------------------------------------------------------------
# 预算分配
# ---------------------------------------------------------------------------
def test_plan_splits_budget_per_expert():
    """每个专家要有独立上限，共用一个池子会出现「1 号烧光、4 号没得用」。"""
    tasks = [TaskSpec(name=f"t{i}", goal=f"任务{i}") for i in range(4)]
    plan = plan_fanout(tasks, total_budget=4000)
    assert isinstance(plan, FanoutPlan)
    assert len(plan.tasks) == 4
    assert all(task.max_prompt_tokens == 1000 for task in plan.tasks)


def test_plan_dedupes_before_budgeting():
    """先去重再分钱 —— 不然预算会分给一个被丢弃的重复任务。"""
    tasks = [
        TaskSpec(name="a", goal="检查支付模块的错误处理"),
        TaskSpec(name="b", goal="检查支付模块的错误处理呀"),
        TaskSpec(name="c", goal="检查登录模块的错误处理"),
    ]
    plan = plan_fanout(tasks, total_budget=2000)
    assert len(plan.tasks) == 2
    assert plan.merged
    assert all(task.max_prompt_tokens == 1000 for task in plan.tasks)


@pytest.mark.parametrize("empty_plan", [plan_fanout([], total_budget=100)])
def test_empty_plan(empty_plan):
    assert empty_plan.tasks == []
