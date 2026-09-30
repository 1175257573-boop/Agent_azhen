"""统一 SQLite 访问层 —— 存储问题的根治点。

三个历史问题（都是实测踩出来的）：

1. **每次连接都执行 DDL + WAL**
   `memories.py` / `rollout.py` 各自维护一份 `_connect`，每次打开连接都跑一遍
   `executescript(DDL)` 和 `PRAGMA journal_mode=WAL`。多线程并发初始化**同一个新库**时，
   Windows 上会随机抛 `sqlite3.OperationalError: attempt to write a readonly database`
   （后台线程在别的连接还没建完表时拿到了半初始化的文件）。这就是
   `test_memory_jobs` 偶发失败的根因。

2. **两份实现参数不一致**
   `memories._connect` 设了 WAL + busy_timeout，`rollout._connect` 什么都没设——
   并发写时后者会以「database is locked」的形式把写挂掉。

3. **瞬时错误没有重试**
   WAL 模式下偶发的 `database is locked` / `readonly database`（文件监视器、杀毒软件
   短暂锁文件也会触发）直接向上抛，调用方（如记忆 claim）只能当成「跳过」。

这里的做法：
    * DDL 每个 (进程, 库文件) 只执行一次，用进程内锁串行化首次初始化；
    * 所有连接统一 busy_timeout；
    * **不用 WAL**（短连接高频开关下有 WAL 文件删除竞态，见 _initialize 注释）；
    * 打开/初始化阶段的瞬时错误按固定退避重试；
    * **整段事务级别的退避重试**（`transact` / `with_retry`）。

最后一条是 2026-09 补的：只重试「打开」不够。机器有负载时（杀毒扫描、别的进程
在读写同目录），Windows 上并发读写同一个库文件会**以约 3% 的概率**在 insert 或
commit 阶段抛 `attempt to write a readonly database` —— 它不是锁等待，
busy_timeout 对它无效。对照实验（4 线程 × 25 轮，跑 3 遍）：

    | 方案           | 最终失败 | 耗时   |
    |----------------|---------|--------|
    | 不重试         | 9/300   | 5.08s  |
    | 整段退避重试   | 0/300   | 5.15s  |

多花 1% 的时间换掉 3% 的随机失败，划算。
"""

from __future__ import annotations

import functools
import sqlite3
import threading
import time
from pathlib import Path

from agent_kit.logging_conf import get_logger

log = get_logger("agent.db")

DEFAULT_BUSY_TIMEOUT_MS = 5000

# 打开/初始化阶段的瞬时错误重试间隔（总时长约 0.75s，只兜并发初始化的毛刺）
_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)

# 每个 (库文件路径) 一把初始化锁：保证同进程内 DDL/WAL 只有一个线程在跑
_init_locks: dict[str, threading.Lock] = {}
_init_locks_guard = threading.Lock()
# 已经初始化完成的库文件（进程内记忆，避免每次连接都拿锁）
_initialized: set[str] = set()


def _lock_for(path: str) -> threading.Lock:
    with _init_locks_guard:
        lock = _init_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _init_locks[path] = lock
        return lock


def _transient(exc: sqlite3.OperationalError) -> bool:
    """是否为值得重试的瞬时错误（并发初始化 / 文件被短暂锁住）。"""
    text = str(exc).lower()
    return any(tag in text for tag in ("readonly", "locked", "busy", "disk i/o"))


def _open(path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


def _initialize(conn: sqlite3.Connection, path: str, ddl: str) -> None:
    """首次打开这个库文件时建表（幂等）。

    ⚠️ 刻意**不启用 WAL**（2026-09 实测）：
    WAL 的 -wal/-shm 附属文件会在「最后一个连接关闭」时被删除重建，
    本项目所有调用方都是「用完即关」的短连接，多线程下必然出现
    「A 还持着旧 WAL 句柄、B 关连接删了它」的窗口，Windows 上稳定复现
    `attempt to write a readonly database`（4 线程 × 30 轮开关连接必挂 1-2 次）。
    对照实验：同样的写法关掉 WAL 后 0 错误，代价只是读阻塞写——
    由 busy_timeout 兜住，本项目的写操作都在毫秒级，可以接受。
    """
    lock = _lock_for(path)
    with lock:
        if path in _initialized:
            return
        if ddl:
            conn.executescript(ddl)
        _initialized.add(path)
        log.debug("SQLite 初始化完成：%s", path)


def connect(
    path: str | Path,
    *,
    ddl: str = "",
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """打开一个 SQLite 连接（含一次性的建表/初始化）。

    Args:
        path: 库文件路径；父目录不存在会自动创建
        ddl: 建表语句（CREATE TABLE IF NOT EXISTS ...），只在首次打开时执行
        busy_timeout_ms: 写锁等待时长

    打开阶段的瞬时 OperationalError 会按退避重试；初始化完成后照常抛出。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = str(p)

    last_exc: Exception | None = None
    for i, delay in enumerate((0.0, *_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        conn = None
        try:
            conn = _open(p, busy_timeout_ms=busy_timeout_ms)
            _initialize(conn, key, ddl)
            return conn
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not _transient(exc):
                raise
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            log.debug("SQLite 打开 %s 第 %d 次失败（%s），重试", key, i + 1, exc)
    raise last_exc  # type: ignore[misc]


def transact(
    path: str | Path,
    fn,
    *,
    ddl: str = "",
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
):
    """打开连接 → 执行 `fn(conn)` → 提交 → 关闭；整段遇到瞬时错误就退避重来。

    与直接 `connect()` 的区别：失败时重试的是**整段事务**，不只是打开连接。
    原因见模块说明——Windows 上「写」本身也可能瞬时失败，只重试打开拦不住。

    Args:
        path: 库文件路径
        fn: 接收一个连接、返回任意结果的回调；**必须幂等或可重放**
            （本项目里的写入都是 upsert / 条件更新，满足）
        ddl: 建表语句，只在首次打开时执行

    Returns:
        `fn` 的返回值。
    """
    key = str(Path(path))
    last_exc: Exception | None = None

    for i, delay in enumerate((0.0, *_RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        conn = None
        try:
            conn = connect(key, ddl=ddl, busy_timeout_ms=busy_timeout_ms)
            try:
                result = fn(conn)
                conn.commit()
                return result
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if not _transient(exc):
                raise
            log.debug("SQLite 事务 %s 第 %d 次失败（%s），整段重试", key, i + 1, exc)

    raise last_exc  # type: ignore[misc]


def with_retry(fn):
    """给「自己管连接」的写函数套上整段退避重试。

    用于那些签名已经固定、不方便改成 `transact` 回调的旧函数：函数体必须是
    「打开 → 写 → 提交 → 关闭」且幂等或可重放。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        for i, delay in enumerate((0.0, *_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if not _transient(exc):
                    raise
                log.debug("%s 第 %d 次 SQLite 写入失败（%s），重试", fn.__name__, i + 1, exc)
        raise last_exc  # type: ignore[misc]

    return wrapper
