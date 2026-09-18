"""atlas.toml 配置层的测试。

重点测三件事：读得到、默认值兜得住、密钥字段被拒绝。
第三件最关键——配置文件是要进版本库的，漏一个密钥进去就是事故。
"""

from __future__ import annotations

from agent_kit import config as config_mod
from agent_kit import memory as mem
from agent_kit.config import AtlasConfig, load_atlas_config


def test_defaults_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: [tmp_path / "不存在.toml"])
    cfg = load_atlas_config(reload=True)
    assert cfg == AtlasConfig()
    assert cfg.memory.short_term == "sqlite"
    assert cfg.memory.long_term == "sqlite"


def test_toml_values_are_read(monkeypatch, tmp_path):
    path = tmp_path / "atlas.toml"
    path.write_text(
        'model_name = "qwen-max"\nmodel_provider = "dashscope"\n'
        '[memory]\nshort_term = "sqlite"\nlong_term = "postgres"\n'
        '[retrieval]\nname = "phrase"\n'
        '[sandbox]\nmode = "read-only"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: [path])
    cfg = load_atlas_config(reload=True)
    assert cfg.model_name == "qwen-max"
    assert cfg.model_provider == "dashscope"
    assert cfg.memory.long_term == "postgres"
    assert cfg.retrieval.name == "phrase"
    assert cfg.sandbox.mode == "read-only"


def test_secret_field_falls_back_to_defaults(monkeypatch, tmp_path):
    path = tmp_path / "atlas.toml"
    path.write_text(
        'model_name = "qwen-max"\n[memory]\napi_key = "sk-不应该写在这里"\n', encoding="utf-8"
    )
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: [path])
    cfg = load_atlas_config(reload=True)
    # 校验失败 → 整份配置作废，回落到默认值（宁可全默认，也不能把密钥读进来）
    assert cfg == AtlasConfig()
    assert cfg.model_name is None


def test_bad_enum_falls_back_to_defaults(monkeypatch, tmp_path):
    path = tmp_path / "atlas.toml"
    path.write_text('[memory]\nshort_term = "mongodb"\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: [path])
    cfg = load_atlas_config(reload=True)
    assert cfg.memory.short_term == "sqlite"


def test_memory_backend_priority(monkeypatch, tmp_path):
    """环境变量 > atlas.toml > 默认 sqlite。"""
    monkeypatch.setattr(config_mod, "config_search_paths", lambda: [tmp_path / "不存在.toml"])
    load_atlas_config(reload=True)

    monkeypatch.delenv("SHORT_TERM_BACKEND", raising=False)
    monkeypatch.delenv("LONG_TERM_BACKEND", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert mem.short_backend() == "sqlite"
    assert mem.long_backend() == "sqlite"

    monkeypatch.setenv("SHORT_TERM_BACKEND", "memory")
    assert mem.short_backend() == "memory"
