"""钉住排队消息（agent_kit/queue.py）与服务层 drain 的行为。

排队这个功能有三类回归最容易发生，都在这里钉住：

  1. **顺序与隔离** —— A 会话的消息跑到 B 会话去，或顺序乱了
  2. **上限与空值** —— 用户狂敲回车导致队列无限增长；空消息被排进来
  3. **drain 的边界** —— 触发人工确认（HITL）后必须暂停 drain，
     否则新消息会灌进一个「等待确认」的中断状态里

服务层的 drain 用一个假 service 验证：把 `_run_one` 换成脚本化实现，
这样不依赖真实模型，也不用起 FastAPI。
"""

from __future__ import annotations

import pytest

from agent_kit.queue import (
    MAX_QUEUED,
    MessageQueue,
    QueueFullError,
    QueueRegistry,
)


# ---------------------------------------------------------------------------
# 基础队列
# ---------------------------------------------------------------------------
def test_enqueue_and_pop_are_fifo():
    q = MessageQueue("t1")
    q.enqueue("第一条")
    q.enqueue("第二条")
    assert q.pop().text == "第一条"
    assert q.pop().text == "第二条"
    assert q.pop() is None


def test_enqueue_rejects_blank_text():
    """空消息不能进队——否则 Agent 会对着一条空输入生成一次回复。"""
    q = MessageQueue("t1")
    with pytest.raises(ValueError):
        q.enqueue("   ")
    assert len(q) == 0


def test_enqueue_respects_max_size():
    q = MessageQueue("t1")
    for i in range(MAX_QUEUED):
        q.enqueue(f"第{i}条")
    with pytest.raises(QueueFullError):
        q.enqueue("太多了")
    assert len(q) == MAX_QUEUED


def test_seq_increments_for_display():
    q = MessageQueue("t1")
    a = q.enqueue("a")
    b = q.enqueue("b")
    assert (a.seq, b.seq) == (1, 2)


def test_pending_lists_without_consuming():
    q = MessageQueue("t1")
    q.enqueue("a")
    q.enqueue("b")
    assert [m.text for m in q.pending()] == ["a", "b"]
    assert len(q) == 2  # 列出不能消耗掉


def test_update_edits_before_send():
    q = MessageQueue("t1")
    item = q.enqueue("打错字了")
    assert q.update(item.id, "改好了").text == "改好了"
    assert q.pop().text == "改好了"


def test_update_rejects_blank():
    q = MessageQueue("t1")
    item = q.enqueue("原文")
    assert q.update(item.id, "  ") is None
    assert q.pop().text == "原文"


def test_remove_and_clear():
    q = MessageQueue("t1")
    a = q.enqueue("a")
    q.enqueue("b")
    assert q.remove(a.id) is True
    assert [m.text for m in q.pending()] == ["b"]
    assert q.remove("不存在的id") is False
    assert q.clear() == 1
    assert q.is_empty


def test_to_dict_has_preview():
    item = MessageQueue("t1").enqueue("x" * 200)
    data = item.to_dict()
    assert len(data["preview"]) == 80
    assert data["thread_id"] == "t1"


# ---------------------------------------------------------------------------
# 会话隔离
# ---------------------------------------------------------------------------
def test_registry_isolates_threads():
    reg = QueueRegistry()
    reg.get("A").enqueue("给A的")
    reg.get("B").enqueue("给B的")
    assert [m.text for m in reg.get("A").pending()] == ["给A的"]
    assert [m.text for m in reg.get("B").pending()] == ["给B的"]


def test_registry_drop_clears():
    reg = QueueRegistry()
    reg.get("A").enqueue("x")
    assert reg.drop("A") == 1
    assert reg.get("A").is_empty


def test_registry_reuses_same_queue_instance():
    """两次 get 必须是同一个对象，否则前端看到的队列和实际 drain 的不是一份。"""
    reg = QueueRegistry()
    assert reg.get("A") is reg.get("A")


# ---------------------------------------------------------------------------
# 服务层 drain：用假执行体验证「自动发送」与「遇到确认就停下」
# ---------------------------------------------------------------------------
class _FakeReq:
    thread_id = "t1"
    mode = "chat"
    provider = None
    user_id = "demo"
    role = "admin"
    message = "第一条"


class _FakeService:
    """复刻 AgentService 的 drain 逻辑，把 `_run_one` 换成脚本化实现。

    直接复用真实 AgentService 会拉起模型和记忆后端，测试要的是**流程正确性**，
    所以这里只照抄 drain 的骨架（与 agent_service._drain 保持一致）。
    """

    def __init__(self, interrupted_at: int | None = None) -> None:
        from agent_kit.queue import QueueRegistry

        self._queues = QueueRegistry()
        self.sent: list[str] = []
        self.interrupted_at = interrupted_at  # 第几条消息会触发人工确认
        self._n = 0

    def _run_one(self, req, text, queued_id=None):
        self._n += 1
        self.sent.append(text)
        interrupted = self.interrupted_at == self._n
        yield {"type": "token", "data": f"回复{text}"}
        yield {"type": "done", "data": {"queued_id": queued_id}}
        return interrupted

    def _drain(self, req):
        queue = self._queues.get(req.thread_id)
        while True:
            item = queue.pop()
            if item is None:
                return
            yield {"type": "queued_start", "data": item.to_dict()}
            interrupted = yield from self._run_one(req, item.text, queued_id=item.id)
            if interrupted:
                return

    def stream(self, req):
        interrupted = yield from self._run_one(req, req.message)
        if interrupted:
            return
        yield from self._drain(req)


def test_stream_auto_drains_queued_messages():
    svc = _FakeService()
    svc._queues.get("t1").enqueue("第二条")
    svc._queues.get("t1").enqueue("第三条")

    events = list(svc.stream(_FakeReq()))
    assert svc.sent == ["第一条", "第二条", "第三条"]
    # 每条排队消息执行前都要先告诉前端，否则前端那条「待发送」气泡不会转正
    starts = [e for e in events if e["type"] == "queued_start"]
    assert [s["data"]["text"] for s in starts] == ["第二条", "第三条"]


def test_drain_stops_at_human_interrupt():
    """人工确认悬而未决时，剩下的排队消息必须留着，等确认完再发。"""
    svc = _FakeService(interrupted_at=1)
    svc._queues.get("t1").enqueue("第二条")

    list(svc.stream(_FakeReq()))
    assert svc.sent == ["第一条"]  # 被中断挡下，第二条没发
    assert len(svc._queues.get("t1")) == 1  # 还留在队列里，没丢


def test_drain_stops_when_queued_item_interrupts():
    svc = _FakeService(interrupted_at=2)
    svc._queues.get("t1").enqueue("第二条")
    svc._queues.get("t1").enqueue("第三条")

    list(svc.stream(_FakeReq()))
    assert svc.sent == ["第一条", "第二条"]
    assert [m.text for m in svc._queues.get("t1").pending()] == ["第三条"]


def test_queue_empty_after_drain():
    svc = _FakeService()
    svc._queues.get("t1").enqueue("第二条")
    list(svc.stream(_FakeReq()))
    assert svc._queues.get("t1").is_empty
