"""Fan-out 编排的离线演示：多专家并行时怎么不浪费、不打架。

零 API Key、零网络，直接 `python examples/fanout_demo.py` 就能跑完。

演示四件事：
  1. 拆解去重   —— 两个专家做重叠的事，等于两份钱买同一份结果
  2. 并行派发   —— 并发上限 + 整轮墙钟上限；一个专家卡住不能拖垮整轮
  3. 汇总消解   —— 专家互不通信，只有主 agent 能发现「A 说改 X、B 说删 X」
  4. token 预算 —— 为什么「调用次数」衡量不了多专家的成本

这四件事的共同特点：**做错了不报错，只是默默多花钱、多花时间**。
所以每一段都把「浪费了多少 / 省下了多少」量出来，而不是只证明「跑通了」。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import ClassVar

# 允许 `python examples/fanout_demo.py` 直接跑（与 examples/guards_demo.py 同一套约定）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_kit.guards import make_cost_guard
from agent_kit.multi_agent.fanout import (
    TaskResult,
    TaskSpec,
    dedupe_tasks,
    detect_conflicts,
    plan_fanout,
    run_fanout,
)


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


class _Resp:
    """假响应：只带 usage，够成本闸门记账就行。"""

    def __init__(self, prompt: int, completion: int) -> None:
        self.response_metadata = {"token_usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


# ---------------------------------------------------------------------------
# 一、拆解去重
# ---------------------------------------------------------------------------
def demo_dedupe() -> None:
    _banner("1 · 拆解去重 —— 重叠的子任务要合并掉，别派两个人做同一件事")
    tasks = [
        TaskSpec(name="分层审查", goal="检查 UserService 的分层是否合理", inputs="UserService.java"),
        TaskSpec(name="分层复查", goal="检查 UserService 的分层是否合理吗", inputs="UserService.java"),
        TaskSpec(name="索引审查", goal="评估数据库索引设计是否合理", inputs="schema.sql"),
        TaskSpec(name="文案校对", goal="校对帮助中心的说明文案"),
    ]
    kept, merged = dedupe_tasks(tasks)
    plan = plan_fanout(tasks, total_budget=4000)

    print(f"  拆解出 {len(tasks)} 个子任务，总预算 4000 token\n")
    print(plan.to_text())
    print(f"\n  去重后 {len(kept)} 个专家，每人预算 {kept[0].max_prompt_tokens} token")
    print(f"  被合并：{merged or '无'}")
    print("\n  注意两点：")
    print("    · 去重本身**不调模型**——用 Jaccard 相似度，为去重再花一次钱不划算；")
    print("    · 先去重再分预算，否则预算会分给一个马上要被丢弃的重复任务。")


# ---------------------------------------------------------------------------
# 二、并行派发
# ---------------------------------------------------------------------------
def demo_fanout() -> None:
    _banner("2 · 并行派发 —— 整轮墙钟取决于最慢的那个专家")

    tasks = [TaskSpec(name=f"专家{i}", goal=f"审查第 {i} 个模块") for i in range(1, 5)]

    def worker(task: TaskSpec) -> str:
        time.sleep(0.5 if task.name != "专家4" else 4.0)
        return f"{task.name} 的结论"

    started = time.perf_counter()
    results = run_fanout(tasks, worker, max_workers=4, timeout=1.0)
    elapsed = time.perf_counter() - started

    for item in results:
        state = "超时未交回执" if item.timed_out else f"完成（{item.elapsed_sec}s）"
        print(f"  {item.name}：{state}")
    print(f"\n  整轮耗时 {elapsed:.2f}s —— 四个专家各 0.5s，但第 4 个卡了 4s。")
    print("  若串行要 5.5s；没有上限的话整轮会被拖到 4s 以上，现在 1s 就收口。")
    print("\n  注意：**超时不是取消**。Python 线程杀不掉，掉队的专家会在后台跑完，")
    print("  这里能做的只是不再等它、如实标记，让主 agent 决定降级、重派还是跳过。")


# ---------------------------------------------------------------------------
# 三、汇总消解
# ---------------------------------------------------------------------------
def demo_conflicts() -> None:
    _banner("3 · 汇总消解 —— 专家互不通信，只有主 agent 能发现结论打架")
    results = [
        TaskResult(name="架构专家", ok=True,
                   output="我修改了 `service.py` 的分层，把持久化的调用收进 repository。"),
        TaskResult(name="精简专家", ok=True,
                   output="建议删除 `service.py`，它的逻辑可以并进 controller。"),
        TaskResult(name="测试专家", ok=True, output="补充了 `test_service.py` 的用例。"),
        TaskResult(name="安全专家", ok=False, timed_out=True, output=None),
    ]
    conflicts = detect_conflicts(results)
    if not conflicts:
        print("  未发现冲突")
        return
    for conflict in conflicts:
        print(f"  {conflict.to_text()}")
    print("\n  冲突检测用规则而不是模型——消解前这一步不该再花钱；")
    print("  失败 / 超时的回执没有结论可比对，不参与检测（上例的安全专家）。")


# ---------------------------------------------------------------------------
# 四、token 预算
# ---------------------------------------------------------------------------
def demo_cost_guard() -> None:
    _banner("4 · token 预算 —— 为什么「调用次数」衡量不了多专家的成本")
    middleware, meter = make_cost_guard(max_prompt_tokens=100, max_model_calls=99)

    def handler(_request):
        # 一个专家带着 20k 的代码库去问，一次就吃掉 120 个 token 预算
        return _Resp(prompt=120, completion=10)

    class _Req:
        state: ClassVar = {}

    middleware.wrap_model_call(_Req(), handler)
    print(f"  第一次调用后：{meter.as_dict()}")
    print("  只算「1 次」的话离上限 99 次远得很，但 token 预算已经穿了。")

    stopped = middleware.wrap_model_call(_Req(), handler)
    print(f"\n  第二次调用：{stopped.result[0].content}")
    print(f"  账本仍是 {meter.as_dict()} —— 这一笔没有真的发出去。")
    print("\n  账本要随中间件一起留着：日志里只有一句「超预算了」排查不出是哪个专家烧的。")


def main() -> None:
    demo_dedupe()
    demo_fanout()
    demo_conflicts()
    demo_cost_guard()
    print("\n" + "=" * 78)
    print("  演示结束：四项控制均已在 agent_kit/multi_agent/fanout.py 与 guards.py 落地。")
    print("  使用前提——子任务真正正交（A 的输出不改变 B 的输入）；否则请分两阶段由主 agent 中转。")
    print("=" * 78)


if __name__ == "__main__":
    main()
