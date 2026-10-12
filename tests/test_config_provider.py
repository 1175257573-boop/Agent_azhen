"""provider 选择的测试。

重点不是"能选出来"，而是**多 key 场景下选出来的那个能不能用**——
这正是实测踩过的坑：机器上同时有失效与可用的 key，
只按"有没有配"去挑会挑中失效那个，于是全程 401。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_kit import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """清掉所有 key 相关环境变量，避免开发者本机的真实 key 干扰断言。"""
    for name in ("LLM_PROVIDER", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL",
                 "DEEPSEEK_API_KEY", "DEEPSEEK_TOKEN", "DEEPSEEK_BASE_URL",
                 "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    yield


# ------------------------------------------------------------ 基础选择


def test_no_keys_gives_fake():
    assert config.available_providers() == []
    assert config.detect_provider() == "fake"


def test_priority_order_is_respected(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-y")
    # deepseek 在 dashscope 之前
    assert config.available_providers() == ["deepseek", "dashscope"]
    assert config.detect_provider() == "deepseek"


def test_alt_env_name_counts(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_TOKEN", "sk-y")   # 不是 ..._API_KEY
    assert config.detect_provider() == "deepseek"


# ------------------------------------------------------------ 惰性回退


def test_resilient_judge_skips_broken_provider(monkeypatch):
    """第一个 provider 抛错时应自动换到第二个，而不是整轮失败。

    这是本机踩过的真实场景：失效的 DEEPSEEK_API_KEY 排在可用的
    DASHSCOPE_API_KEY 前面，只按"有没有配"挑必然挑中失效那个。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-broken")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-good")

    calls: list[str] = []

    class FakeModel:
        def __init__(self, provider):
            self.provider = provider

        def invoke(self, prompt):
            if self.provider == "deepseek":
                raise RuntimeError("401 Incorrect API key")
            return SimpleNamespace(content=f"ok:{self.provider}")

    monkeypatch.setattr(config, "build_chat_model",
                        lambda s: FakeModel(s.provider))
    judge, candidates = config.build_resilient_judge()
    assert candidates == ["deepseek", "dashscope"]
    assert judge("你好") == "ok:dashscope"
    assert judge.failed() == ["deepseek"]
    assert calls == []


def test_resilient_judge_reuses_working_model(monkeypatch):
    """切换到可用的那个之后要缓存，不能每次调用都重建。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-good")
    built: list[str] = []

    class FakeModel:
        def invoke(self, prompt):
            return SimpleNamespace(content="ok")

    def fake_build(settings):
        built.append(settings.provider)
        return FakeModel()

    monkeypatch.setattr(config, "build_chat_model", fake_build)
    judge, _ = config.build_resilient_judge()
    judge("a")
    judge("b")
    judge("c")
    assert built == ["dashscope"], f"成功路径不该重复构造模型，实际 {built}"


def test_resilient_judge_raises_when_all_fail(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-a")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-b")

    def boom(settings):
        raise RuntimeError("连接失败")

    monkeypatch.setattr(config, "build_chat_model", boom)
    judge, candidates = config.build_resilient_judge()
    with pytest.raises(RuntimeError) as e:
        judge("x")
    assert "2 次尝试" in str(e.value)


def test_explicit_provider_is_not_overridden(monkeypatch):
    """显式设了 LLM_PROVIDER 就只用它——静默换 provider 更让人困惑。"""
    monkeypatch.setenv("LLM_PROVIDER", "dashscope")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-a")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-b")
    _, candidates = config.build_resilient_judge()
    assert candidates == ["dashscope"]


def test_no_candidates_means_empty_list(monkeypatch):
    judge, candidates = config.build_resilient_judge()
    assert candidates == []
    with pytest.raises(RuntimeError, match="0 次尝试"):
        judge("x")