"""记忆产出管线（对标 Codex 的 `memories` 模块：Phase 1 抽取 + Phase 2 合并）。

和 `memory.py` 的区别要分清：
    memory.py   —— **存储层**：短期 checkpoint / 长期 store，回答「数据放哪」
    memories.py —— **加工层**：把会话流水提炼成可复用的记忆，回答「记住了什么」

两个阶段（照 Codex 的划分）：
    Phase 1 · 抽取（per-thread）：一次会话 → 一条结构化记忆（raw_memory + 摘要 + 别名）
    Phase 2 · 合并（global）    ：多条记忆 → 去重、消解冲突 → 一份 MEMORY.md

**红线：不编造记忆。** 抽取必须真的调用模型；没有可用模型（provider=fake 或缺 Key）时
**明确跳过并说明原因**，绝不用模板生成一段看起来像记忆的文字。

并发保护（对齐 Codex memories 的 lease/claim）：
    Phase 1 按 thread 抢占 lease，抢不到就跳过——多进程同时跑时同一个会话只会被抽一次。
    lease 有过期时间，进程崩了不会被永久占住；抽取失败走退避重试，超过上限就停在那儿等人介入。
"""

from __future__ import annotations

import hashlib
import os
import socket
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
_LEASE_TABLE = "memory_lease"
# 全局作业锁：Phase 2 的合并在改共享产物，同一时刻只能有一个在动
_LOCK_TABLE = "memory_lock"
PHASE2_LOCK = "phase2"

# Phase 1 的状态机： pending → claimed → done / failed
_STATE_PENDING = "pending"
_STATE_CLAIMED = "claimed"
_STATE_DONE = "done"
_STATE_FAILED = "failed"

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

# Phase 1 的抽取台账：谁占着这条会话、占到什么时候、失败过几次、下次什么时候能重试。
# source_digest 记的是抽取当时流水内容的指纹 —— 内容没变就不用重新抽（省一次模型调用）。
_LEASE_DDL = f"""
CREATE TABLE IF NOT EXISTS {_LEASE_TABLE} (
    thread_id       TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT '{_STATE_PENDING}',
    owner           TEXT,
    lease_until     REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    source_digest   TEXT,
    last_error      TEXT,
    updated_at      TEXT NOT NULL
);
"""

# 没用过且超过这个天数的记忆，不再参与 Phase 2 的合并（对齐 Codex 的 max_unused_days）
DEFAULT_MAX_UNUSED_DAYS = 30
DEFAULT_TOP_N = 20

# 一次抽取默认给 2 分钟独占；超时后别的进程可以接手（防止进程被杀后 lease 永久悬挂）
DEFAULT_LEASE_SECONDS = 120

# Phase 2 的全局锁：同样是 DB 里的带过期 lease，只是没有 per-thread 粒度
_LOCK_DDL = f"""
CREATE TABLE IF NOT EXISTS {_LOCK_TABLE} (
    name        TEXT PRIMARY KEY,
    owner       TEXT,
    lease_until REAL,
    updated_at  TEXT NOT NULL
);
"""

# 合并一次的默认独占时长；跑不完的话锁会过期给别人，但那之前谁也别碰产物
DEFAULT_PHASE2_LOCK_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3
# 失败退避：第 1 次失败等 10 秒、第 2 次 60 秒；第 3 次起不再自动重试，停在 failed 等人介入。
# 长度必须正好是 DEFAULT_MAX_ATTEMPTS - 1（最后一次失败后面没有等待），有单测钉着这条不变量。
_BACKOFF_SECONDS = (10, 60)
# 抢不到锁时最多等多久（SQLite 写锁互斥，超时会抛 OperationalError）
_BUSY_TIMEOUT_MS = 5000


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
class Lease:
    """一条会话的抽取台账。时间列用 epoch 秒而不是 ISO 字符串——

    SQLite 比 ISO 字符串是**逐字符**比的，一旦混进不同时区偏移（+08:00 vs +00:00）
    表示同一时刻的字符串会排出错的先后。转成浮点秒就没有这个坑了。
    """

    thread_id: str
    state: str
    owner: str | None
    lease_until: float | None
    attempts: int
    next_attempt_at: float | None
    source_digest: str | None
    last_error: str | None
    updated_at: str

    @property
    def held(self) -> bool:
        """是否正被某个进程占着且在有效期内。"""
        if self.state != _STATE_CLAIMED or self.lease_until is None:
            return False
        return self.lease_until > _utc_epoch()

    def when(self, value: float | None) -> str:
        return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds") if value else "-"


