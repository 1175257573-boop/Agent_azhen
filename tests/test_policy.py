"""审批 / 沙箱策略的测试。

策略决定「写操作要不要先问人」，这类东西错了要么拦得太死、要么形同虚设，
所以优先级、非法值回落、派生字段这三件事都要钉住。
"""

from __future__ import annotations

from agent_kit import config as config_mod
from agent_kit.policy import (
    DEFAULT_APPROVAL,
    DEFAULT_SANDBOX,
    ENV_APPROVAL,
    ENV_SANDBOX,
    describe,
    resolve,
)


def _use_toml(monkeypatch, *paths):
    """切换配置搜索路径，并**强制刷新缓存**（load_atlas_config 默认带缓存）。"""
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: list(paths))
    config_mod.load_atlas_config(reload=True)


def _no_toml(monkeypatch, tmp_path):
    _use_toml(monkeypatch, tmp_path / "不存在.toml")


def test_defaults_are_strictest(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    monkeypatch.delenv(ENV_APPROVAL, raising=False)
    monkeypatch.delenv(ENV_SANDBOX, raising=False)
    policy = resolve()
    assert policy.approval == DEFAULT_APPROVAL == "untrusted"
    assert policy.sandbox == DEFAULT_SANDBOX == "workspace-write"
    assert policy.hitl is True


def test_cli_wins_over_env_and_toml(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_APPROVAL, "never")
    monkeypatch.setenv(ENV_SANDBOX, "read-only")
    policy = resolve(cli_approval="untrusted", cli_sandbox="workspace-write")
    assert policy.approval == "untrusted"
    assert policy.sandbox == "workspace-write"


def test_env_wins_over_toml(monkeypatch, tmp_path):
    path = tmp_path / "atlas.toml"
    path.write_text('[approval_policy]\nvalue = "never"\n', encoding="utf-8")
    _use_toml(monkeypatch, path)
    monkeypatch.setenv(ENV_APPROVAL, "on-failure")
    assert resolve().approval == "on-failure"


def test_toml_wins_over_default(monkeypatch, tmp_path):
    path = tmp_path / "atlas.toml"
    path.write_text(
        '[approval_policy]\nvalue = "on-failure"\n[sandbox]\nmode = "read-only"\n',
        encoding="utf-8",
    )
    _use_toml(monkeypatch, path)
    monkeypatch.delenv(ENV_APPROVAL, raising=False)
    monkeypatch.delenv(ENV_SANDBOX, raising=False)
    policy = resolve()
    assert policy.approval == "on-failure"
    assert policy.escalate_on_failure is True
    assert policy.hitl is False
    assert policy.sandbox == "read-only"
    assert policy.write_tools is False


def test_source_reports_code_default_when_nothing_configured(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    monkeypatch.delenv(ENV_APPROVAL, raising=False)
    monkeypatch.delenv(ENV_SANDBOX, raising=False)
    assert "代码默认值" in resolve().source


def test_illegal_value_falls_back(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    monkeypatch.setenv(ENV_APPROVAL, "随便写个值")
    policy = resolve()
    assert policy.approval == DEFAULT_APPROVAL
    assert "非法" in policy.source


def test_old_no_hitl_switch_maps_to_never(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    policy = resolve(cli_hitl=False)
    assert policy.approval == "never"
    assert policy.hitl is False


def test_non_admin_role_forces_readonly(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    policy = resolve(role="viewer")
    assert policy.sandbox == "read-only"
    assert policy.write_tools is False
    assert policy.readonly is True
    assert any("非 admin" in w for w in policy.warnings)


def test_danger_mode_warns(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    policy = resolve(cli_sandbox="danger-full-access")
    assert policy.write_tools is True
    assert any("danger-full-access" in w for w in policy.warnings)


def test_describe_is_human_readable(monkeypatch, tmp_path):
    _no_toml(monkeypatch, tmp_path)
    text = describe(resolve(cli_approval="never", cli_sandbox="read-only"))
    assert "never" in text and "read-only" in text
