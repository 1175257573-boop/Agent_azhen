"""排队消息（queued messages）：Agent 忙碌时把用户输入缓存下来，本轮结束后自动发出。

解决的问题很具体：
  Agent 一轮要跑几十秒，用户在这期间输入的第二条、第三条指令，
  如果直接丢弃，用户就得重新打一遍；如果立刻并发发给 Agent，
  又会打断当前这轮的上下文。WorkBuddy 的做法是「排队」——
  输入先缓存，当前这轮结束（done）后按顺序自动发出去。

设计取舍：

  1. **为什么用 deque + asyncio.Event，而不是 asyncio.Queue**
     asyncio.Queue 只能 `get()`，拿不到「还没发出的消息列表」；
     而前端要把排队中的消息显示成「待发送 N 条」并允许删除/编辑，
     必须能列出与按下标操作。所以自己维护一个 deque，用 Event 做唤醒。

  2. **为什么 Event 要延迟创建**
     `asyncio.Event()` 在 3.10 之前会绑定创建它的事件循环；
     队列对象是在请求线程/循环外创建的，延迟到第一次 await 时再建，最稳。

  3. **为什么按 thread_id 隔离**
     排队是**会话级**的：A 会话排的消息不能跑到 B 会话去。

  4. **为什么要有上限**
     没有上限时，用户狂敲回车会让队列无限增长，Agent 一轮结束后
     连续自言自语几十轮。上限 20 条，超出直接拒绝并让前端提示。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

# 单个会话最多排多少条：超过说明用户在乱敲，或者 Agent 卡死了
MAX_QUEUED = 20


class QueueFullError(RuntimeError):
    """排队上限；前端应提示「当前回复结束后再试」而不是静默丢弃。"""


@dataclass
class QueuedMessage:
    """一条排队中的用户输入。

    seq 从 1 开始，只用于前端显示「第 N 条待发送」，不参与排序
    （顺序由 deque 本身保证）。
    """

    id: str
    thread_id: str
    text: str
    seq: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["preview"] = self.text[:80]
        return data


class MessageQueue:
    """单个会话的排队队列。

    所有方法都是**同一事件循环内**调用（FastAPI 的请求与 SSE 生成器都在主线程循环里），
    因此不需要加锁；deque 的 append/popleft 本身就是原子的。
    """

    def __init__(self, thread_id: str, max_size: int = MAX_QUEUED) -> None:
        self.thread_id = thread_id
        self.max_size = max_size
        self._items: deque[QueuedMessage] = deque()
        self._event: asyncio.Event | None = None
        self._seq = 0

    # ------------------------------------------------------------ 内部
    def _get_event(self) -> asyncio.Event:
        if self._event is None:
            self._event = asyncio.Event()
        return self._event

    def _wake(self) -> None:
        if self._event is not None:
            self._event.set()

    # ------------------------------------------------------------ 写
    def enqueue(self, text: str) -> QueuedMessage:
        """入队一条消息。空文本直接拒绝——否则会出现「发送了一条空消息」。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("排队内容为空")
        if len(self._items) >= self.max_size:
            raise QueueFullError(f"排队已满（{self.max_size} 条）")

        self._seq += 1
        item = QueuedMessage(id=uuid.uuid4().hex[:12], thread_id=self.thread_id, text=text, seq=self._seq)
        self._items.append(item)
        self._wake()
        return item

    # ------------------------------------------------------------ 读
    def pending(self) -> list[QueuedMessage]:
        """列出全部待发消息（不弹出），给前端渲染用。"""
        return list(self._items)

    def pop(self) -> QueuedMessage | None:
        """弹出队首；没有则返回 None（非阻塞）。"""
        return self._items.popleft() if self._items else None

    async def wait(self, timeout: float | None = None) -> QueuedMessage | None:
        """等到有消息可取为止，返回队首；超时返回 None。

        目前 drain 走的是「先把队列排空再结束」的同步循环，用不到这个等待；
        保留它是为了将来做「Agent 空闲后长驻等待用户输入」的长连接模式。
        """
        if self._items:
            return self.pop()
        event = self._get_event()
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        event.clear()
        return self.pop() if self._items else None

    # ------------------------------------------------------------ 改 / 删
    def update(self, item_id: str, text: str) -> QueuedMessage | None:
        """编辑排队中的消息内容（发之前还能改）。"""
        text = (text or "").strip()
        if not text:
            return None
        for item in self._items:
            if item.id == item_id:
                item.text = text
                return item
        return None

    def remove(self, item_id: str) -> bool:
        """删除一条排队消息；已发出的（已 pop）删不掉，返回 False。"""
        for item in list(self._items):
            if item.id == item_id:
                self._items.remove(item)
                return True
        return False

    def clear(self) -> int:
        """清空队列，返回被清掉的条数（切会话、中止生成时用）。"""
        count = len(self._items)
        self._items.clear()
        return count

    # ------------------------------------------------------------ 元信息
    def __len__(self) -> int:
        return len(self._items)

    @property
    def is_empty(self) -> bool:
        return not self._items


class QueueRegistry:
    """按会话隔离的队列集合。

    生命周期与进程相同；会话被删除时调用 `drop()` 回收，
    否则长时间运行的进程会攒下大量空队列（每个只有几十字节，但没理由留着）。
    """

    def __init__(self) -> None:
        self._queues: dict[str, MessageQueue] = {}

    def get(self, thread_id: str) -> MessageQueue:
        queue = self._queues.get(thread_id)
        if queue is None:
            queue = MessageQueue(thread_id)
            self._queues[thread_id] = queue
        return queue

    def drop(self, thread_id: str) -> int:
        queue = self._queues.pop(thread_id, None)
        return queue.clear() if queue else 0

    def thread_ids(self) -> list[str]:
        return list(self._queues)
