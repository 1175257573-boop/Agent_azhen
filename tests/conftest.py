"""共享测试夹具。

所有测试都必须能在**没有 Redis / PostgreSQL / 真实 API Key** 的机器上跑通过：
    pytest -q
这是刻意的设计——CI 里不方便起外部服务，单元测试也不该依赖它们。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """每个用例都在干净环境里跑，避免读到开发机的 Redis / PG / Key。

    同时把 CWD 切到临时目录，防止测试里的写工具真的落到项目里。
    """
    for name in ("REDIS_URL", "PG_DSN", "POSTGRES_URL", "DATABASE_URL",
                 "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
                 "DASHSCOPE_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORY_STRICT", "0")
    # 家目录必须隔离：忘记传 db_path / memories_dir 的用例会真的写到 ~/.atlas，
    # 而 Phase 2 还会在那里 git init —— 落到开发机上就是凭空多出一个仓库
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "atlas-home"))
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def notes_dir(project_root) -> Path:
    return project_root / "notes"


def pytest_configure(config):
    os.environ.setdefault("PYTEST_RUNNING", "1")
