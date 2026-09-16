"""工具调用钩子的同步 / 异步双实现。

背景（README 6.3）：`@wrap_tool_call` 只为它装饰的那一种函数生成实现，
`stream()` 走同步、`astream()` 走异步，缺一个就直接 NotImplementedError。
所以这对「双模」必须被测试钉住，否则将来有人改回单实现时会在线上炸。
"""

from __future__ import annotations

import asyncio

from agent_kit.tool_hooks import dual, dual_model


# ------------------------------------------------------------------ 工具侧
def _sync_tool_hook(request, handler):
    return f"sync:{handler(request)}"


async def _async_tool_hook(request, handler):
    return f"async:{await handler(request)}"


def test_dual_exposes_both_hook_names():
    mw = dual(_sync_tool_hook, _async_tool_hook, name="demo")
    # 两个钩子都必须存在，缺任一个都会在另一种调用方式下抛 NotImplementedError
    assert callable(mw.wrap_tool_call)
    assert callable(mw.awrap_tool_call)
    assert asyncio.iscoroutinefunction(mw.awrap_tool_call)


def test_dual_sync_path():
    mw = dual(_sync_tool_hook, _async_tool_hook)
    assert mw.wrap_tool_call("req", lambda r: "ok") == "sync:ok"


def test_dual_async_path():
    async def handler(request):
        return "ok"

    mw = dual(_sync_tool_hook, _async_tool_hook)
    assert asyncio.run(mw.awrap_tool_call("req", handler)) == "async:ok"


def test_dual_name_is_readable():
    # AgentMiddleware.name 是只读属性，必须用 mixin 覆盖，否则永远返回类名
    mw = dual(_sync_tool_hook, _async_tool_hook, name="my_hook")
    assert mw.name == "my_hook"


# ------------------------------------------------------------------ 模型侧
def _sync_model_hook(request, handler):
    return f"m-sync:{handler(request)}"


async def _async_model_hook(request, handler):
    return f"m-async:{await handler(request)}"


def test_dual_model_exposes_both():
    mw = dual_model(_sync_model_hook, _async_model_hook, name="swapper")
    assert callable(mw.wrap_model_call)
    assert callable(mw.awrap_model_call)
    assert asyncio.iscoroutinefunction(mw.awrap_model_call)


def test_dual_model_both_paths():
    async def handler(request):
        return "ok"

    mw = dual_model(_sync_model_hook, _async_model_hook)
    assert mw.wrap_model_call("req", lambda r: "ok") == "m-sync:ok"
    assert asyncio.run(mw.awrap_model_call("req", handler)) == "m-async:ok"


def test_dual_model_state_schema_subclass():
    class Dummy:
        pass

    mw = dual_model(_sync_model_hook, _async_model_hook, state_schema=Dummy)
    # 自定义 state_schema 要走动态子类，不能污染基类
    assert mw.state_schema is Dummy
    assert asyncio.iscoroutinefunction(mw.awrap_model_call)
