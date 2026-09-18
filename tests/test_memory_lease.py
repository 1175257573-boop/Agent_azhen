"""Phase 1 并发保护（lease/claim）的测试。

这些用例的写法有个讲究：**必须真的多线程去抢**。
单线程调用 claim 两次只能验状态机，验不出加锁逻辑到底有没有用——
真正的坑（check-then-act 竞态）只在并发下才暴露。
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from types import SimpleNamespace

from agent_kit import memories, rollout
from agent_kit.memories import MemoryExtract, MemoryRecord


class _CountingModel:
    """每次抽取都记账，用来断言「同一条会话没有被抽两次」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def with_structured_output(self, schema):
        outer = self

        class _Extract:
            def invoke(self, prompt: str) -> MemoryExtract:
                with outer.lock:
                    outer.calls.append(prompt)
                return MemoryExtract(
                    raw_memory="并发下抽取的记忆",
                    rollout_summary="一次并发抽取",
                    rollout_slug="concurrent",
                )

        return _Extract()

    def invoke(self, prompt: str):
        return SimpleNamespace(content="# MEMORY\n\n- 并发安全的记忆")


class _BoomModel:
    """总会失败的模型，用来验退避路径。"""

    def with_structured_output(self, schema):
        class _Extract:
            def invoke(self, prompt: str) -> MemoryExtract:
                raise RuntimeError("模型炸了")

        return _Extract()


def _record(thread_id: str, *, usage: int = 0) -> MemoryRecord:
    from datetime import datetime, timezone

    return MemoryRecord(
        thread_id=thread_id,
        raw_memory=f"{thread_id} 的原始记忆",
        summary=f"{thread_id} 的摘要",
        slug="",
        generated_at=datetime.now(timezone.utc).isoformat(),
        usage_count=usage,
    )


def test_claim_is_exclusive(tmp_path):
    """同一个人抢两次：第二次必须失败（不同 owner 更不用说了）。"""
    db = tmp_path / "m.db"
    assert memories.claim("t1", owner="a", db_path=db) is not None
    assert memories.claim("t1", owner="b", db_path=db) is None

    lease = memories.lease_state("t1", db_path=db)
    assert lease is not None
    assert lease.state == "claimed"
    assert lease.owner == "a"
    assert lease.held is True


def test_expired_lease_can_be_taken_over(tmp_path):
    """lease 过期（相当于持有者进程崩了）后必须能被接手，不能永久悬挂。"""
    db = tmp_path / "m.db"
    assert memories.claim("t1", owner="crashed", lease_seconds=1, db_path=db) is not None

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE memory_lease SET lease_until = ? WHERE thread_id = 't1'", (0.0,))
    conn.commit()
    conn.close()

    assert memories.claim("t1", owner="rescue", db_path=db) is not None
    assert memories.lease_state("t1", db_path=db).owner == "rescue"


def test_release_only_lets_owner_undo(tmp_path):
    db = tmp_path / "m.db"
    memories.claim("t1", owner="a", db_path=db)
    assert memories.release("t1", "b", db_path=db) is False      # 不是自己的锁，放不掉
    assert memories.release("t1", "a", db_path=db) is True
    assert memories.lease_state("t1", db_path=db).state == "pending"


def test_failed_goes_through_backoff_then_stops(tmp_path):
    """失败三次后停在那儿等人介入，而不是无限重试烧 Token。

    循环里用 force 是因为第 1 次失败的退避是 10 秒，测试不可能真等；
    force 在这里代表「退避时间已过，重试者来了」。
    """
    db = tmp_path / "m.db"
    owner = "worker"
    for expected in (1, 2, 3):
        assert memories.claim("t1", owner=owner, force=True, db_path=db) is not None
        attempts = memories.mark_failed("t1", owner, "boom", db_path=db)
        assert attempts == expected

    lease = memories.lease_state("t1", db_path=db)
    assert lease.state == "failed"
    assert lease.attempts == 3
    # 达到上限后不排下次重试时间，于是正常 claim 抢不到
    assert lease.next_attempt_at is None
    assert memories.claim("t1", owner="someone-else", db_path=db) is None


def test_backoff_table_has_no_dead_entries(tmp_path):
    """退避表长度必须正好是 上限-1，多出来的是永远用不到的死配置。"""
    assert len(memories._BACKOFF_SECONDS) == memories.DEFAULT_MAX_ATTEMPTS - 1
    for attempts in range(1, memories.DEFAULT_MAX_ATTEMPTS):
        assert memories._backoff_seconds(attempts) > 0
    assert memories._backoff_seconds(memories.DEFAULT_MAX_ATTEMPTS) is None


def test_backoff_window_blocks_immediate_retry(tmp_path):
    """退避窗口没过时不许重试，避免失败的请求被反复重放。"""
    db = tmp_path / "m.db"
    memories.claim("t2", owner="w", db_path=db)
    memories.mark_failed("t2", "w", "boom", db_path=db)

    lease = memories.lease_state("t2", db_path=db)
    assert lease.attempts == 1
    assert lease.next_attempt_at > memories._utc_epoch()   # 未来某个时刻才准重试
    assert memories.claim("t2", owner="w2", db_path=db) is None

    # 把等待时间拨到过去（模拟退避已过）→ 这次就该抢到了
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE memory_lease SET next_attempt_at = ? WHERE thread_id = 't2'", (0.0,))
    conn.commit()
    conn.close()
    assert memories.claim("t2", owner="w2", db_path=db) is not None


