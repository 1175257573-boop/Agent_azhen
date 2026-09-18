"""会话流水（rollout）的测试。

流水是给「事后复盘」用的，写歪了没人会发现，所以要钉住三件事：
顺序对不对、增量同步会不会重复写、取不到状态时会不会把对话搞崩。
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_kit import rollout


class _FakeState:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages

    def get_state(self, config):
        return _FakeState(self._messages)


class _BrokenGraph:
    def get_state(self, config):
        raise RuntimeError("这个图没有 checkpointer")


def test_append_and_load_keep_order(tmp_path):
    db = tmp_path / "rollout.db"
    messages = [
        HumanMessage(content="你好"),
        AIMessage(content="你好，有什么可以帮您？"),
        ToolMessage(content="12:00", tool_call_id="1", name="get_current_time"),
    ]
    assert rollout.append("t1", messages, db_path=db) == 3

    records = rollout.load("t1", db_path=db)
    assert [r["role"] for r in records] == ["human", "ai", "tool"]
    assert records[0]["content"] == "你好"
    assert records[2]["tool_name"] == "get_current_time"


def test_content_blocks_are_flattened(tmp_path):
    db = tmp_path / "rollout.db"
    message = AIMessage(content=[{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}])
    rollout.append("t2", [message], db_path=db)
    assert rollout.load("t2", db_path=db)[0]["content"] == "第一段第二段"


def test_sync_from_checkpoint_is_incremental(tmp_path):
    db = tmp_path / "rollout.db"
    graph = _FakeGraph([HumanMessage(content="a"), AIMessage(content="b")])
    assert rollout.sync_from_checkpoint(graph, "t3", config={}, db_path=db) == 2

    # 同一批消息再同步一次：不该重复写
    assert rollout.sync_from_checkpoint(graph, "t3", config={}, db_path=db) == 0

    graph2 = _FakeGraph(
        [HumanMessage(content="a"), AIMessage(content="b"), HumanMessage(content="c")]
    )
    assert rollout.sync_from_checkpoint(graph2, "t3", config={}, db_path=db) == 1
    assert len(rollout.load("t3", db_path=db)) == 3


def test_sync_returns_zero_when_state_unavailable(tmp_path):
    db = tmp_path / "rollout.db"
    # router 这类自定义 state 的工作流没有 checkpointer，不能让它把对话搞崩
    assert rollout.sync_from_checkpoint(_BrokenGraph(), "t4", config={}, db_path=db) == 0


def test_list_sessions(tmp_path):
    db = tmp_path / "rollout.db"
    rollout.append("session-a", [HumanMessage(content="x")], db_path=db)
    rollout.append("session-b", [HumanMessage(content="y")] * 3, db_path=db)
    sessions = {s["thread_id"]: s["turns"] for s in rollout.list_sessions(db_path=db)}
    assert sessions == {"session-a": 1, "session-b": 3}


def test_export_writes_one_json_per_line(tmp_path):
    db = tmp_path / "rollout.db"
    rollout.append("t5", [HumanMessage(content="你好"), AIMessage(content="嗨")], db_path=db)
    target = tmp_path / "out.jsonl"
    assert rollout.export("t5", target, db_path=db) == 2
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "你好"


def test_clear(tmp_path):
    db = tmp_path / "rollout.db"
    rollout.append("t6", [HumanMessage(content="x")], db_path=db)
    assert rollout.clear("t6", db_path=db) == 1
    assert rollout.load("t6", db_path=db) == []