@dataclass
class Report:
    """一次运行的产出说明。**没跑成也要说清为什么**。"""

    phase: str
    succeeded: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (thread_id, 原因)
    failed: list[tuple[str, str]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # 与成败无关的说明，如「git 不可用」

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
        for note in self.notes:
            lines.append(f"  说明 {note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _utc_epoch() -> float:
    """lease 的时间列一律存 epoch 秒（原因见 Lease 的注释）。"""
    return datetime.now(timezone.utc).timestamp()


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else layout().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL + _LEASE_DDL + _LOCK_DDL)
    # WAL 让读不阻塞写；本机家目录适用（网络盘上不建议开，这里够用）
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def _default_owner() -> str:
    """lease 的持有者标识：主机名 + 进程号，够定位是哪个进程占着。"""
    return f"{socket.gethostname()}:{os.getpid()}"


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
# Phase 1 的并发保护：lease / claim
#
# 为什么需要：Phase 1 是「一条会话抽一次」，多个进程（比如两个终端同时跑、或后台任务与前台
# 命令撞车）同时扫到同一批会话就会重复调模型。重复抽取不只是浪费钱——两次结果不同会让
# MEMORY.md 在两版记忆之间反复横跳。
#
# 做法照 Codex：给每条会话一把 lease，抢到才干活。
#
# 互斥到底来自哪里，这里要说准（本机做过对照实验：8 线程抢同一条会话，跑 20 轮）：
#   * **真正的闸门是那条带条件的 UPDATE**。`UPDATE ... WHERE lease_until IS NULL OR
#     lease_until < ?` 是**一条**语句，SQLite 执行单条 UPDATE 时持写锁且不可分割，
#     所以并发下必然只有一条 UPDATE 能匹配到行（实验里两种写法都是每轮恰好 1 个赢家）。
#   * BEGIN IMMEDIATE 的作用是把「补台账行」和「改占有状态」合成一个单元，并且
#     **先拿写锁再干活**。不用它时迟到的连接要走「延迟事务中途升级写锁」这条路，
#     失败形态会更难看也难诊断——它不是互斥的来源，但省掉了无谓的重试。
#   * lease 带过期时间，持有者进程被杀后别人能接手，不会永久悬挂。
#   * 失败走退避，超过上限就停在 failed 等人介入，而不是无限重试烧 Token。
# ---------------------------------------------------------------------------
def _row_to_lease(row: sqlite3.Row) -> Lease:
    return Lease(
        thread_id=row["thread_id"],
        state=row["state"],
        owner=row["owner"],
        lease_until=row["lease_until"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        source_digest=row["source_digest"],
        last_error=row["last_error"],
        updated_at=row["updated_at"],
    )


def lease_state(thread_id: str, *, db_path: str | Path | None = None) -> Lease | None:
    """查一条会话的抽取台账；没记录返回 None。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(f"SELECT * FROM {_LEASE_TABLE} WHERE thread_id = ?", (thread_id,)).fetchone()
        return _row_to_lease(row) if row else None
    finally:
        conn.close()


def list_leases(*, db_path: str | Path | None = None) -> list[Lease]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM {_LEASE_TABLE} ORDER BY thread_id").fetchall()
        return [_row_to_lease(r) for r in rows]
    finally:
        conn.close()


def _backoff_seconds(attempts: int) -> float | None:
    """第 n 次失败后要等多久；达到上限返回 None（不再自动重试）。"""
    if attempts >= DEFAULT_MAX_ATTEMPTS:
        return None
    return float(_BACKOFF_SECONDS[min(attempts, len(_BACKOFF_SECONDS)) - 1])


def claim(
    thread_id: str,
    *,
    owner: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    force: bool = False,
    unless_digest: str | None = None,
    db_path: str | Path | None = None,
) -> Lease | None:
    """抢占一条会话的抽取权。抢到返回台账，抢不到返回 None。

    Args:
        owner: 持有者标识，默认「主机名:进程号」。释放时按 owner 校验，避免误放别人的锁
        lease_seconds: 独占时长，超时后别人可以接手
        force: 无视现有状态强抢（命令行 `--force` 用，正常流程不用）
        unless_digest: 若台账显示「已完成且内容就是这个指纹」则放弃抢占——

            这是「流水没变就不重复抽」的**原子版本**。放在事务外先查再抢会漏出一个窗口：
            两个 worker 同时查到「内容变了」→ 都去抢 → 后到的那个把前一个的成果覆盖掉。
            作为 UPDATE 的一个条件，判断和抢占就是一步，中间插不进别人。
    """
    who = owner or _default_owner()
    now = _utc_epoch()
    until = now + lease_seconds

    conn = _connect(db_path)
    try:
        conn.isolation_level = None   # 关掉隐式事务，自己控制 BEGIN IMMEDIATE
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT OR IGNORE INTO {_LEASE_TABLE} (thread_id, state, attempts, updated_at)"
                " VALUES (?, ?, 0, ?)",
                (thread_id, _STATE_PENDING, _now_iso()),
            )
            if force:
                conn.execute(
                    f"UPDATE {_LEASE_TABLE} SET state=?, owner=?, lease_until=?, updated_at=?"
                    " WHERE thread_id=?",
                    (_STATE_CLAIMED, who, until, _now_iso(), thread_id),
                )
            else:
                cursor = conn.execute(
                    f"""UPDATE {_LEASE_TABLE}
                           SET state=?, owner=?, lease_until=?, updated_at=?
                         WHERE thread_id=?
                           AND (lease_until IS NULL OR lease_until < ?)   -- 锁没被人占 / 持锁者已超时
                           AND (state != ? OR (next_attempt_at IS NOT NULL AND next_attempt_at < ?))
                           AND (? IS NULL OR state != ? OR source_digest IS NULL OR source_digest <> ?)""",
                    (
                        _STATE_CLAIMED, who, until, _now_iso(), thread_id,
                        now,                                    # lease_until 过期判断
                        _STATE_FAILED, now,                     # 失败退避是否已到期
                        unless_digest, _STATE_DONE, unless_digest,  # 内容是否已变（CAS）
                    ),
                )
                if cursor.rowcount == 0:
                    conn.execute("ROLLBACK")
                    return None
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:  # 拿不到写锁（并发激烈）
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # 事务可能已经回滚了，吞掉
                pass
            log.debug("claim %s 失败：%s", thread_id, exc)
            return None
        return lease_state(thread_id, db_path=db_path)
    finally:
        conn.close()


def release(thread_id: str, owner: str, *, db_path: str | Path | None = None) -> bool:
    """主动归还 lease（还没干完就退出时调用）。只放自己持有的锁。"""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            f"UPDATE {_LEASE_TABLE} SET state=?, owner=NULL, lease_until=NULL, updated_at=?"
            " WHERE thread_id=? AND owner=? AND state=?",
            (_STATE_PENDING, _now_iso(), thread_id, owner, _STATE_CLAIMED),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def mark_done(
    thread_id: str, owner: str, source_digest: str, *, db_path: str | Path | None = None
) -> None:
    """抽取成功：**先写数据（save）再调这里**。

    反过来会把状态说成完成而数据没落库，之后流水没变就被跳过了，记忆永久丢失。
    """
    conn = _connect(db_path)
    try:
        conn.execute(
            f"UPDATE {_LEASE_TABLE} SET state=?, owner=NULL, lease_until=NULL,"
            " attempts=0, next_attempt_at=NULL, source_digest=?, last_error=NULL, updated_at=?"
            " WHERE thread_id=? AND owner=?",
            (_STATE_DONE, source_digest, _now_iso(), thread_id, owner),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(
    thread_id: str, owner: str, error: str, *, db_path: str | Path | None = None
) -> int:
    """抽取失败：累加次数并按退避 schedule 安排下次可重试时间。返回当前失败次数。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT attempts FROM {_LEASE_TABLE} WHERE thread_id=? AND owner=?",
            (thread_id, owner),
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        wait = _backoff_seconds(attempts)
        conn.execute(
            f"UPDATE {_LEASE_TABLE} SET state=?, owner=NULL, lease_until=NULL,"
            " attempts=?, next_attempt_at=?, last_error=?, updated_at=?"
            " WHERE thread_id=? AND owner=?",
            (
                _STATE_FAILED,
                attempts,
                _utc_epoch() + wait if wait is not None else None,
                error[:500],
                _now_iso(),
                thread_id,
                owner,
            ),
        )
        conn.commit()
        return attempts
    finally:
        conn.close()


def reset_lease(thread_id: str, *, db_path: str | Path | None = None) -> bool:
    """清空一条会话的抽取台账（重跑整条会话用）。返回是否真删了。"""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(f"DELETE FROM {_LEASE_TABLE} WHERE thread_id=?", (thread_id,))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _rollout_digest(records: list[dict[str, Any]]) -> str:
    """流水内容指纹：内容没变就不用重新抽取。"""
    digest = hashlib.sha256()
    for item in records:
        digest.update(f"{item['role']}|{item.get('tool_name') or ''}|{item['content']}\n".encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 全局作业锁（Phase 2 用）
#
# Phase 1 的 lease 是 per-thread 的，多个 worker 各干各的互不干扰；
# Phase 2 反过来 —— 它在改 `raw_memories.md` / `MEMORY.md` 这些**共享产物**，
# 两个进程同时合并会互相覆盖，还可能各写一版 MEMORY.md。
# Codex 的做法是"改产物前先抢一把全局锁"，这里照做，机制与 Phase 1 同源（DB 里的带过期 lease）。
# ---------------------------------------------------------------------------
def claim_global_lock(
    name: str = PHASE2_LOCK,
    *,
    owner: str | None = None,
    lease_seconds: int = DEFAULT_PHASE2_LOCK_SECONDS,
    db_path: str | Path | None = None,
) -> bool:
    """抢全局作业锁。抢到 True；已被别人占着（或拿不到写锁）返回 False。"""
    who = owner or _default_owner()
    now = _utc_epoch()
    conn = _connect(db_path)
    try:
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT OR IGNORE INTO {_LOCK_TABLE} (name, owner, lease_until, updated_at)"
                " VALUES (?, NULL, NULL, ?)",
                (name, _now_iso()),
            )
            cursor = conn.execute(
                f"UPDATE {_LOCK_TABLE} SET owner=?, lease_until=?, updated_at=?"
                " WHERE name=? AND (lease_until IS NULL OR lease_until < ?)",
                (who, now + lease_seconds, _now_iso(), name, now),
            )
            won = cursor.rowcount == 1
            conn.execute("COMMIT" if won else "ROLLBACK")
            return won
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            log.debug("抢不到全局锁 %s：%s", name, exc)
            return False
    finally:
        conn.close()


def release_global_lock(
    name: str, owner: str, *, db_path: str | Path | None = None
) -> bool:
    """归还全局作业锁。只放自己持有的。"""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            f"UPDATE {_LOCK_TABLE} SET owner=NULL, lease_until=NULL, updated_at=?"
            " WHERE name=? AND owner=?",
            (_now_iso(), name, owner),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def lock_owner(name: str = PHASE2_LOCK, *, db_path: str | Path | None = None) -> str | None:
    """看锁现在归谁（锁空闲或已过期返回 None）。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"SELECT owner, lease_until FROM {_LOCK_TABLE} WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            return None
        until = row["lease_until"]
        if until is None or until < _utc_epoch():
            return None
        return row["owner"]
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
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    force: bool = False,
) -> Report:
    """把会话流水逐条抽取成结构化记忆。

    Args:
        model: 直接给一个模型；给 None 时用 make_model 构造
        make_model: 构造模型的工厂，便于测试注入假模型
        lease_seconds: 每条会话的独占时长
        force: 忽略已经抽取过的事实，强制重抽（正常流程不用）
    """
    from agent_kit import rollout as rollout_mod

    report = Report(phase="Phase 1 · 抽取")

    if model is None and make_model is not None:
        model = make_model()
    if model is None:
        model = _default_model()

    targets = threads or [s["thread_id"] for s in rollout_mod.list_sessions(db_path=rollout_db)]

    if model is None:
        for thread_id in targets:
            report.skipped.append((thread_id, "没有可用模型（provider=fake 或未配 Key），跳过抽取"))
        return report

    owner = _default_owner()
    extractor = _structured(model)

    for thread_id in targets:
        records = rollout_mod.load(thread_id, db_path=rollout_db)
        if not records:
            report.skipped.append((thread_id, "该会话没有流水"))
            continue

        digest = _rollout_digest(records)
        lease = claim(
            thread_id,
            owner=owner,
            lease_seconds=lease_seconds,
            force=force,
            unless_digest=None if force else digest,
            db_path=db_path,
        )
        if lease is None:
            holder = lease_state(thread_id, db_path=db_path)
            report.skipped.append((thread_id, _busy_reason(holder, digest)))
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
            mark_failed(thread_id, owner, f"{type(exc).__name__}: {exc}", db_path=db_path)
            report.failed.append((thread_id, f"{type(exc).__name__}: {exc}"))
            continue
        if result is None:
            # 「模型没给结果」不算失败：不进退避，把锁放回 pending，下次还可以再抽
            release(thread_id, owner, db_path=db_path)
            report.skipped.append((thread_id, "模型未返回结构化结果"))
            continue
        if not result.raw_memory.strip() or result.raw_memory.strip() in _EMPTY_MEMORY_MARKERS:
            # Codex 管这叫 succeeded_no_output：跑完了，只是这次会话确实没东西值得记。
            # **不能拿模板编一条出来**，也不算失败（不该进退避）
            release(thread_id, owner, db_path=db_path)
            report.skipped.append((thread_id, "本次会话没有可记忆内容"))
            continue
        save(
            MemoryRecord(
                thread_id=thread_id,
                raw_memory=_redact(result.raw_memory),
                summary=_redact(result.rollout_summary),
                slug=result.rollout_slug or "",
                generated_at=_now_iso(),
            ),
            db_path=db_path,
        )
        # 顺序不能反：数据先落库，再标完成（理由见 mark_done 的注释）
        mark_done(thread_id, owner, digest, db_path=db_path)
        report.succeeded.append(thread_id)
    return report


# 模型在「没什么可记」时的常见说法。注意这是**识别为空结果**用的白名单，
# 不是拿来生成内容的模板——匹配上就跳过，绝不反过来套用。
_EMPTY_MEMORY_MARKERS = frozenset({"本次无可记忆内容", "无", "无可记忆内容", "（无）", "N/A", "none"})


def _redact(text: str) -> str:
    """记忆要长期留存还会进 git，落盘前把像密钥的片段抹掉。"""
    try:
        from agent_kit.memory_jobs import redact_secrets

        return redact_secrets(text)
    except Exception:  # noqa: BLE001 - 脱敏失败不能让整轮抽取挂掉
        return text


def _busy_reason(holder: Lease | None, digest: str | None = None) -> str:
    """抢不到 lease 时说清是哪种情况：内容没变 / 别人在抽 / 还在退避 / 数据库忙。"""
    if holder is None:
        return "无法获取抽取锁（数据库忙），交给下次运行"
    if digest and holder.state == _STATE_DONE and holder.source_digest == digest:
        return "已有最新记忆（流水未变），跳过抽取"
    if holder.state == _STATE_CLAIMED and holder.held:
        return f"正被 {holder.owner} 抽取中，交给下次运行"
    if holder.state == _STATE_FAILED:
        return f"此前失败 {holder.attempts} 次，等待退避结束"
    return f"当前状态 {holder.state}，跳过"


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
    """尽量走结构化输出；拿不到结果时退回普通调用 + 手工解析。

    为什么要有降级路径（实测 qwen-plus，同一份输入跑了若干次）：

    | 调用方式 | 结果 |
    |---|---|
    | `with_structured_output` | 三次里两次抛 `LengthFinishReasonError`（跑到 token 上限还没生成完） |
    | 裸 `invoke` | 稳定返回，53 tokens 就停 |

    而且把 `max_tokens` 从 2048 提到 4096 **没用**——它照样能把 4096 跑满。
    也就是说这是 function-calling 模式下偶发的「跑飞」，不是配额不够。
    裸调用在同样输入下始终正常，所以降级是有效且必要的。

    降级只对**解析类错误**生效：鉴权失败、网络错误这类必须照原样抛出去，
    被降级悄悄吃掉就会变成「看起来成功了但其实是空记忆」。
    """
    try:
        primary = model.with_structured_output(MemoryExtract)
    except Exception:  # noqa: BLE001 - 不是所有模型都支持结构化输出
        primary = None

    class _Adapter:
        """把「结构化优先、失败降级」包成一个稳定的 invoke。"""

        def invoke(self_inner, prompt: str) -> MemoryExtract | None:
            if primary is not None:
                try:
                    return primary.invoke(prompt)
                except Exception as exc:
                    if not _is_parse_failure(exc):
                        raise
                    log.warning("结构化抽取失败（%s），降级为普通调用再试一次", type(exc).__name__)
            reply = model.invoke(prompt)
            return _parse_markdown_fields(getattr(reply, "content", str(reply)))

    return _Adapter()


# 只有这三类算「模型给的东西没法用」，可以换条路重试。
# 其余（鉴权失败、超时、 quota 不足……）一律原样抛出，不许静默吞掉。
_PARSE_FAILURE_TAGS = ("LengthFinishReason", "OutputParser", "Validation")


def _is_parse_failure(exc: Exception) -> bool:
    name = type(exc).__name__
    return any(tag in name for tag in _PARSE_FAILURE_TAGS)


def _parse_markdown_fields(text: str) -> MemoryExtract | None:
    """从「- raw_memory：xxx」这种答复里把字段抠出来（降级路径专用）。

    模型不保证给全字段；一个字段都没有就返回 None（交给上层当跳过处理，不编造）。
    """
    if not text or not text.strip():
        return None
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*•").strip()
        for field_name in ("raw_memory", "rollout_summary", "rollout_slug"):
            if not stripped.startswith(field_name):
                continue
            value = stripped[len(field_name):].lstrip("：:").strip()
            if value:
                found.setdefault(field_name, value)
    if not found:
        # 连一个标签都没有：可能是模型直接写了一段话，整段当作 raw_memory
        return MemoryExtract(raw_memory=text.strip(), rollout_summary=text.strip()[:120], rollout_slug=None)
    return MemoryExtract(
        raw_memory=found.get("raw_memory", ""),
        rollout_summary=found.get("rollout_summary", "") or (found.get("raw_memory", "")[:120]),
        rollout_slug=found.get("rollout_slug"),
    )


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
    use_git: bool = True,
    use_lock: bool = True,
) -> Report:
    """合并选中的记忆，产出 MEMORY.md。

    流程照 Codex 的 workspace diff：抢全局锁 → 基线快照 → 同步产物 → diff →
    有变更才调模型 → 落快照并 reset 基线 → 还锁。
    `use_git=False` 退回「每次全量重写」，`use_lock=False` 关闭全局锁（单进程时可省这一步）。
    """
    from agent_kit import memory_git as git

    report = Report(phase="Phase 2 · 合并")
    root = Path(memories_dir) if memories_dir else layout().memories_dir

    # ---- 全局锁：改的是共享产物，同一时刻只准一个进程在动
    owner = _default_owner()
    locked = claim_global_lock(PHASE2_LOCK, owner=owner, db_path=db_path) if use_lock else True
    if not locked:
        holder = lock_owner(PHASE2_LOCK, db_path=db_path)
        report.skipped.append(("（合并）", f"另一个进程（{holder or '未知'}）正在合并，本次跳过"))
        return report

    try:
        return _run_phase2_locked(
            report,
            root=root,
            model=model,
            make_model=make_model,
            top_n=top_n,
            db_path=db_path,
            use_git=use_git,
            git=git,
        )
    finally:
        if use_lock:
            release_global_lock(PHASE2_LOCK, owner, db_path=db_path)


def _run_phase2_locked(
    report: Report,
    *,
    root: Path,
    model: Any,
    make_model: Callable[[], Any] | None,
    top_n: int,
    db_path: str | Path | None,
    use_git: bool,
    git: Any,
) -> Report:
    git_ready = False
    if use_git:
        git_ready, reason = git.ensure_repo(root)
        if git_ready:
            # 待收录数量写进提交说明：回看历史时能分清哪次是新产物、哪次只是初始化
            pending = git.changes(root)
            git.snapshot(root, f"baseline: 合并前快照（{len(pending)} 项待收录）")
        else:
            report.notes.append(f"{reason}，改为全量重写")

    selected = select_for_phase2(top_n=top_n, db_path=db_path)
    report.artifacts = sync_artifacts(selected, memories_dir=root)

    # 产物和基线一字不差 → 没必要再调一次模型重写 MEMORY.md
    if git_ready and not git.changes(root):
        report.skipped.append(("（合并）", "记忆产物相对基线无变化，跳过合并"))
        return report

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

    raw = (root / "raw_memories.md").read_text(encoding="utf-8")
    change_list = git.changes(root) if git_ready else []
    # Codex 把 diff 落成文件交给 consolidation agent，而不是只在 prompt 里提一句文件名；
    # 这样模型能看到**新增/删除的具体内容**，而不只是「哪些文件动了」
    diff_path = git.write_diff_artifact(root) if git_ready else None
    diff_hint = ""
    if diff_path:
        diff_hint = (
            "\n\n本次相对上次合并的产物变更见 "
            f"{Path(diff_path).name}（重点处理这些变化，其余保持一致）：\n"
            f"{git.describe(change_list)}"
        )
    elif git_ready:
        diff_hint = (
            "\n\n本次相对上次合并，产物变动如下（重点处理这些变化，其余保持一致）：\n"
            f"{git.describe(change_list)}"
        )
    prompt = (
        "下面是多条会话记忆的原始记录。请去重、消解冲突，合并成一份简洁的 MEMORY.md，"
        "按主题分组；不确定的地方标注来源会话。\n"
        "**直接输出 Markdown 正文**：不要用 ``` 代码围栏把整篇包起来，"
        "也不要加「以下是合并结果」这类开场白。\n\n"
        f"{raw}{diff_hint}"
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

    if git_ready:
        # diff 是给模型看的临时产物，落基线前必须删掉：
        # 否则这次删除掉的内容会被留在产物文件里、也留在 git 对象里（Codex 明确这么做）
        if diff_path:
            Path(diff_path).unlink(missing_ok=True)
        git.snapshot(root, f"consolidate: 本次 {len(change_list)} 个文件变更")
    return report
