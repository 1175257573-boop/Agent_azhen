"""检索：只留接口与一个内置实现，语义/向量检索由外部按需接入。

对外就四个东西：
    Retriever            协议，实现它就能接进来
    Hit                  结果结构
    register / create    注册与取用
    available / describe 给 CLI 列清单

内置的只有 `keyword` / `phrase` 两个字面检索器（见 keyword.py），
零依赖、离线可跑。要做向量语义检索，自己写个 Retriever 注册进来即可，
工具层（tools.search_notes）不用改——它只认协议，不认具体实现。
"""

from __future__ import annotations

from .base import Hit, Retriever
from .keyword import KeywordRetriever
from .registry import (
    RetrieverError,
    RetrieverNotFoundError,
    available,
    create,
    default_name,
    describe,
    register,
)

# 内置检索器在 import 时完成注册，所以这里要显式引入一次
__all__ = [
    "Hit",
    "KeywordRetriever",
    "Retriever",
    "RetrieverError",
    "RetrieverNotFoundError",
    "available",
    "create",
    "default_name",
    "describe",
    "register",
]
