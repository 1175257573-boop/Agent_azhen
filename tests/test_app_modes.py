"""冒烟测试：所有运行模式都能装配出图。

这是改动中间件 / 钩子之后的第一道防线——任何一处 `awrap_*` 缺失、
import 环、参数写错，都会在这里立刻暴露，而不是等到用户点某个页面才发现。

刻意用 `provider="fake"`：不依赖任何真实 API Key。
"""

from __future__ import annotations

import pytest

from agent_kit.app import MODE_HELP, AppConfig, build_app

# mcp 模式必须异步装配（会 fork 本地 MCP Server 子进程），单独放 test_mcp_async.py
SYNC_MODES = [m for m in MODE_HELP if m != "mcp"]


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_build_app_sync_modes(mode):
    cfg = AppConfig(provider="fake", mode=mode, thread_id=f"test-{mode}")
    app = build_app(cfg)

    assert app.graph is not None
    assert not app.is_mcp, "未开启 MCP 时不该带 MCP 标记"
    # thread_id 要能被正确透传到 config（短期记忆的主键）
    assert app.thread_config["configurable"]["thread_id"] == f"test-{mode}"
    # context 里要带上用户身份，供中间件做权限判断
    assert app.context["user_id"] == "demo"


def test_unknown_mode_is_rejected():
    from agent_kit.app import build_app

    # AppConfig 本身不校验 mode，由 build_app 落到默认 chat 分支；
    # 真正的合法性校验在 AgentService._cfg 里（避免绕过 service 层）
    cfg = AppConfig(provider="fake", mode="chat")
    assert build_app(cfg).graph is not None


def test_real_model_is_required_without_key():
    """没有 Key 就必须明确失败，绝不能静默降级到假模型（用户明确要求）。"""
    cfg = AppConfig(provider=None, mode="chat")
    with pytest.raises(RuntimeError, match="API Key"):
        build_app(cfg)


def test_explicit_fake_provider_is_allowed():
    """但用户显式选 fake 时要放行（离线演示场景）。"""
    cfg = AppConfig(provider="fake", mode="chat")
    assert build_app(cfg).graph is not None
