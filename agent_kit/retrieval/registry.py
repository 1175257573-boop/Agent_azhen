"""检索器注册表。

一条硬规矩：**要的检索器没注册就报错，不许静默降级**。
静默降级的可怕之处在于——你以为在用语义检索，其实跑的是字面匹配，
结果不对还查不出原因（上一版 RAG 就是这么踩的坑：embedding 不可用时
悄悄换成哈希向量，输出看着正常，语义准确度却没了）。

默认检索器名字的优先级：环境变量 `ATLAS_RETRIEVER` > atlas.toml 的
`[retrieval] name` > 内置 `keyword`。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from agent_kit.config import load_atlas_config

from .base import Retriever

# 名字 → 工厂（每次调用 new 一个实例，避免调用方之间共享可变状态）
_REGISTRY: dict[str, Callable[[], Retriever]] = {}

DEFAULT_RETRIEVER = "keyword"


class RetrieverError(RuntimeError):
    """检索相关的基类异常。"""


class RetrieverNotFoundError(RetrieverError):
    """要的检索器没有注册。"""


def register(name: str, factory: Callable[[], Retriever] | None = None) -> Any:
    """注册一个检索器。可用作装饰器，也可直接传工厂函数。

    用法一（装饰器，注册类）：
        @register("mine")
        class MyRetriever:
            ...

    用法二（直接注册工厂）：
        register("mine", lambda: MyRetriever(path))
    """
    if factory is None:
        # 当作装饰器用：被装饰的是类或零参工厂
        def _decorate(cls_or_factory: Callable[[], Retriever]) -> Callable[[], Retriever]:
            _REGISTRY[name] = cls_or_factory
            return cls_or_factory

        return _decorate

    _REGISTRY[name] = factory
    return factory


def available() -> list[str]:
    """已注册的检索器名字（排序后返回，方便打印）。"""
    return sorted(_REGISTRY)


def default_name() -> str:
    """默认用哪个检索器：环境变量 > atlas.toml > keyword。"""
    return (
        os.getenv("ATLAS_RETRIEVER", "").strip()
        or load_atlas_config().retrieval.name
        or DEFAULT_RETRIEVER
    )


def create(name: str | None = None) -> Retriever:
    """按名字取一个检索器。未注册直接抛错——**不静默降级**。"""
    target = name or default_name()
    factory = _REGISTRY.get(target)
    if factory is None:
        raise RetrieverNotFoundError(
            f"未注册的检索器 {target!r}。已注册：{available() or '无'}；"
            f"自定义检索器请实现 agent_kit.retrieval.base.Retriever 并调用 register() 注册。"
        )
    return factory()


def describe() -> dict[str, str]:
    """给 CLI /info 用：名字 → 检索器的自我描述（无 docstring 时给占位）。"""
    out: dict[str, str] = {}
    for name in available():
        instance = create(name)
        doc = (type(instance).__doc__ or "").strip().split("\n")[0]
        out[name] = doc or "（该检索器未写说明）"
    return out
