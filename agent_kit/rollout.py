"""会话流水（rollout）：把每轮对话落进 SQLite，可回放、可导出。

对标 Codex 的 `~/.codex/sessions/**/rollout-*.jsonl`。差异要说清楚：
Codex 每会话一个 JSONL 文件，本项目既然已经有一个 SQLite 家目录，就直接存表——
少维护一种文件格式，查询也方便（列会话、按时间排序都是一条 SQL）。
要带走时再 `export()` 成 JSONL，格式与 Codex 一致（一行一条 JSON）。

**数据来源是 checkpointer，不是另记一份**。
另记一份迟早会和真实状态对不上（这正是"两处真相"的经典坑），
所以 `sync_from_checkpoint()` 是**增量同步**：比对已有条数，只补新的。

表的落点与短期记忆同一个库文件（ATLAS_HOME/atlas.db），表名不同。

`source` 标记这条流水来自哪类会话：**memory Phase 1 只抽交互会话**（cli / chat），
sub-agent 与工具内部会话排除在外——它们反映的是agent内部的调度过程，
抽进用户长期记忆等于把临时的任务分派当成用户偏好记下来（Codex 同样按 session source 过滤）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_kit.home import resolve_db_path

_TABLE = "rollout"

DEFAULT_SOURCE = "cli"

# 会话来源：哪些能进长期记忆，哪些不能
SOURCE_CLI = "cli"              # REPL 交互
SOURCE_CHAT = "chat"            # 单次问答
SOURCE_SUBAGENT = "subagent"    # 子代理会话
SOURCE_TOOL = "tool"            # 工具 / 路由内部会话
INTERACTIVE_SOURCES = (SOURCE_CLI, SOURCE_CHAT)
ALL_SOURCES = INTERACTIVE_SOURCES + (SOURCE_SUBAGENT, SOURCE_TOOL)

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    tool_name  TEXT,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '{DEFAULT_SOURCE}'
);
CREATE INDEX IF NOT EXISTS idx_rollout_thread ON {_TABLE}(thread_id, seq);
"""

# 老库没有 source 列；ALTER 的 DEFAULT 值没法用占位符绑定，所以用模块常量拼进 SQL。
# 这里不存在注入风险：DEFAULT_SOURCE 是本模块里的常量。
_ADD_SOURCE_COLUMN = f"ALTER TABLE {_TABLE} ADD COLUMN source TEXT NOT NULL DEFAULT '{DEFAULT_SOURCE}'"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    _ensure_source_column(conn)
    return conn


def _ensure_source_column(conn: sqlite3.Connection) -> None:
    """幂等迁移：给没有 source 列的老库补上。

    旧版已经落过流水，`CREATE TABLE IF NOT EXISTS` 对它们是空操作，
    所以必须单独加列——漏这一步会在 select 时抛 no such column。
    """
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    if "source" not in columns:
        conn.execute(_ADD_SOURCE_COLUMN)
        conn.commit()


def _text_of(content: Any) -> str:
    """消息内容可能是 str，也可能是 [{"type":"text","text":...}] 这样的块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts)
    return str(content) if content is not None else ""


def _role_of(message: Any) -> str:
    role = getattr(message, "type", None)
    if role:
        return str(role)
    return type(message).__name__.replace("Message", "").lower() or "unknown"


def append(
    thread_id: str,
    messages: Iterable[Any],
    *,
    db_path: str | Path | None = None,
    source: str = DEFAULT_SOURCE,
) -> int:
    """追加一批消息，返回实际写入条数。"""
    origin = source if source in ALL_SOURCES else DEFAULT_SOURCE
    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {_TABLE} WHERE thread_id = ?", (thread_id,)).fetchone()
        seq = row["n"] if row else 0
        now = _now_iso()
        written = 0
        for message in messages:
            conn.execute(
                f"INSERT INTO {_TABLE} (thread_id, seq, role, content, tool_name, created_at, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    seq,
                    _role_of(message),
                    _text_of(getattr(message, "content", "")),
                    getattr(message, "name", None),
                    now,
                    origin,
                ),
            )
            seq += 1
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()


def sync_from_checkpoint(
    graph: Any,
    thread_id: str,
    *,
    config: dict | None = None,
    db_path: str | Path | None = None,
    source: str = DEFAULT_SOURCE,
) -> int:
    """从 checkpointer 增量同步会话流水。**这是推荐的写入方式**。

    返回写入条数；取不到状态（如 router 这类自定义 state 的工作流）时返回 0。
    """
    try:
        state = graph.get_state(config or {"configurable": {"thread_id": thread_id}})
        values = getattr(state, "values", None) or {}
        messages = values.get("messages") or []
    except Exception:  # noqa: BLE001 - 流水落盘不能影响对话本身
        return 0
    if not messages:
        return 0

    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {_TABLE} WHERE thread_id = ?", (thread_id,)).fetchone()
        existing = row["n"] if row else 0
    finally:
        conn.close()

    if existing >= len(messages):
        return 0  # 没有新增（或状态被裁剪过，保守跳过）
    return append(thread_id, messages[existing:], db_path=db_path, source=source)


def list_sessions(
    *,
    limit: int = 20,
    sources: Iterable[str] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """列出最近有流水的会话：thread_id / 条数 / 最后一条时间。

    Args:
        sources: 限定会话来源；None 表示不限。多个来源之间是「或」的关系。
    """
    conn = _connect(db_path)
    try:
        where = ""
        params: list[Any] = []
        if sources is not None:
            wanted = [s for s in sources]
            if not wanted:
                return []       # 空集合：谁都不匹配，别退化成「不限」
            placeholders = ",".join("?" for _ in wanted)
            where = f"WHERE source IN ({placeholders})"
            params.extend(wanted)
        rows = conn.execute(
            f"""SELECT thread_id,
                       MIN(source) AS source,
                       COUNT(*) AS turns,
                       MAX(created_at) AS last_at
                FROM {_TABLE} {where} GROUP BY thread_id
                ORDER BY last_at DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [
            {
                "thread_id": r["thread_id"],
                "source": r["source"],
                "turns": r["turns"],
                "last_at": r["last_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def load(thread_id: str, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """按 seq 顺序读回一个会话的流水。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT seq, role, content, tool_name, created_at FROM {_TABLE}"
            " WHERE thread_id = ? ORDER BY seq",
            (thread_id,),
        ).fetchall()
        return [
            {
                "seq": r["seq"],
                "role": r["role"],
                "content": r["content"],
                "tool_name": r["tool_name"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def export(thread_id: str, path: str | Path, *, db_path: str | Path | None = None) -> int:
    """导出成 JSONL（一行一条，与 Codex 的 rollout 文件同思路）。返回条数。"""
    records = load(thread_id, db_path=db_path)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for item in records:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return len(records)


def clear(thread_id: str, *, db_path: str | Path | None = None) -> int:
    """删掉一个会话的流水，返回删除条数。"""
    conn = _connect(db_path)
    try:
        cur = conn.execute(f"DELETE FROM {_TABLE} WHERE thread_id = ?", (thread_id,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()
