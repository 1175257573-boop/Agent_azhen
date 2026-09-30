"""agent_kit.db 统一 SQLite 访问层的测试。

背景：memories / rollout 各自的 _connect 在多线程并发初始化同一个新库时，
Windows 上会随机抛 `attempt to write a readonly database`
（test_memory_jobs 偶发失败的根因）。这里把并发初始化钉死成回归测试。
"""

from __future__ import annotations

import sqlite3
import threading

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

    旧实现在这个场景下 5 轮里至少挂 1 次；修复后必须稳定通过。
    """
    path = tmp_path / "race.db"
    errors: list[str] = []

    def worker(n: int) -> None:
        for i in range(20):
            try:
                conn = db.connect(path, ddl=_DDL)
                conn.execute(
                    "INSERT INTO demo (id, value) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                    (f"t{n}-{i}", str(i)),
                )
                conn.commit()
                conn.close()
            except sqlite3.OperationalError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


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
