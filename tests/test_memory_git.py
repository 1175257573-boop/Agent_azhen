"""Git 基线（workspace diff）的测试。

要点有两个：
  * `snapshot()` 的返回值必须把「有变更」和「没变更」分清楚——合并省不省那次 LLM 调用
    就靠这个返回值，它说谎的话整套增量机制就没意义了
  * git 不可用时必须**明确报出来**，不能静默假装成功
"""

from __future__ import annotations

import pytest

from agent_kit import memory_git

requires_git = pytest.mark.skipif(not memory_git.git_available(), reason="本机没有 git")


@pytest.fixture(name="repo")
def repo_fixture(tmp_path):
    """一个已经初始化好的记忆产物目录。"""
    directory = tmp_path / "memories"
    ok, reason = memory_git.ensure_repo(directory)
    assert ok, reason
    return directory


@requires_git
def test_ensure_repo_is_idempotent(repo):
    first = memory_git.is_repo(repo)
    again_ok, again_reason = memory_git.ensure_repo(repo)
    assert first and again_ok
    assert again_reason == "已存在基线仓库"
    assert (repo / ".gitattributes").is_file()


@requires_git
def test_snapshot_reports_false_when_nothing_changed(repo):
    (repo / "a.md").write_text("第一条记忆\n", encoding="utf-8")
    assert memory_git.snapshot(repo, "baseline") is True
    # 关键：同一个内容再落一次必须是 False，否则「无变更就跳过合并」永远不会生效
    assert memory_git.snapshot(repo, "baseline again") is False


@requires_git
def test_changes_detects_added_modified_deleted(repo):
    (repo / "a.md").write_text("第一条\n", encoding="utf-8")
    (repo / "b.md").write_text("第二条\n", encoding="utf-8")
    memory_git.snapshot(repo, "baseline")

    assert memory_git.changes(repo) == []

    (repo / "c.md").write_text("第三条\n", encoding="utf-8")
    (repo / "a.md").write_text("改过了\n", encoding="utf-8")
    kinds = {path: kind for kind, path in memory_git.changes(repo)}
    assert kinds["c.md"] == "added"
    assert kinds["a.md"] == "modified"

    (repo / "b.md").unlink()
    kinds = {path: kind for kind, path in memory_git.changes(repo)}
    assert kinds["b.md"] == "deleted"


@requires_git
def test_history_records_snapshots(repo):
    (repo / "a.md").write_text("第一条\n", encoding="utf-8")
    memory_git.snapshot(repo, "baseline: 第一次")
    log = memory_git.history(repo)
    assert any("baseline" in line for line in log)


def test_functions_degrade_explicitly_without_repo(tmp_path, monkeypatch):
    """没有仓库 / 没有 git 时，返回空值并把原因交回调用方，不假装成功。"""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert memory_git.is_repo(plain) is False
    assert memory_git.changes(plain) == []
    assert memory_git.snapshot(plain, "whatever") is False
    assert memory_git.history(plain) == []

    memory_git.ensure_repo(plain)   # 建个真仓库，确保下面的失败是因为 git 不存在而不是目录不存在
    monkeypatch.setattr(memory_git, "git_available", lambda: False)
    ok_without_git, reason_without_git = memory_git.ensure_repo(plain)
    assert ok_without_git is False
    assert "git" in reason_without_git      # 原因要能写进运行报告


def test_describe_renders_chinese_labels():
    items = [("added", "raw_memories.md"), ("modified", "MEMORY.md"), ("deleted", "old.md")]
    text = memory_git.describe(items)
    assert "新增 raw_memories.md" in text
    assert "修改 MEMORY.md" in text
    assert "删除 old.md" in text
    assert memory_git.describe([]) == "（无变化）"


@requires_git
def test_phase2_skips_model_when_artifacts_unchanged(tmp_path):
    """git 基线的主要收益：产物一字未变时不调模型、不重写 MEMORY.md。"""
    from agent_kit import memories

    db = tmp_path / "mem.db"
    root = tmp_path / "memories"
    memories.save(_record("t1", usage=2), db_path=db)

    model = _MergeCounter()
    first = memories.run_phase2(model=model, db_path=db, memories_dir=root)
    assert first.succeeded == ["MEMORY.md"]
    assert model.calls == 1

    second = memories.run_phase2(model=model, db_path=db, memories_dir=root)
    assert model.calls == 1                       # 关键：第二次一次模型都没调
    assert any("无变化" in reason for _tid, reason in second.skipped)

    # 多出一条会话 → 产物真的变了 → 才应该重新合并
    memories.save(_record("t2", usage=2), db_path=db)
    third = memories.run_phase2(model=model, db_path=db, memories_dir=root)
    assert model.calls == 2
    assert third.succeeded == ["MEMORY.md"]


@requires_git
def test_phase2_passes_diff_hint_to_model(tmp_path):
    """合并时要把「这次变了哪些文件」交给模型，否则增量机制只省了钱没提升质量。"""
    from agent_kit import memories

    db = tmp_path / "mem.db"
    root = tmp_path / "memories"
    memories.save(_record("t2", usage=2), db_path=db)
    model = _MergeCounter()
    memories.run_phase2(model=model, db_path=db, memories_dir=root)
    assert "raw_memories.md" in model.prompts[-1]


def test_phase2_without_git_rewrites_every_time(tmp_path):
    """关掉 git 就退回全量重写（旧行为），且不会留下含义不清的跳过理由。"""
    from agent_kit import memories

    db = tmp_path / "mem.db"
    root = tmp_path / "memories"
    memories.save(_record("t1", usage=2), db_path=db)

    model = _MergeCounter()
    memories.run_phase2(model=model, db_path=db, memories_dir=root, use_git=False)
    memories.run_phase2(model=model, db_path=db, memories_dir=root, use_git=False)
    assert model.calls == 2
    assert not (root / ".git").exists()


def test_phase2_reports_missing_git_in_notes(tmp_path, monkeypatch):
    """git 不可用要说在明处，由调用方决定降级，不能偷偷走全量。"""
    from agent_kit import memories

    monkeypatch.setattr(memory_git, "git_available", lambda: False)
    db = tmp_path / "mem.db"
    root = tmp_path / "memories"
    memories.save(_record("t1", usage=2), db_path=db)

    report = memories.run_phase2(model=_MergeCounter(), db_path=db, memories_dir=root)
    assert report.succeeded == ["MEMORY.md"]
    assert any("跳过基线 diff" in note for note in report.notes)


class _MergeCounter:
    """记录 Phase 2 调了几次模型、每次的 prompt 是什么。"""

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def invoke(self, prompt: str):
        self.calls += 1
        self.prompts.append(prompt)
        return _TextResult("# MEMORY\n\n- 合并后的记忆")


class _TextResult:
    def __init__(self, content: str) -> None:
        self.content = content


def _record(thread_id: str, *, usage: int = 0):
    from datetime import datetime, timezone

    from agent_kit.memories import MemoryRecord

    return MemoryRecord(
        thread_id=thread_id,
        raw_memory=f"{thread_id} 的原始记忆",
        summary=f"{thread_id} 的摘要",
        slug="",
        generated_at=datetime.now(timezone.utc).isoformat(),
        usage_count=usage,
        last_usage=datetime.now(timezone.utc).isoformat(),
    )
