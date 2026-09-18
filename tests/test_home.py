"""ATLAS_HOME 家目录解析的测试。

家目录是记忆/会话的落盘根，解析错了后果是「数据写到了意想不到的地方」，
所以默认值、环境变量覆盖、目录创建这三件事都要有测试兜住。
"""

from __future__ import annotations

from pathlib import Path

from agent_kit import home


def test_default_is_under_user_home(monkeypatch):
    monkeypatch.delenv(home.ENV_ATLAS_HOME, raising=False)
    assert home.atlas_home() == Path.home() / ".atlas"


def test_env_overrides_default(monkeypatch, tmp_path):
    target = tmp_path / "custom-home"
    monkeypatch.setenv(home.ENV_ATLAS_HOME, str(target))
    assert home.atlas_home() == target


def test_ensure_dirs_creates_all_subdirs(tmp_path):
    lay = home.layout(tmp_path / "root").ensure_dirs()
    for path in (lay.root, lay.sessions_dir, lay.memories_dir, lay.logs_dir):
        assert path.is_dir(), f"{path} 未被创建"


def test_db_path_follows_home(monkeypatch, tmp_path):
    monkeypatch.setenv(home.ENV_ATLAS_HOME, str(tmp_path))
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    assert home.resolve_db_path() == tmp_path / "atlas.db"


def test_sqlite_path_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(home.ENV_ATLAS_HOME, str(tmp_path))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "other.db"))
    assert home.resolve_db_path() == tmp_path / "other.db"
