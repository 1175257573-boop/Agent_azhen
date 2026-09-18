"""记忆产出管线的测试。

最要紧的一条：**没有模型时不能编造记忆**。所以「无模型 → 全部跳过且不写库」
这条必须有测试兜住，其余功能（抽取、排序、产物同步）才轮得到。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_kit import memories
from agent_kit.memories import MemoryExtract, MemoryRecord


class _FakeStructured:
    def invoke(self, prompt: str) -> MemoryExtract:
        return MemoryExtract(
            raw_memory="用户偏好：回复用中文，例子要给命令",
            rollout_summary="问了检索扩展点怎么用",
            rollout_slug="retrieval-howto",
        )


class _FakeModel:
    """同时支持结构化抽取与合并两种调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def with_structured_output(self, schema):
        return _FakeStructured()

    def invoke(self, prompt: str):
        self.calls.append(prompt)
        return SimpleNamespace(content="# MEMORY\n\n- 偏好：中文回复")


def _record(thread_id: str, *, usage: int = 0, last_usage: str | None = None, days_ago: int = 0):
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return MemoryRecord(
        thread_id=thread_id,
        raw_memory=f"{thread_id} 的原始记忆",
        summary=f"{thread_id} 的摘要",
        slug="",
        generated_at=stamp.isoformat(),
        usage_count=usage,
        last_usage=last_usage or stamp.isoformat(),
    )


def test_phase1_without_model_skips_and_writes_nothing(tmp_path):
    """红线用例：没有模型就跳过，库里**不能凭空多出记忆**。"""
    from agent_kit import rollout as rollout_mod

    db = tmp_path / "mem.db"
    rollout_db = tmp_path / "rollout.db"
    rollout_mod.append("t1", [SimpleNamespace(type="human", content="你好", name=None)], db_path=rollout_db)

    report = memories.run_phase1(model=None, db_path=db, rollout_db=rollout_db)
    assert report.succeeded == []
    assert memories.all_records(db_path=db) == []
    assert any("没有可用模型" in reason for _tid, reason in report.skipped)


def test_phase1_extracts_with_fake_model(tmp_path):
    from agent_kit import rollout as rollout_mod

    db = tmp_path / "mem.db"
    rollout_db = tmp_path / "rollout.db"
    rollout_mod.append(
        "t1",
        [
            SimpleNamespace(type="human", content="怎么用检索扩展点", name=None),
            SimpleNamespace(type="ai", content="实现 Retriever 协议并注册", name=None),
        ],
        db_path=rollout_db,
    )

    report = memories.run_phase1(model=_FakeModel(), db_path=db, rollout_db=rollout_db)
    assert report.succeeded == ["t1"]
    records = memories.all_records(db_path=db)
    assert len(records) == 1
    assert "中文" in records[0].raw_memory
    assert records[0].slug == "retrieval-howto"


def test_mark_used_increments(tmp_path):
    db = tmp_path / "mem.db"
    memories.save(_record("t1"), db_path=db)
    memories.mark_used("t1", db_path=db)
    assert memories.all_records(db_path=db)[0].usage_count == 1


def test_select_prefers_most_used_and_drops_stale(tmp_path):
    db = tmp_path / "mem.db"
    memories.save(_record("low", usage=1), db_path=db)
    memories.save(_record("high", usage=9), db_path=db)
    memories.save(_record("old", usage=5, days_ago=90), db_path=db)   # 超过 30 天未用

    picked = [r.thread_id for r in memories.select_for_phase2(db_path=db)]
    assert picked[0] == "high"
    assert "old" not in picked


def test_sync_artifacts_writes_and_prunes(tmp_path):
    root = tmp_path / "memories"
    summaries = root / "rollout_summaries"
    summaries.mkdir(parents=True)
    (summaries / "gone.md").write_text("过时的会话", encoding="utf-8")

    written = memories.sync_artifacts([_record("b"), _record("a")], memories_dir=root)
    assert (root / "raw_memories.md").is_file()
    assert (summaries / "a.md").is_file() and (summaries / "b.md").is_file()
    assert not (summaries / "gone.md").exists()
    # 产物顺序按 thread_id 稳定，避免每次合并都产生无意义的文件抖动
    raw_text = (root / "raw_memories.md").read_text(encoding="utf-8")
    assert raw_text.index("## a") < raw_text.index("## b")
    assert len(written) == 3


def test_phase2_without_model_only_syncs_artifacts(tmp_path):
    db = tmp_path / "mem.db"
    memories.save(_record("t1", usage=2), db_path=db)
    report = memories.run_phase2(model=None, db_path=db, memories_dir=tmp_path / "memories")
    assert report.failed == []
    assert any("未生成 MEMORY.md" in reason for _tid, reason in report.skipped)
    assert not (tmp_path / "memories" / "MEMORY.md").exists()


def test_phase2_with_model_writes_memory_md(tmp_path):
    db = tmp_path / "mem.db"
    memories.save(_record("t1", usage=2), db_path=db)
    model = _FakeModel()
    report = memories.run_phase2(model=model, db_path=db, memories_dir=tmp_path / "memories")
    assert report.succeeded == ["MEMORY.md"]
    content = (tmp_path / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert "中文回复" in content


def test_code_fence_is_stripped():
    """模型把整篇 Markdown 用 ``` 包起来时，落盘前要剥掉（实测 qwen 会这么干）。"""
    fenced = "```markdown\n# MEMORY\n\n- 一条记忆\n```"
    assert memories._strip_code_fence(fenced) == "# MEMORY\n\n- 一条记忆"
    assert memories._strip_code_fence("# 没有围栏") == "# 没有围栏"
