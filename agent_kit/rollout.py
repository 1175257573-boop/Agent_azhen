"""会话流水（rollout）：把每轮对话落进 SQLite，可回放、可导出。

对标 Codex 的 `~/.codex/sessions/**/rollout-*.jsonl`。差异要说清楚：
Codex 每会话一个 JSONL 文件，本项目既然已经有一个 SQLite 家目录，就直接存表——
少维护一种文件格式，查询也方便（列会话、按时间排序都是一条 SQL）。
要带走时再 `export()` 成 JSONL，格式与 Codex 一致（一行一条 JSON）。

**数据来源是 checkpointer，不是另记一份**。
另记一份迟早会和真实状态对不上（这正是"两处真相"的经典坑），
所以 `sync_from_checkpoint()` 是**增量同步**：比对已有条数，只补新的。

表的落点与短期记忆同一个库文件（ATLAS_HOME/atlas.db），表名不同。
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

_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    tool_name  TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rollout_thread ON {_TABLE}(thread_id, seq);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


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
) -> int:
    """追加一批消息，返回实际写入条数。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {_TABLE} WHERE thread_id = ?", (thread_id,)).fetchone()
        seq = row["n"] if row else 0
        now = _now_iso()
        written = 0
        for message in messages:
            conn.execute(
                f"INSERT INTO {_TABLE} (thread_id, seq, role, content, tool_name, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    seq,
                    _role_of(message),
                    _text_of(getattr(message, "content", "")),
                    getattr(message, "name", None),
                    now,
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
    return append(thread_id, messages[existing:], db_path=db_path)


def list_sessions(*, limit: int = 20, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """列出最近有流水的会话：thread_id / 条数 / 最后一条时间。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT thread_id,
                       COUNT(*) AS turns,
                       MAX(created_at) AS last_at
                FROM {_TABLE} GROUP BY thread_id
                ORDER BY last_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {"thread_id": r["thread_id"], "turns": r["turns"], "last_at": r["last_at"]} for r in rows
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
