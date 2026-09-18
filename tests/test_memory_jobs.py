"""Phase 1 编排层（Codex 形态的 startup claim）的测试。

重点是把「为什么这条会话能/不能被受理」测到 —— 门槛配错不会报错，
只会在几个月后发现记忆里全是些 agent 内部调度的流水。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_kit import memory_jobs, rollout
from agent_kit.memories import MemoryExtract


class _StubModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def with_structured_output(self, schema):
        outer = self

        class _Extract:
            def invoke(self, prompt: str) -> MemoryExtract:
                outer.calls.append(prompt)
                return MemoryExtract(
                    raw_memory="用户偏好：解释要讲原理",
                    rollout_summary="问了记忆怎么管",
                    rollout_slug="memory-howto",
                )

        return _Extract()

    def invoke(self, prompt: str):
        return SimpleNamespace(content="# MEMORY\n\n- 偏好：讲原理")


def _seed(rollout_db: Path, thread_id: str, *, minutes_ago: int = 60, source: str = "cli") -> None:
    """写一条会话流水，并把它的最后活动时间改到指定时刻。"""
    rollout.append(
        thread_id,
        [SimpleNamespace(type="human", content=f"{thread_id} 的内容", name=None)],
        db_path=rollout_db,
        source=source,
    )
    import sqlite3

    stamp = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=minutes_ago)).isoformat()
    conn = sqlite3.connect(str(rollout_db))
    conn.execute("UPDATE rollout SET created_at = ? WHERE thread_id = ?", (stamp, thread_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 资格门槛
# ---------------------------------------------------------------------------
def test_interactive_sources_are_the_default(tmp_path):
    """默认只收交互会话：sub-agent / 工具会话不该被当成用户长期记忆。"""
    rules = memory_jobs.Eligibility()
    assert set(rules.sources) == set(rollout.INTERACTIVE_SOURCES)
    assert "subagent" not in rules.sources and "tool" not in rules.sources


def test_idle_sessions_are_picked_active_ones_are_not(tmp_path):
    db = tmp_path / "r.db"
    _seed(db, "fresh", minutes_ago=1)      # 刚刚还在聊
    _seed(db, "settled", minutes_ago=90)   # 凉了

    picked = memory_jobs.select_eligible(rollout_db=db)
    assert picked == ["settled"]


def test_old_sessions_fall_outside_the_age_window(tmp_path):
    db = tmp_path / "r.db"
    _seed(db, "ancient", minutes_ago=60 * 24 * 30)   # 30 天前
    _seed(db, "recent", minutes_ago=120)
    rules = memory_jobs.Eligibility(max_age_days=7)

    picked = memory_jobs.select_eligible(eligibility=rules, rollout_db=db)
    assert "recent" in picked
    assert "ancient" not in picked


def test_subagent_sessions_are_excluded(tmp_path):
    db = tmp_path / "r.db"
    _seed(db, "human-talk", minutes_ago=90, source="cli")
    _seed(db, "inner-work", minutes_ago=90, source="subagent")

    picked = memory_jobs.select_eligible(rollout_db=db)
    assert "human-talk" in picked
    assert "inner-work" not in picked


def test_max_jobs_caps_the_batch(tmp_path):
    """bounded work per startup：不能一次受理无限条。"""
    db = tmp_path / "r.db"
    for i in range(12):
        _seed(db, f"t{i}", minutes_ago=60)

    picked = memory_jobs.select_eligible(eligibility=memory_jobs.Eligibility(max_jobs=3), rollout_db=db)
    assert len(picked) == 3


def test_eligibility_from_env_ignores_bad_values(tmp_path, monkeypatch):
    """半个坏配置宁可全丢，也不要静默用错的数字跑很久。"""
    monkeypatch.setenv("ATLAS_MEMORY_MAX_JOBS", "abc")
    monkeypatch.setenv("ATLAS_MEMORY_IDLE_MINUTES", "7")
    rules = memory_jobs.Eligibility.from_env()
    assert rules.max_jobs == memory_jobs.Eligibility().max_jobs   # 非法值被忽略
    assert rules.idle_minutes == 7

    monkeypatch.setenv("ATLAS_MEMORY_SOURCES", "cli,nonsense")
    rules = memory_jobs.Eligibility.from_env()
    assert rules.sources == rollout.INTERACTIVE_SOURCES           # 含未知来源：整条忽略


def test_unparsable_timestamp_is_dropped(tmp_path):
    db = tmp_path / "r.db"
    _seed(db, "broken", minutes_ago=60)
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE rollout SET created_at = 'not-a-date' WHERE thread_id = 'broken'")
    conn.commit()
    conn.close()

    assert memory_jobs.select_eligible(rollout_db=db) == []


# ---------------------------------------------------------------------------
# 密钥脱敏
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "needle", "must_be_gone"),
    [
        ("我的 key 是 sk-abcdefghijklmnopqrstuvwxyz123456", "sk-abcdefghijklmnopqrstuvwxyz123456", True),
        ("github token ghp_abcdefghijklmnopqrstuv", "ghp_abcdefghijklmnopqrstuv", True),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefg", "eyJhbGciOiJIUzI1NiJ9abcdefg", True),
        ("api_key=AXIS2024secretvalue", "AXIS2024secretvalue", True),
    ],
)
def test_secrets_are_redacted(raw, needle, must_be_gone):
    cleaned = memory_jobs.redact_secrets(raw)
    if must_be_gone:
        assert needle not in cleaned
    assert "[REDACTED" in cleaned or "=[REDACTED]" in cleaned


def test_redaction_keeps_normal_text_intact():
    """宁可漏，也不能把正常内容改坏——这条要有测试兜着。"""
    text = "用户偏好：回复用中文，例子要给命令；版本号 skylake-2024 不是密钥"
    assert memory_jobs.redact_secrets(text) == text


def test_phase1_redacts_before_saving(tmp_path):
    """脱敏必须在落盘前发生，否则密钥会进 SQLite 再进 git 基线仓库。"""
    from agent_kit import memories

    class _Leaky(_StubModel):
        def __init__(self) -> None:
            super().__init__()

            class _Extract:
                def invoke(self_inner, prompt: str) -> MemoryExtract:
                    return MemoryExtract(
                        raw_memory="用户说他的 key 是 sk-ZZZZZZZZZZZZZZZZZZZZ，要记住",
                        rollout_summary="粘贴了密钥",
                        rollout_slug=None,
                    )

            self._extract = _Extract()

        def with_structured_output(self, schema):
            return self._extract

    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    _seed(rollout_db, "t1", minutes_ago=60)

    memories.run_phase1(model=_Leaky(), threads=["t1"], db_path=db, rollout_db=rollout_db)
    stored = memories.all_records(db_path=db)[0]
    assert "sk-ZZZZZZZZZZZZZZZZZZZZ" not in stored.raw_memory
    assert "[REDACTED" in stored.raw_memory


# ---------------------------------------------------------------------------
# 没有内容时不编造
# ---------------------------------------------------------------------------
def test_no_useful_content_is_reported_not_fabricated(tmp_path):
    """红线用例：模型说「没东西好记」，就得如实跳过，不能拿模板补一条。"""
    from agent_kit import memories

    class _Empty(_StubModel):
        def with_structured_output(self, schema):
            class _Extract:
                def invoke(self_inner, prompt: str) -> MemoryExtract:
                    return MemoryExtract(
                        raw_memory="本次无可记忆内容",
                        rollout_summary="闲聊",
                        rollout_slug=None,
                    )

            return _Extract()

    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    _seed(rollout_db, "t1", minutes_ago=60)

    report = memories.run_phase1(model=_Empty(), threads=["t1"], db_path=db, rollout_db=rollout_db)
    assert report.succeeded == []
    assert report.failed == []
    assert memories.all_records(db_path=db) == []
    assert any("没有可记忆内容" in reason for _tid, reason in report.skipped)


# ---------------------------------------------------------------------------
# 编排 / 后台
# ---------------------------------------------------------------------------
def test_pipeline_respects_eligibility_and_runs_parallel(tmp_path):
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    for i in range(4):
        _seed(rollout_db, f"t{i}", minutes_ago=60)
    _seed(rollout_db, "busy-now", minutes_ago=1)     # 正在聊，不该被受理

    model = _StubModel()
    report = memory_jobs.run_pipeline(
        model=model,
        eligibility=memory_jobs.Eligibility(max_jobs=4, concurrency=3, idle_minutes=30),
        db_path=db,
        rollout_db=rollout_db,
        phase2=False,
    )
    assert report.ok
    assert len(report.claimed) == 4
    assert "busy-now" not in report.claimed
    assert len(model.calls) == 4


def test_pipeline_without_model_skips_without_writing(tmp_path):
    """没有模型时整轮不该凭空产出记忆。"""
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    _seed(rollout_db, "t1", minutes_ago=60)

    report = memory_jobs.run_pipeline(
        make_model=lambda: None,
        db_path=db,
        rollout_db=rollout_db,
        phase2=False,
    )
    assert report.claimed == ()
    assert report.failed == 0
    assert report.skipped


def test_spawn_runs_in_background_thread(tmp_path):
    db = tmp_path / "m.db"
    rollout_db = tmp_path / "r.db"
    for i in range(3):
        _seed(rollout_db, f"t{i}", minutes_ago=60)

    worker, bag = memory_jobs.spawn(db_path=db, rollout_db=rollout_db, phase2=False)
    assert worker.daemon is True       # 守护线程：主进程退出时不拖住
    finished = bag["done"].wait(timeout=30)
    assert finished, "后台任务在 30 秒内没跑完"
    worker.join(timeout=30)

    report = bag["report"]
    assert report is not None
    # 环境里没有真实模型 → 应当明确跳过而不是出错或编造
    assert report.claimed == ()
    assert not report.errors
