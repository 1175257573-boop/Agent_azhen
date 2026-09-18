"""检索器的协议定义（可扩展接口）。

设计取向：**项目自带检索能力只留字面检索，语义/向量检索交给外部实现**。
理由有两条：

  1. 向量检索要引 embedding 依赖 + 一份向量库，而这两样在「本地跑个 demo」的
     场景里是纯负担——Codex 本身也不做 RAG，检索靠 `file-search` 这类文件搜索。
  2. 但它确实是真实需求，所以留一个稳定的接缝：实现 `Retriever` 协议、注册进来，
     工具层不用改一行代码。

协议刻意做得很小：一个 `name` + 一个 `search`。**小而稳的接缝才有人愿意实现**，
塞一堆方法进去等于劝退。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Hit:
    """一条检索结果。

    doc_id   来源标识（笔记文件名、文档 ID 之类）
    text     片段正文，调用方会按长度截断
    score    相关性分，越大越相关；不同检索器的分值**不要求可比**
    source   检索器自己填的来源说明，用于给模型/用户展示出处
    """

    doc_id: str
    text: str
    score: float
    source: str = ""
    extra: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Retriever(Protocol):
    """检索器协议。实现它 + 注册，即可替换/扩展项目的检索能力。"""

    name: str

    def search(self, query: str, *, top_k: int = 3) -> list[Hit]:
        """检索。返回按相关性降序的结果（可以少于 top_k 条，也可以是空列表）。"""
        ...
