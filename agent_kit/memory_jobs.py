"""记忆 Phase 1 的编排层：把「手动跑一次」变成 Codex 那种**后台任务**。

`memories.run_phase1()` 只负责「一条会话怎么抽」；这一层负责**哪些会话该抽、抽多少、
怎么并行、什么时候跑**——Codex 里称作 "startup claim" 的那一坨规则。

对 Codex 口径（`codex-rs/memories/README.md`）的对应关系：

| Codex 的要求 | 这里的实现 |
|---|---|
| 会话启动后后台异步执行 | `spawn()` 起守护线程 |
| 只看最近 N 天的 rollout（age window） | `Eligibility.max_age_days` |
| 空闲够久才抽，别总结还在进行的会话 | `Eligibility.idle_minutes` |
| 限定 session source，排除 sub-agent | `Eligibility.sources`，默认只收 cli / chat |
| 每次受理有上限（bounded work per startup） | `Eligibility.max_jobs` |
| 并行处理 + 并发上限 | `Eligibility.concurrency` |
| 处理前先 lease/claim，失败进退避 | 复用 `memories.claim()` 的 CAS 抢锁 |
| 抽出来的内容要抹掉密钥 | `redact_secrets()` |

红线不变：**没有可用模型就明确跳过，绝不编造记忆内容**。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_kit import rollout as rollout_mod
from agent_kit.logging_conf import get_logger
from agent_kit.memories import Report

log = get_logger("agent.memory_jobs")


# ---------------------------------------------------------------------------
# 资格门槛
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Eligibility:
    """一次 startup 受理哪些会话。默认值偏保守：宁可少抽，也别把还在聊的会话截成「最终结论」。

    Args:
        max_jobs: 单轮最多受理几条（Codex: startup scan/claim limits）
        max_age_days: 超过这个天数的流水不再回溯抽取
        idle_minutes: 会话空闲够久才抽
        sources: 允许的会话来源，默认排除 sub-agent / 工具会话
        concurrency: 并行抽取上限
    """

    max_jobs: int = 5
    max_age_days: int = 7
    idle_minutes: int = 30
    sources: tuple[str, ...] = rollout_mod.INTERACTIVE_SOURCES
    concurrency: int = 3

    @classmethod
    def from_env(cls) -> Eligibility:
        """环境变量覆盖单个字段；值非法时**忽略该字段**并回落默认，不静默接受半个配置。"""
        changes: dict[str, Any] = {}
        int_fields = {
            "ATLAS_MEMORY_MAX_JOBS": "max_jobs",
            "ATLAS_MEMORY_MAX_AGE_DAYS": "max_age_days",
            "ATLAS_MEMORY_IDLE_MINUTES": "idle_minutes",
            "ATLAS_MEMORY_CONCURRENCY": "concurrency",
        }
        for env_name, field_name in int_fields.items():
            raw = os.environ.get(env_name)
            if not raw:
                continue
            try:
                changes[field_name] = int(raw)
            except ValueError:
                log.warning("%s=%r 不是整数，忽略并沿用默认值", env_name, raw)

        raw_sources = os.environ.get("ATLAS_MEMORY_SOURCES")
        if raw_sources:
            wanted = tuple(part.strip() for part in raw_sources.split(",") if part.strip())
            unknown = [item for item in wanted if item not in rollout_mod.ALL_SOURCES]
            if unknown:
                log.warning("ATLAS_MEMORY_SOURCES 含未知来源 %s，整条忽略", unknown)
            else:
                changes["sources"] = wanted
        return replace(cls(), **changes) if changes else cls()

    def describe(self) -> str:
        return (
            f"最多 {self.max_jobs} 条 / {self.max_age_days} 天内 / 空闲≥{self.idle_minutes} 分钟 / "
            f"来源 {','.join(self.sources)} / 并发 {self.concurrency}"
        )


def _parse_stamp(stamp: str) -> datetime | None:
    """流水时间是本地带偏移的 ISO；解析不出来返回 None（当不满足门槛处理）。"""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def select_eligible(
    *,
    eligibility: Eligibility | None = None,
    now: datetime | None = None,
    rollout_db: str | Path | None = None,
) -> list[str]:
    """按 startup claim 规则挑候选会话，返回 thread_id 列表。

    这里只做**粗筛**（时间窗 / 空闲 / 来源），真正的并发互斥交给 `memories.claim()`
    那条带 CAS 的条件 UPDATE——粗筛多给几个候选不会导致重复抽取，claim 会拦住。
    """
    rules = eligibility or Eligibility.from_env()
    moment = now or datetime.now(timezone.utc).astimezone()

    # scan 上限给 max_jobs 留余量：claim 会再筛掉「别人占着 / 内容没变」的那些
    scans = rollout_mod.list_sessions(
        limit=max(rules.max_jobs * 4, 20),
        sources=rules.sources,
        db_path=rollout_db,
    )

    picked: list[str] = []
    for item in scans:
        last = _parse_stamp(item["last_at"])
        if last is None:
            continue
        idle = moment - last
        if idle > timedelta(days=rules.max_age_days):
            log.debug("跳过 %s：超出 %d 天回溯窗口", item["thread_id"], rules.max_age_days)
            continue
        if idle < timedelta(minutes=rules.idle_minutes):
            log.debug("跳过 %s：还在聊或刚聊完（空闲不足 %d 分钟）", item["thread_id"], rules.idle_minutes)
            continue
        picked.append(item["thread_id"])
        if len(picked) >= rules.max_jobs:
            break
    return picked


# ---------------------------------------------------------------------------
# 密钥脱敏
# ---------------------------------------------------------------------------
# 模型把对话收进记忆时，用户随口粘贴的 Key 会一起进 raw_memory；而记忆是要长期留存、
# 还会进 git 基线仓库的，那就是凭据泄漏。
# 做法：只抹形状极像密钥的模式。宁可漏掉一些，也不要把正常文本改坏。
_SECRET_RULES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), lambda m: "[REDACTED_SK]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), lambda m: "[REDACTED_GHP]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), lambda m: "[REDACTED_SLACK]"),
    (re.compile(r"AKID[A-Za-z0-9]{10,}"), lambda m: "[REDACTED_AKID]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.?[A-Za-z0-9_\-]*"),
     lambda m: "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     lambda m: "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.=]{16,}"), lambda m: "Bearer [REDACTED]"),
    # 口语化的「key 是 xxx」「api_key=xxx」；只抹值，保留字段名，便于事后核对上下文
    (re.compile(r"(?i)\b(api[_\-]?key|secret|token|password)\s*[:=]\s*[\"']?([^\s\"',]{8,})"),
     lambda m: f"{m.group(1)}=[REDACTED]"),
)


def redact_secrets(text: str) -> str:
    """把形状像密钥的片段抹掉。返回新字符串，入参不动。"""
    result = text
    for pattern, replacer in _SECRET_RULES:
        result = pattern.sub(replacer, result)
    return result


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
@dataclass
class PipelineReport:
    """一轮编排的结果。"""

    claimed: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    errors: tuple[str, ...] = ()
    failed: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_text(self) -> str:
        lines = [f"[编排] 受理 {len(self.claimed)} 条 / 失败 {self.failed}"]
        if self.claimed:
            lines.append("  抽出记忆：" + ", ".join(self.claimed))
        for tid, reason in self.skipped:
            lines.append(f"  跳过 {tid}：{reason}")
        for item in self.errors:
            lines.append(f"  错误 {item}")
        return "\n".join(lines)


def run_pipeline(
    *,
    model: Any = None,
    make_model: Callable[[], Any] | None = None,
    eligibility: Eligibility | None = None,
    threads: Iterable[str] | None = None,
    db_path: str | Path | None = None,
    rollout_db: str | Path | None = None,
    memories_dir: str | Path | None = None,
    phase2: bool = True,
) -> PipelineReport:
    """按 startup claim 规则跑一轮 Phase 1（可选接着跑 Phase 2）。

    Args:
        threads: 显式指定会话，绕过资格粗筛（测试或上层显式调度时用）
        phase2: 本轮抽出记忆后是否接着跑合并
    """
    from agent_kit import memories

    rules = eligibility or Eligibility.from_env()
    targets = list(threads) if threads is not None else select_eligible(eligibility=rules, rollout_db=rollout_db)
    if not targets:
        log.debug("没有符合门槛的会话：%s", rules.describe())
        return PipelineReport()

    shared_model = model if model is not None else _build_model(make_model)

    def _job(thread_id: str) -> Report:
        return memories.run_phase1(
            model=shared_model,
            threads=[thread_id],
            db_path=db_path,
            rollout_db=rollout_db,
        )

    succeeded: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed = 0
    try:
        # 线程池而不是进程池：这条路主要是等模型返回（IO 密集），
        # 而且免去跨平台启动进程的一堆差异
        with ThreadPoolExecutor(max_workers=max(rules.concurrency, 1)) as pool:
            for outcome in pool.map(_job, targets):
                succeeded.extend(outcome.succeeded)
                skipped.extend(outcome.skipped)
                failed += len(outcome.failed)
                for tid, reason in outcome.failed:
                    log.warning("会话 %s 抽取失败：%s", tid, reason)
    except Exception as exc:  # noqa: BLE001 - 编排层不能把异常甩到会话里
        return PipelineReport(errors=(f"{type(exc).__name__}: {exc}",))

    report = PipelineReport(claimed=tuple(succeeded), skipped=tuple(skipped), failed=failed)

    if phase2 and succeeded:
        try:
            memories.run_phase2(model=shared_model, db_path=db_path, memories_dir=memories_dir)
        except Exception as exc:  # noqa: BLE001
            report = replace(report, errors=report.errors + (f"Phase 2 失败：{type(exc).__name__}: {exc}",))
    return report


def _build_model(make_model: Callable[[], Any] | None = None) -> Any:
    """构造模型。构造不出来返回 None —— 由 `memories` 明确跳过，而不是编造记忆。"""
    if make_model is not None:
        try:
            return make_model()
        except Exception as exc:  # noqa: BLE001
            log.warning("构造模型失败：%s", exc)
            return None
    try:
        from agent_kit.memories import _default_model as builtin

        return builtin()
    except Exception:  # noqa: BLE001
        return None


def spawn(
    *,
    make_model: Callable[[], Any] | None = None,
    eligibility: Eligibility | None = None,
    db_path: str | Path | None = None,
    rollout_db: str | Path | None = None,
    memories_dir: str | Path | None = None,
    phase2: bool = True,
    daemon: bool = True,
) -> tuple[threading.Thread, dict[str, Any]]:
    """后台起一轮记忆管线（对标 Codex「会话启动后异步跑 Phase 1 再接 Phase 2」）。

    返回线程与一个结果袋子：跑完后 `bag["report"]` 有值，`bag["done"]` 会被 set。
    **本函数不等待** —— 要不要等、等多久由调用方决定。
    """
    bag: dict[str, Any] = {"report": None, "done": threading.Event()}

    def _run() -> None:
        try:
            bag["report"] = run_pipeline(
                make_model=make_model,
                eligibility=eligibility,
                db_path=db_path,
                rollout_db=rollout_db,
                memories_dir=memories_dir,
                phase2=phase2,
            )
        except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常甩给解释器
            log.warning("记忆后台任务失败：%s: %s", type(exc).__name__, exc)
            bag["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            bag["done"].set()

    worker = threading.Thread(target=_run, name="atlas-memory-phase1", daemon=daemon)
    worker.start()
    return worker, bag