def test_reset_lease_re_enables_extraction(tmp_path):
    db = tmp_path / "m.db"
    memories.claim("t1", owner="w", db_path=db)
    memories.mark_failed("t1", "w", "boom", db_path=db)
    memories.mark_failed("t1", "w", "boom", db_path=db)
    memories.mark_failed("t1", "w", "boom", db_path=db)

    assert memories.reset_lease("t1", db_path=db) is True
    assert memories.claim("t1", owner="w2", db_path=db) is not None


def test_done_thread_is_not_re_extracted_when_rollout_unchanged(tmp_path):
    """流水没变就不重复调模型：既省钱，也避免 MEMORY.md 在两版结果之间摇摆。"""
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    rollout.append("t1", [SimpleNamespace(type="human", content="你好", name=None)], db_path=rollout_db)

    model = _CountingModel()
    first = memories.run_phase1(model=model, db_path=db, rollout_db=rollout_db)
    assert first.succeeded == ["t1"]
    assert len(model.calls) == 1

    second = memories.run_phase1(model=model, db_path=db, rollout_db=rollout_db)
    assert len(model.calls) == 1   # 关键：第二次一次模型都没多调
    assert any("流水未变" in reason for _tid, reason in second.skipped)


def test_force_bypasses_the_digest_shortcut(tmp_path):
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    rollout.append("t1", [SimpleNamespace(type="human", content="你好", name=None)], db_path=rollout_db)

    model = _CountingModel()
    memories.run_phase1(model=model, db_path=db, rollout_db=rollout_db)
    again = memories.run_phase1(model=model, db_path=db, rollout_db=rollout_db, force=True)
    assert again.succeeded == ["t1"]
    assert len(model.calls) == 2


def test_new_rollout_triggers_re_extraction(tmp_path):
    """会话接着聊（流水变了）时应该重抽，不能一直吃旧记忆。"""
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    rollout.append("t1", [SimpleNamespace(type="human", content="你好", name=None)], db_path=rollout_db)

    model = _CountingModel()
    memories.run_phase1(model=model, db_path=db, rollout_db=rollout_db)
    rollout.append("t1", [SimpleNamespace(type="human", content="再说点新事", name=None)], db_path=rollout_db)

    again = memories.run_phase1(model=model, db_path=db, rollout_db=rollout_db)
    assert again.succeeded == ["t1"]
    assert len(model.calls) == 2


def test_only_one_winner_under_real_contention(tmp_path):
    """8 个线程同时抢同一条会话，**赢家必须恰好一个**。

    串行调两次 claim 只能验状态机，验不出并发；这条才是真并发。
    互斥的来源是 claim 里那条带条件的 UPDATE（单语句原子），不是运气。
    """
    db = tmp_path / "m.db"
    barrier = threading.Barrier(8)
    winners: list[str] = []
    lock = threading.Lock()

    def racer(name: str) -> None:
        barrier.wait()
        outcome = memories.claim("t1", owner=name, db_path=db)
        if outcome is not None:
            with lock:
                winners.append(name)

    workers = [threading.Thread(target=racer, args=(f"w{i}",)) for i in range(8)]
    for item in workers:
        item.start()
    for item in workers:
        item.join(timeout=60)
    assert all(not item.is_alive() for item in workers)

    assert len(winners) == 1
    lease = memories.lease_state("t1", db_path=db)
    assert lease.owner == winners[0]
    assert lease.state == "claimed"


def test_parallel_workers_extract_each_thread_at_most_once(tmp_path):
    """并发用例：多个 worker 同时扫同一批会话，每条会话最多被抽一次。"""
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    threads = [f"t{i}" for i in range(6)]
    for thread_id in threads:
        rollout.append(
            thread_id,
            [SimpleNamespace(type="human", content=f"{thread_id} 的内容", name=None)],
            db_path=rollout_db,
        )

    model = _CountingModel()
    barrier = threading.Barrier(4)
    reports: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()   # 尽量让四个 worker 同时开工
        result = memories.run_phase1(model=model, threads=list(threads), db_path=db, rollout_db=rollout_db)
        with lock:
            reports.append(result)

    workers = [threading.Thread(target=worker) for _ in range(4)]
    for item in workers:
        item.start()
    for item in workers:
        item.join(timeout=60)

    assert all(not item.is_alive() for item in workers)
    # 每个 worker 都跑完了，但模型调用总数不能超过会话数 —— 多出来就是重复抽取
    counts = Counter(model.calls)
    assert sum(counts.values()) <= len(threads)
    assert max(counts.values(), default=0) == 1

    extracted = sum(counts.values())
    done = [tid for tid in threads if memories.lease_state(tid, db_path=db).state == "done"]
    assert len(done) == extracted       # 台账和模型调用次数对得上，没有「声称完成却没抽」


def test_phase1_failure_is_recorded_not_swallowed(tmp_path):
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    rollout.append("t1", [SimpleNamespace(type="human", content="你好", name=None)], db_path=rollout_db)

    report = memories.run_phase1(model=_BoomModel(), db_path=db, rollout_db=rollout_db)
    assert report.failed and report.failed[0][0] == "t1"
    # 失败后锁要归还，否则这条会话永远卡在 claimed
    lease = memories.lease_state("t1", db_path=db)
    assert lease.state == "failed"
    assert lease.owner is None
    assert lease.next_attempt_at is not None
    assert memories.all_records(db_path=db) == []


def test_busy_reason_mentions_holder(tmp_path):
    db = tmp_path / "m.db"
    memories.claim("t1", owner="other-worker", lease_seconds=600, db_path=db)
    reason = memories._busy_reason(memories.lease_state("t1", db_path=db))
    assert "other-worker" in reason
