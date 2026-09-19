"""Fan-out / fan-back 编排：主 agent 拆任务 → 多个专家并行 → 汇总消解。

这一层解决的是「多专家拓扑」特有的四个问题，和 `guards.py` 的分工要分清：
    guards.py  —— 单个 agent 别失控（跑偏、死循环）
    本模块     —— 一群 agent 别浪费、别打架

四件事：

1. `dedupe_tasks`  拆解阶段去重：两个专家做重叠的事 = 两份钱买同一份结果
2. `run_fanout`    并行派发：并发上限 + **整轮墙钟上限**
                   （fan-out 的墙钟 = 最慢的那个专家，不设上限就等着被拖垮）
3. `detect_conflicts` 汇总阶段冲突检测：专家互不通信，看不到彼此结论，
                   只有主 agent 能发现「A 说改 X、B 说删 X」
4. `plan_fanout`   给拆解结果定预算：每个专家分多少 token，而不是四人共用一个池子

为什么强调「无通信」：这个拓扑成立的前提是**子任务真正交**
（互不依赖）。只要专家 A 的输出会改变专家 B 的输入，就不能一次 fan-out，
必须分两阶段由主 agent 中转 —— 否则省下的时间会在冲突消解里加倍还回去。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_kit.logging_conf import get_logger

log = get_logger("agent.fanout")

DEFAULT_MAX_WORKERS = 4
DEFAULT_TASK_TIMEOUT = 120.0
# 去重所需的最小证据量：特征并集低于这个值就不下合并判断，见 `dedupe_tasks`
MIN_EVIDENCE = 4


# ---------------------------------------------------------------------------
# 任务描述
# ---------------------------------------------------------------------------
@dataclass
class TaskSpec:
    """派给一个专家的任务包。

    `shared` 是共享上下文 —— **刻意单独放一个字段**：
    fan-out 拓扑里最大的浪费就是四个专家各读一遍同样的背景，
    把它单拎出来，才能在派发时只发一次（比如落成文件让专家按需读）。
    """

    name: str
    goal: str                    # 这一个专家要达成什么
    inputs: str = ""             # 只有他需要看的输入
    shared: str = ""             # 和其他专家共用的背景（只应发一次）
    acceptance: str = ""         # 验收标准：没有它，专家跑偏后只能返工
    max_prompt_tokens: int = 0   # 这个专家的独立预算；0 表示不单独限制

    def keywords(self) -> set[str]:
        """用于去重的粗特征：中英文都按词切，短词没区分度就丢掉。"""
        text = f"{self.goal} {self.inputs}"
        # 英文单词 + 中文按 2 字滑窗（中文没有空格，用二元文法近似）
        words = set(re.findall(r"[A-Za-z_]{3,}", text.lower()))
        hanzi = re.findall(r"[\u4e00-\u9fff]+", text)
        for chunk in hanzi:
            if len(chunk) < 2:
                continue
            words.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
        return words


def dedupe_tasks(tasks: list[TaskSpec], *, threshold: float = 0.7) -> tuple[list[TaskSpec], list[tuple[str, str]]]:
    """拆完先去重：目标高度重叠的子任务合并掉，别派两个人做同一件事。

    用 Jaccard 相似度（交集/并集）而不是语义向量 —— 拆解结果通常很短，
    关键词重合度已经够用，而且**不需要为此再调一次模型**（去重本身不该花钱）。

    两个保护：
      * 特征为空不合并（没有证据）
      * 并集小于 `MIN_EVIDENCE` 个词也不合并 —— 极短的 goal（比如「任务0」「任务1」）
        各自只能抽出 1~2 个特征，Jaccard 会算出 1.0 而误判成同一件事。
        宁可多派一个专家，也不能因为「证据不足」把两件不同的事并成一件。

    Returns:
        (保留的任务, 被合并掉的 [(保留者, 被合并者)])
    """
    kept: list[tuple[int, TaskSpec]] = []
    merged: list[tuple[str, str]] = []
    features = [task.keywords() for task in tasks]

    for index, task in enumerate(tasks):
        duplicate_of: TaskSpec | None = None
        for _other_index, other in kept:
            left, right = features[index], features[_other_index]
            if not left or not right:
                continue
            union = left | right
            if len(union) < MIN_EVIDENCE:
                continue
            if len(left & right) / len(union) >= threshold:
                duplicate_of = other
                break
        if duplicate_of is None:
            kept.append((index, task))
        else:
            merged.append((duplicate_of.name, task.name))
            log.info("去重：%s 与 %s 目标重叠，丢弃后者", duplicate_of.name, task.name)
    return [task for _index, task in kept], merged


# ---------------------------------------------------------------------------
# 并行派发
# ---------------------------------------------------------------------------
@dataclass
class TaskResult:
    """一个专家的回执。失败也算回执 —— 主 agent 要能据此决定降级还是跳过。"""

    name: str
    ok: bool
    output: Any = None
    error: str = ""
    elapsed_sec: float = 0.0
    timed_out: bool = False


def run_fanout(
    tasks: list[TaskSpec],
    worker: Callable[[TaskSpec], Any],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: float = DEFAULT_TASK_TIMEOUT,
) -> list[TaskResult]:
    """并行派发，带并发上限与**整轮墙钟上限**。

    线程而不是进程：这条路的瓶颈是等模型返回（IO 密集），
    进程还得多付一份启动开销和跨平台的麻烦。

    Args:
        max_workers: 同时最多几个专家在跑
        timeout: 这一批任务的墙上时间上限（从派发开始算）。
                 到点后没交回执的专家一律标记 `timed_out=True`，
                 不算失败也不重试 —— 让主 agent 决定是降级、重派还是跳过

    为什么用裸线程而不是 `ThreadPoolExecutor`：
        `with ThreadPoolExecutor(...)` 退出时会 `shutdown(wait=True)`，
        **会一直等到所有任务跑完**。也就是说即便你对 future 设了超时、
        主流程已经不等了，上下文管理器那一步还是把时间原封不动等回去 ——
        超时保护等于白做。这里自己起守护线程并按全局 deadline join，
        到点就走，不等掉队的。

    注意：**超时不是取消**。Python 线程杀不掉，掉队的专家会在后台继续跑完
    （也因此线程设成 daemon，进程退出时不会挂住）；
    这里能做的是不再等它、如实标记，避免整轮被一个专家拖住。
    """
    if not tasks:
        return []

    slots = threading.BoundedSemaphore(max(1, max_workers))
    done: dict[str, TaskResult] = {}
    lock = threading.Lock()

    def _one(task: TaskSpec) -> None:
        with slots:
            started = time.monotonic()
            try:
                outcome = worker(task)
                result = TaskResult(
                    name=task.name, ok=True, output=outcome,
                    elapsed_sec=round(time.monotonic() - started, 2),
                )
            except Exception as exc:  # noqa: BLE001 - 单个专家挂掉不能带崩整轮
                result = TaskResult(
                    name=task.name, ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_sec=round(time.monotonic() - started, 2),
                )
            with lock:
                done[task.name] = result

    threads = [
        threading.Thread(target=_one, args=(task,), name=f"fanout-{task.name}", daemon=True)
        for task in tasks
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)

    sorted_results: list[TaskResult] = []
    for task in tasks:
        result = done.get(task.name)
        if result is None:
            log.warning("专家 %s 超过 %.0f 秒未完成，不再等待", task.name, timeout)
            result = TaskResult(name=task.name, ok=False, timed_out=True,
                                error=f"超过 {timeout:.0f} 秒未完成", elapsed_sec=timeout)
        sorted_results.append(result)
    return sorted_results


# ---------------------------------------------------------------------------
# 汇总：冲突检测
# ---------------------------------------------------------------------------
@dataclass
class Conflict:
    """两个专家的结论互相打架。"""

    kind: str            # resource / verdict
    left: str
    right: str
    detail: str

    def to_text(self) -> str:
        return f"[{self.kind}] {self.left} 与 {self.right} 冲突：{self.detail}"


# 「我改了这个东西」的常见说法；用规则而不是模型来识别 —— 消解前这一步不该花钱。
#
# 动词和文件名之间要有 `_GAP` 这个缓冲：中文里几乎必然夹着东西
# （「修改了 `service.py`」「更新：README.md」「编辑 -> src/app.py」），
# 只写 `\s*` 会一个都匹配不上。缓冲限 8 个字符且不跨行，
# 免得把「修改了错误处理逻辑，另外建议看看 docs.md」里的文件也算到这个动词头上。
_GAP = r"[^\n]{0,8}?"
_FILE = r"([\w./\-]+\.\w+)"


def _touch(verbs: str) -> re.Pattern:
    return re.compile(rf"(?:{verbs}){_GAP}{_FILE}")


_TOUCH_PATTERNS = (
    _touch("修改|改动|更新|编辑|重写|重构"),
    _touch("删除|移除|去掉"),
    _touch("新建|新增|创建"),
)
_DELETE_WORDS = ("删除", "移除", "去掉")


def detect_conflicts(results: list[TaskResult]) -> list[Conflict]:
    """汇总阶段：找出专家之间互相打架的结论。

    专家互不通信，谁也不知道别人改了什么。这一步**不能省**，
    否则主 agent 只是把结果拼起来，交付出去的东西自相矛盾。

    目前覆盖两类：
      * resource —— 两个专家动了同一个文件，且一个要删、一个要改
      * verdict  —— 对同一个判断给出相反的结论
    """
    conflicts: list[Conflict] = []
    touched: dict[str, list[tuple[str, str]]] = {}

    for result in results:
        if not result.ok or not isinstance(result.output, str):
            continue
        for pattern in _TOUCH_PATTERNS:
            for match in pattern.finditer(result.output):
                target = match.group(1)
                action = "删除" if any(word in match.group(0) for word in _DELETE_WORDS) else "修改"
                touched.setdefault(target, []).append((result.name, action))

    for target, actors in touched.items():
        if len(actors) < 2:
            continue
        actions = {action for _name, action in actors}
        if "删除" in actions and len(actions) > 1:
            names = ", ".join(f"{name}({action})" for name, action in actors)
            conflicts.append(
                Conflict(kind="resource", left=actors[0][0], right=actors[1][0],
                         detail=f"{target} 被多方改动：{names}")
            )

    conflicts.extend(_verdict_conflicts(results))
    return conflicts


_POSITIVE = ("建议采用", "推荐", "通过", "可行", "同意", "保留")
_NEGATIVE = ("不建议", "不推荐", "不通过", "不可行", "否决", "不要", "移除")


def _verdict_conflicts(results: list[TaskResult]) -> list[Conflict]:
    """两个专家对同一件事给出相反结论。只找明显的反义对，不做语义判断。"""
    verdicts: dict[str, list[tuple[str, bool]]] = {}
    for result in results:
        if not result.ok or not isinstance(result.output, str):
            continue
        for line in result.output.splitlines():
            positive = [w for w in _POSITIVE if w in line]
            negative = [w for w in _NEGATIVE if w in line]
            if not positive or not negative:
                if positive:
                    verdicts.setdefault(positive[0], []).append((result.name, True))
                elif negative:
                    verdicts.setdefault(negative[0], []).append((result.name, False))
    found: list[Conflict] = []
    for topic, opinions in verdicts.items():
        yes = [name for name, flag in opinions if flag]
        no = [name for name, flag in opinions if not flag]
        if yes and no:
            found.append(
                Conflict(kind="verdict", left=yes[0], right=no[0],
                         detail=f"对「{topic}」的结论相反：{yes[0]} 倾向支持，{no[0]} 倾向反对")
            )
    return found


# ---------------------------------------------------------------------------
# 预算分配
# ---------------------------------------------------------------------------
@dataclass
class FanoutPlan:
    """一次 fan-out 的计划：派哪些专家、每人多少预算。"""

    tasks: list[TaskSpec] = field(default_factory=list)
    merged: list[tuple[str, str]] = field(default_factory=list)
    total_budget: int = 0

    def to_text(self) -> str:
        lines = [f"派发 {len(self.tasks)} 个专家，预算合计 {self.total_budget} token"]
        for task in self.tasks:
            lines.append(f"  - {task.name}：{task.goal[:50]}（预算 {task.max_prompt_tokens}）")
        for keeper, dropped in self.merged:
            lines.append(f"  - 去重：{dropped} 与 {keeper} 重叠，已丢弃")
        return "\n".join(lines)


def plan_fanout(
    tasks: list[TaskSpec],
    *,
    total_budget: int = 0,
    threshold: float = 0.7,
) -> FanoutPlan:
    """去重 + 按任务权重分配预算。

    预算分配的意义在于**每个人的上限是独立的**：共用一个池子会出现
    「专家 1 烧光了，专家 4 还没开始」—— 那还不如串行。
    """
    kept, merged = dedupe_tasks(tasks, threshold=threshold)
    if total_budget and kept:
        share = max(1, total_budget // len(kept))
        for task in kept:
            if not task.max_prompt_tokens:
                task.max_prompt_tokens = share
    return FanoutPlan(tasks=kept, merged=merged, total_budget=total_budget)
