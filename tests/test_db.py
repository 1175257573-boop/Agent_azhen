"""agent_kit.db 统一 SQLite 访问层的测试。

背景：memories / rollout 各自的 _connect 在多线程并发初始化同一个新库时，
Windows 上会随机抛 `attempt to write a readonly database`
（test_memory_jobs 偶发失败的根因）。这里把并发初始化钉死成回归测试。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from agent_kit import db

_DDL = """
CREATE TABLE IF NOT EXISTS demo (id TEXT PRIMARY KEY, value TEXT);
"""


def test_init_once_per_path(tmp_path):
    """DDL 只在首次打开时执行；后续连接不再写。"""
    path = tmp_path / "once.db"
    conn = db.connect(path, ddl=_DDL)
    conn.execute("INSERT INTO demo VALUES ('a', '1')")
    conn.commit()
    conn.close()

    # 二次连接：表还在（DDL 幂等），且 _initialized 已标记
    conn2 = db.connect(path, ddl=_DDL)
    row = conn2.execute("SELECT value FROM demo WHERE id='a'").fetchone()
    assert row["value"] == "1"
    conn2.close()
    assert str(path) in db._initialized


def test_concurrent_first_open_does_not_explode(tmp_path):
    """并发首开同一个新库：不再出现 readonly database / locked（回归用例）。

    走 `db.transact` —— 生产代码就是这么写的。整段事务重试是必须的：
    只重试「打开连接」拦不住写阶段的瞬时失败（Windows 上机器有负载时约 3%），
    见 db.py 模块说明里的对照实验。
    """
    path = tmp_path / "race.db"
    errors: list[str] = []

    def worker(n: int) -> None:
        for i in range(20):
            try:
                db.transact(path, lambda conn: conn.execute(
                    "INSERT INTO demo (id, value) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                    (f"t{n}-{i}", str(i)),
                ), ddl=_DDL)
            except sqlite3.OperationalError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_transact_retries_transient_write_failures(tmp_path, monkeypatch):
    """写阶段抛瞬时错误时，整段事务会重来，最终成功。

    模拟 Windows 上那种「文件被短暂占用」的毛刺：第一次 commit 失败，
    之后正常。只重试打开连接是不够的，这里钉住的是「整段重试」。
    """
    path = tmp_path / "flaky.db"
    state = {"failed": False}
    real_connect = db.connect

    class FlakyOnce:
        """代理连接：第一次 commit 抛瞬时错误，之后正常。

        sqlite3.Connection 是 C 类型、不能打补丁，所以从 connect 这一层包。
        """

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def commit(self):
            if not state["failed"]:
                state["failed"] = True
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return self._real.commit()

    monkeypatch.setattr(db, "connect", lambda *a, **kw: FlakyOnce(real_connect(*a, **kw)))

    db.transact(path, lambda conn: conn.execute(
        "INSERT INTO demo (id, value) VALUES ('a', '1')"), ddl=_DDL)

    assert state["failed"] is True, "没触发预期的瞬时失败，这条测试就失去意义了"

    rows = db.transact(path, lambda conn: conn.execute(
        "SELECT value FROM demo WHERE id='a'").fetchall())
    assert [r["value"] for r in rows] == ["1"], "重试后数据只应落一份（不能重复写）"


def test_with_retry_gives_up_on_permanent_errors(tmp_path):
    """非瞬时的错误（比如表不存在）必须原样抛出，不能被重试悄悄吞掉。"""
    path = tmp_path / "noddl.db"
    conn = db.connect(path)
    conn.close()

    with pytest.raises(sqlite3.OperationalError) as excinfo:
        db.transact(path, lambda conn: conn.execute("SELECT * FROM demo"))
    assert "no such table" in str(excinfo.value).lower()


def test_busy_timeout_is_set(tmp_path):
    """所有连接统一设置 busy_timeout（写锁等待的前提）。

    注意：刻意**不**启用 WAL——短连接高频开关下 WAL 附属文件有删除竞态
    （Windows 实测复现 readonly database），见 db.py._initialize 的注释。
    """
    path = tmp_path / "bt.db"
    conn = db.connect(path, ddl=_DDL)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout == db.DEFAULT_BUSY_TIMEOUT_MS


def test_parent_directory_is_created(tmp_path):
    path = tmp_path / "deep" / "nested" / "x.db"
    conn = db.connect(path, ddl=_DDL)
    conn.execute("SELECT 1").fetchone()
    conn.close()
    assert path.exists()
