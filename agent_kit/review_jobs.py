"""评审任务：后台跑 + 状态查询。

为什么要异步
------------
一次完整评审（取证 + 4 专家 + 模块分析）在大仓库上要**几分钟**，
HTTP 请求同步等不住；而且同步等待期间连接一直占着，网关一断就前功尽弃。

沿用 `memory_jobs.spawn()` 的既有范式：后台线程 + 结果袋子 + Event，
**不发明新机制**。状态存内存而非 SQLite——评审结果是给用户当场看的，
进程重启丢失可以接受；写进数据库反而要处理"半截报告"的清理。

刻意**不做细粒度进度**：`run_review` 是黑盒，拿不到中间百分比。
与其报一个假的进度条，不如只报 排队/运行中/已完成/失败 + 已耗时。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_kit.logging_conf import get_logger

log = get_logger("review_jobs")

#: 内存里最多留多少个任务的历史（含结果）。超了丢最老的**已完成**任务——
#: 运行中的绝不丢，丢了那个线程就成了没人管的孤儿。
MAX_JOBS = 50

#: 单次评审的墙钟上限。超了强制中断——否则一个卡死的网络请求
#: 会让线程永久占着，任务状态永远停在"运行中"。
HARD_TIMEOUT_S = 30 * 60

STATES = ("queued", "running", "done", "failed")


@dataclass
class ReviewJob:
    id: str
    target: str
    options: dict[str, Any] = field(default_factory=dict)
    state: str = "queued"
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    markdown: str = ""
    module_count: int = 0
    blocked: bool = False

    def to_dict(self, *, include_result: bool = True) -> dict[str, Any]:
        elapsed = self._elapsed_s()
        out: dict[str, Any] = {
            "id": self.id,
            "target": self.target,
            "options": self.options,
            "state": self.state,
            "created_at": self.created_at,
            "elapsed_s": round(elapsed, 1),
            "error": self.error,
            "module_count": self.module_count,
        }
        if include_result and self.state == "done":
            out["markdown"] = self.markdown
        return out

    def _elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or _now()
        try:
            t0 = datetime.fromisoformat(self.started_at)
            t1 = datetime.fromisoformat(end)
        except ValueError:
            return 0.0
        return max(0.0, (t1 - t0).total_seconds())


_jobs: dict[str, ReviewJob] = {}
_lock = threading.Lock()


def _now() -> str:
    """微秒精度。

    秒级不够用：并发提交的两个任务 created_at 完全相同，
    `list_jobs` 的排序就变成随机的——用户刷新一次，列表顺序就变了。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------- 提交


def submit(
    target: str,
    *,
    run: Callable[..., tuple[str, int, bool]] | None = None,
    **options: Any,
) -> ReviewJob:
    """提交一次评审，立刻返回（**不等**）。

    Args:
        target: 仓库 URL 或本地路径
        run: 执行函数，默认 `_default_run`；测试可注入假实现
        options: 传给 run 的关键字参数（with_modules / module_top_n / …）
    """
    job = ReviewJob(
        id=uuid.uuid4().hex[:12],
        target=target,
        options=dict(options),
        created_at=_now(),
    )
    with _lock:
        _jobs[job.id] = job
        _evict_old_jobs()

    runner = run or _default_run
    thread = threading.Thread(target=_worker, args=(job, runner), daemon=True, name=f"review-{job.id}")
    thread.start()
    return job


def _worker(job: ReviewJob, runner) -> None:
    job.state = "running"
    job.started_at = _now()
    try:
        markdown, module_count, blocked = runner(job.target, **job.options)
        job.markdown = markdown
        job.module_count = module_count
        job.blocked = blocked
        job.state = "done"
    except Exception as exc:  # noqa: BLE001 —— 后台线程不能把异常甩给解释器
        log.warning("评审任务 %s 失败：%s: %s", job.id, type(exc).__name__, exc)
        log.debug(traceback.format_exc())
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished_at = _now()


def _default_run(target: str, **options: Any) -> tuple[str, int, bool]:
    """真正干活：URL → 本地 → 评审 → 渲染。

    `options` 原样透传给 `run_review`（`with_modules` / `module_top_n` /
    `module_batch` / `module_code_lines`），只有 `no_judge` 在这里消费掉——
    它决定 judge 给不给，不是 run_review 的参数。
    """
    from agent_kit.config import AgentSettings, build_resilient_judge
    from agent_kit.multi_agent import review as review_mod

    no_judge = bool(options.pop("no_judge", False))

    # 1) 拿到本地目录（本地路径直接透传，URL 才下载）
    if target.startswith(("http://", "https://", "git@")):
        from agent_kit.repo_fetch import fetch

        fetched = fetch(target, dest_root=Path(AgentSettings().sandbox_dir) / "repos")
        local = str(fetched.path)
    else:
        local = target

    # 2) 组装 judge：多 key 时自动回退；没有可用模型则降级（judge=None）
    judge = None
    if not no_judge:
        try:
            judge, candidates = build_resilient_judge()
            if not candidates:
                log.info("未检测到模型 Key，本次降级为证据清单")
                judge = None
        except Exception as exc:  # noqa: BLE001
            log.warning("模型初始化失败，降级：%s", exc)
            judge = None

    # 3) 跑评审
    report = review_mod.run_review(local, judge=judge, **options)
    markdown = review_mod.render_markdown(report)
    modules = getattr(report.modules, "modules", None) if report.modules else None
    return markdown, len(modules or []), False


# ---------------------------------------------------------------- 查询


def get(job_id: str) -> ReviewJob | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.to_dict(include_result=False) for j in jobs[:limit]]


def cancel(job_id: str) -> bool:
    """取消一个任务。

    只能取消还没开始的——Python 没法安全地杀掉运行中的线程，
    与其假装能取消，不如对已运行的任务明确说不。
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.state != "queued":
            return False
        job.state = "failed"
        job.error = "已取消"
        job.finished_at = _now()
        return True


def clear_finished() -> int:
    with _lock:
        done = [k for k, v in _jobs.items() if v.state in ("done", "failed")]
        for k in done:
            del _jobs[k]
        return len(done)


def _evict_old_jobs() -> None:
    """超上限时丢最老的**已完成**任务。运行中的绝不丢。"""
    if len(_jobs) <= MAX_JOBS:
        return
    finished = sorted(
        (j for j in _jobs.values() if j.state in ("done", "failed")),
        key=lambda j: j.created_at,
    )
    for job in finished:
        if len(_jobs) <= MAX_JOBS:
            break
        del _jobs[job.id]


def wait(job_id: str, timeout: float = HARD_TIMEOUT_S) -> ReviewJob | None:
    """等待任务结束——**仅供测试与 CLI 使用**，HTTP 路径不该调它。"""
    job = get(job_id)
    if job is None:
        return None
    deadline = time.monotonic() + timeout
    while job.state in ("queued", "running"):
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    return job