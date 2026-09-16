"""参数注入拦截器的 schema 判断逻辑。

背景（README 7.17）：早期版本无脑给**所有**工具注入 user_id / source，
结果 `current_utc` 这类「不接受任何参数」的工具直接被 Pydantic 判
`unexpected_keyword_argument`。现在改成按 schema 决定是否注入。
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from agent_kit.mcp_client import _schema_accepts, _tool_schema, make_arg_injector


class _NoArgs:
    """模拟 `current_utc`：schema 声明不接受任何参数。"""

    args: ClassVar[dict[str, Any]] = {"properties": {}, "type": "object"}


class _WithUserId(BaseModel):
    user_id: str


class _StrictTool:
    """显式关闭额外属性，且 properties 里没有要注入的键。"""

    args: ClassVar[dict[str, Any]] = {
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
        "type": "object",
    }


class _LooseTool:
    """允许任意额外属性。"""

    args: ClassVar[dict[str, Any]] = {"properties": {}, "additionalProperties": True, "type": "object"}


class _NoSchema:
    """拿不到 schema（None）。"""


# ------------------------------------------------------------------ _tool_schema
def test_tool_schema_reads_dict():
    assert _tool_schema(_NoArgs()) == {"properties": {}, "type": "object"}


def test_tool_schema_none_when_missing():
    assert _tool_schema(None) is None


def test_tool_schema_reads_pydantic():
    # 工具把入参模型挂在 args_schema 上（Pydantic 类，非 dict）
    class _PydanticBacked:
        args_schema = _WithUserId

    schema = _tool_schema(_PydanticBacked())
    assert schema is not None
    assert "user_id" in schema.get("properties", {})


# ------------------------------------------------------------------ _schema_accepts
@pytest.fixture
def keys():
    return {"user_id", "source"}


def test_zero_arg_tool_rejects_injection(keys):
    # current_utc 的 schema：properties 为空 → 不该注入
    assert _schema_accepts({"properties": {}, "type": "object"}, keys) is False


def test_strict_additional_properties_rejects(keys):
    assert _schema_accepts(_StrictTool.args, keys) is False


def test_loose_additional_properties_accepts(keys):
    assert _schema_accepts(_LooseTool.args, keys) is True


def test_missing_schema_rejects(keys):
    # 拿不到 schema 时保守起见不注入
    assert _schema_accepts(None, keys) is False


def test_empty_keys_never_injects():
    assert _schema_accepts({"properties": {}, "type": "object"}, set()) is False


# ------------------------------------------------------------------ 白名单
def test_only_whitelist_bypasses_schema_check():
    """显式指定 only= 时，跳过 schema 判断，强制注入到指定工具。"""

    class FakeRequest:
        def __init__(self):
            self.tool_call = {"name": "target", "args": {"a": 1}}
            self.tool = _NoArgs()

        def override(self, **kw):
            self.tool_call = kw["tool_call"]
            return self

    captured = {}

    def handler(request):
        captured["args"] = request.tool_call["args"]
        return "done"

    injector = make_arg_injector({"user_id": "demo"}, only=["target"])
    injector.wrap_tool_call(FakeRequest(), handler)

    assert captured["args"] == {"a": 1, "user_id": "demo"}


def test_injector_has_async_impl():
    # MCP 场景必走异步，缺了 awrap_tool_call 会在 astream 里直接炸
    import asyncio

    injector = make_arg_injector({"user_id": "demo"})
    assert asyncio.iscoroutinefunction(injector.awrap_tool_call)
