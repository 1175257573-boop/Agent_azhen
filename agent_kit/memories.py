"""记忆产出管线（对标 Codex 的 `memories` 模块：Phase 1 抽取 + Phase 2 合并）。

和 `memory.py` 的区别要分清：
    memory.py   —— **存储层**：短期 checkpoint / 长期 store，回答「数据放哪」
    memories.py —— **加工层**：把会话流水提炼成可复用的记忆，回答「记住了什么」

两个阶段（照 Codex 的划分）：
    Phase 1 · 抽取（per-thread）：一次会话 → 一条结构化记忆（raw_memory + 摘要 + 别名）
    Phase 2 · 合并（global）    ：多条记忆 → 去重、消解冲突 → 一份 MEMORY.md

**红线：不编造记忆。** 抽取必须真的调用模型；没有可用模型（provider=fake 或缺 Key）时
**明确跳过并说明原因**，绝不用模板生成一段看起来像记忆的文字。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_kit.home import layout
from agent_kit.logging_conf import get_logger

log = get_logger("agent.memories")

_TABLE = "memories"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    thread_id    TEXT PRIMARY KEY,
    raw_memory   TEXT NOT NULL,
    summary      TEXT NOT NULL,
    slug         TEXT,
    generated_at TEXT NOT NULL,
    usage_count  INTEGER NOT NULL DEFAULT 0,
    last_usage   TEXT
);
"""

# 没用过且超过这个天数的记忆，不再参与 Phase 2 的合并（对齐 Codex 的 max_unused_days）
DEFAULT_MAX_UNUSED_DAYS = 30
DEFAULT_TOP_N = 20


class MemoryExtract(BaseModel):
    """Phase 1 要求模型输出的结构。字段少了解析会失败，所以约束写死。"""

    raw_memory: str = Field(description="这次会话里值得长期记住的内容，尽量保留细节")
    rollout_summary: str = Field(description="一句话概括这次会话干了什么")
    rollout_slug: str | None = Field(default=None, description="可选的短别名，便于人读")


@dataclass
class MemoryRecord:
    thread_id: str
    raw_memory: str
    summary: str
    slug: str
    generated_at: str
    usage_count: int = 0
    last_usage: str | None = None


@dataclass
class Report:
    """一次运行的产出说明。**没跑成也要说清为什么**。"""

    phase: str
    succeeded: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (thread_id, 原因)
    failed: list[tuple[str, str]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_text(self) -> str:
        lines = [f"[{self.phase}] 成功 {len(self.succeeded)} / 跳过 {len(self.skipped)} / 失败 {len(self.failed)}"]
        for tid, reason in self.skipped:
            lines.append(f"  跳过 {tid}：{reason}")
        for tid, reason in self.failed:
            lines.append(f"  失败 {tid}：{reason}")
        for path in self.artifacts:
            lines.append(f"  产物 {path}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else layout().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def save(record: MemoryRecord, *, db_path: str | Path | None = None) -> None:
    """写入/更新一条记忆（用 thread_id 做主键，同一会话重复抽取即覆盖）。"""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"""INSERT INTO {_TABLE} (thread_id, raw_memory, summary, slug, generated_at, usage_count, last_usage)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    raw_memory = excluded.raw_memory,
                    summary = excluded.summary,
                    slug = excluded.slug,
                    generated_at = excluded.generated_at""",
            (
                record.thread_id,
                record.raw_memory,
                record.summary,
                record.slug,
                record.generated_at,
                record.usage_count,
                record.last_usage,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def all_records(*, db_path: str | Path | None = None) -> list[MemoryRecord]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM {_TABLE}").fetchall()
        return [
            MemoryRecord(
                thread_id=r["thread_id"],
                raw_memory=r["raw_memory"],
                summary=r["summary"],
                slug=r["slug"] or "",
                generated_at=r["generated_at"],
                usage_count=r["usage_count"],
                last_usage=r["last_usage"],
            )
            for r in rows
        ]
    finally:
        conn.close()


def mark_used(thread_id: str, *, db_path: str | Path | None = None) -> None:
    """记一次使用：Phase 2 靠 usage_count / last_usage 排序与裁剪。"""
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {_TABLE} SET usage_count = usage_count + 1, last_usage = ? WHERE thread_id = ?",
            (_now_iso(), thread_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 1：per-thread 抽取
# ---------------------------------------------------------------------------
def _flatten(records: list[dict[str, Any]], *, max_chars: int = 6000) -> str:
    lines = []
    for item in records:
        name = f" [{item['tool_name']}]" if item.get("tool_name") else ""
        lines.append(f"{item['role']}{name}: {item['content']}")
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def run_phase1(
    *,
    model: Any = None,
    make_model: Callable[[], Any] | None = None,
    threads: list[str] | None = None,
    db_path: str | Path | None = None,
    rollout_db: str | Path | None = None,
) -> Report:
    """把会话流水逐条抽取成结构化记忆。

    Args:
        model: 直接给一个模型；给 None 时用 make_model 构造
        make_model: 构造模型的工厂，便于测试注入假模型
    """
    from agent_kit import rollout as rollout_mod

    report = Report(phase="Phase 1 · 抽取")

    if model is None and make_model is not None:
        model = make_model()
    if model is None:
        model = _default_model()

    if model is None:
        pending = threads or [s["thread_id"] for s in rollout_mod.list_sessions(db_path=rollout_db)]
        for thread_id in pending:
            report.skipped.append((thread_id, "没有可用模型（provider=fake 或未配 Key），跳过抽取"))
        return report

    targets = threads or [s["thread_id"] for s in rollout_mod.list_sessions(db_path=rollout_db)]
    extractor = _structured(model)

    for thread_id in targets:
        records = rollout_mod.load(thread_id, db_path=rollout_db)
        if not records:
            report.skipped.append((thread_id, "该会话没有流水"))
            continue
        prompt = (
            "下面是一段人机对话记录。请提炼出**值得长期记住**的内容：\n"
            "- raw_memory：用户偏好、长期事实、反复出现的约定；没有就写「本次无可记忆内容」\n"
            "- rollout_summary：一句话概括这次会话做了什么\n\n"
            f"{_flatten(records)}"
        )
        try:
            result = extractor.invoke(prompt)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整轮
            report.failed.append((thread_id, f"{type(exc).__name__}: {exc}"))
            continue
        if result is None:
            report.skipped.append((thread_id, "模型未返回结构化结果"))
            continue
        save(
            MemoryRecord(
                thread_id=thread_id,
                raw_memory=result.raw_memory,
                summary=result.rollout_summary,
                slug=result.rollout_slug or "",
                generated_at=_now_iso(),
            ),
            db_path=db_path,
        )
        report.succeeded.append(thread_id)
    return report


def _default_model() -> Any:
    """按当前环境构造模型；**fake provider 返回 None**，由调用方明确跳过而不是编造。"""
    from agent_kit.config import AgentSettings, build_chat_model

    settings = AgentSettings()
    if settings.provider == "fake":
        return None
    try:
        return build_chat_model(settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("构造模型失败，记忆抽取将跳过：%s", exc)
        return None


def _structured(model: Any) -> Any:
    """尽量走结构化输出；模型不支持时退回普通调用 + 手工解析。"""
    try:
        return model.with_structured_output(MemoryExtract)
    except Exception:  # noqa: BLE001 - 不是所有模型都支持
        class _Fallback:
            def invoke(self_inner, prompt: str):
                text = model.invoke(prompt)
                content = getattr(text, "content", str(text))
                return MemoryExtract(
                    raw_memory=content,
                    rollout_summary=content[:120],
                    rollout_slug=None,
                )

        return _Fallback()


# ---------------------------------------------------------------------------
# Phase 2：全局合并
# ---------------------------------------------------------------------------
def select_for_phase2(
    *,
    top_n: int = DEFAULT_TOP_N,
    max_unused_days: int = DEFAULT_MAX_UNUSED_DAYS,
    db_path: str | Path | None = None,
) -> list[MemoryRecord]:
    """按 usage_count → last_usage/generated_at 排序，取前 N 条参与合并。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_unused_days)
    candidates = []
    for record in all_records(db_path=db_path):
        stamp = record.last_usage or record.generated_at
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            when = datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            candidates.append((record.usage_count, when, record))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in candidates[:top_n]]


def sync_artifacts(records: list[MemoryRecord], *, memories_dir: Path | str | None = None) -> list[str]:
    """把选中的记忆同步成文件产物：raw_memories.md + rollout_summaries/<thread>.md。"""
    root = Path(memories_dir) if memories_dir else layout().memories_dir
    root.mkdir(parents=True, exist_ok=True)
    summaries = root / "rollout_summaries"
    summaries.mkdir(parents=True, exist_ok=True)

    written = []
    raw_path = root / "raw_memories.md"
    # 按 thread_id 升序写，避免每次排序变动导致文件内容抖动（对齐 Codex 的做法）
    ordered = sorted(records, key=lambda r: r.thread_id)
    raw_path.write_text(
        "\n\n".join(f"## {r.thread_id}\n\n{r.raw_memory}" for r in ordered) or "（暂无记忆）",
        encoding="utf-8",
    )
    written.append(str(raw_path))

    keep = set()
    for record in ordered:
        target = summaries / f"{record.thread_id}.md"
        target.write_text(f"# {record.thread_id}\n\n{record.summary}\n", encoding="utf-8")
        keep.add(target.name)
        written.append(str(target))

    for stale in summaries.glob("*.md"):
        if stale.name not in keep:
            stale.unlink()
    return written


def _strip_code_fence(content: str) -> str:
    """剥掉模型爱加的 ```markdown ... ``` 外壳（实测 qwen 会加，prompt 约束不住）。"""
    text = content.strip()
    if not text.startswith("```"):
        return content
    lines = text.split("\n")
    if len(lines) >= 2 and lines[-1].strip().startswith("```"):
        # 丢掉首行（可能带语言标记）与末行
        return "\n".join(lines[1:-1]).strip()
    return content


def run_phase2(
    *,
    model: Any = None,
    make_model: Callable[[], Any] | None = None,
    top_n: int = DEFAULT_TOP_N,
    db_path: str | Path | None = None,
    memories_dir: Path | str | None = None,
) -> Report:
    """合并选中的记忆，产出 MEMORY.md。"""
    report = Report(phase="Phase 2 · 合并")

    selected = select_for_phase2(top_n=top_n, db_path=db_path)
    report.artifacts = sync_artifacts(selected, memories_dir=memories_dir)

    if not selected:
        report.skipped.append(("（无）", "没有可选记忆，跳过合并"))
        return report

    if model is None and make_model is not None:
        model = make_model()
    if model is None:
        model = _default_model()

    if model is None:
        report.skipped.append(("（合并）", "没有可用模型，只同步了原始记忆产物，未生成 MEMORY.md"))
        return report

    root = Path(memories_dir) if memories_dir else layout().memories_dir
    raw = (root / "raw_memories.md").read_text(encoding="utf-8")
    prompt = (
        "下面是多条会话记忆的原始记录。请去重、消解冲突，合并成一份简洁的 MEMORY.md，"
        "按主题分组；不确定的地方标注来源会话。\n"
        "**直接输出 Markdown 正文**：不要用 ``` 代码围栏把整篇包起来，"
        "也不要加「以下是合并结果」这类开场白。\n\n"
        f"{raw}"
    )
    try:
        result = model.invoke(prompt)
        content = getattr(result, "content", str(result))
    except Exception as exc:  # noqa: BLE001
        report.failed.append(("（合并）", f"{type(exc).__name__}: {exc}"))
        return report

    # 模型很爱把整篇 Markdown 用 ``` 代码围栏包起来（实测 qwen 就会），
    # 光靠 prompt 约束不住，所以落盘前再兜一层
    content = _strip_code_fence(content)

    target = root / "MEMORY.md"
    target.write_text(content, encoding="utf-8")
    report.artifacts.append(str(target))
    report.succeeded.append("MEMORY.md")
    return report
