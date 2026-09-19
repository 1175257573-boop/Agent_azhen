"""钉住三个 MCP Server 的行为（直接调逻辑函数，不走协议）。

为什么测逻辑函数而不是走协议：
  协议链路（子进程 + 握手）慢且依赖进程环境，适合放在 examples 的演示里；
  单元测试要的是**快且稳**，所以 server 里刻意把「逻辑」与「注册」分开写
  （见各文件末尾的 `for _fn in (...): mcp.tool(_fn)`）。

重点防三类回归：
  1. 路径越界防护被改坏 → 「只读体检工具」变成任意文件读取
  2. 密钥扫描输出原文 → 工具本身成为泄密通道
  3. 本机路径泄漏 → 输出里出现绝对路径
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agent_kit.builtin_skills import BUILTIN_SKILLS, PROJECT_ENGINEERING
from agent_kit.mcp_client import ALL_SERVERS
from agent_kit.mcp_servers import doc_audit, git_history, quality
from agent_kit.mcp_servers._common import PROJECT_ROOT, resolve_dir


# ---------------------------------------------------------------------------
# 路径防护
# ---------------------------------------------------------------------------
def test_resolve_dir_accepts_project_subdir():
    assert resolve_dir("agent_kit").is_dir()


@pytest.mark.parametrize("bad", ["../..", "../../etc", "/etc", "..", "agent_kit/../../.."])
def test_resolve_dir_rejects_escaping(bad):
    """越界必须抛错——这是所有只读工具的底线。"""
    with pytest.raises(ValueError):
        resolve_dir(bad)


def test_resolve_dir_rejects_missing():
    with pytest.raises(ValueError):
        resolve_dir("no_such_dir_xyz")


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------
def test_code_stats_reports_scale():
    stats = quality.project_code_stats()
    assert stats["files"] > 0
    assert stats["total_lines"] > stats["code_lines"] > 0
    assert stats["files"] == sum(stats["by_ext"].values())


def test_debt_markers_respect_limit():
    scan = quality.scan_debt_markers(limit=2)
    assert len(scan["hits"]) <= 2
    assert scan["total"] >= len(scan["hits"])


def test_secrets_never_leak_raw_value():
    """输出必须是打码片段，绝不能出现原文。"""
    scan = quality.scan_secrets(limit=20)
    for hit in scan["hits"] + scan["local_sample"]:
        assert hit["masked"].endswith("*")
        assert len(hit["masked"]) < 80


def test_secrets_downgrades_localhost_defaults():
    """localhost 默认凭据不算泄密，但必须单独计数提示。"""
    scan = quality.scan_secrets()
    assert "local_defaults" in scan
    for item in scan["local_sample"]:
        assert "file" in item and "line" in item


def test_secrets_catches_underscored_password(tmp_path, monkeypatch):
    """`DB_PASSWORD = "..."` 这种最常见写法必须命中。

    曾经漏报：正则给 password 加了 `\b` 前缀，而 `DB_PASSWORD` 里
    `PASSWORD` 前是下划线 —— 下划线属于单词字符，`\b` 在此不成立。
    是评审真实仓库时实测发现的，这条用例就是那个坑的钉子。
    """
    from agent_kit.mcp_servers import _common

    (tmp_path / "app.py").write_text('DB_PASSWORD = "Tr0ub4dor&3xK"\n', encoding="utf-8")
    monkeypatch.setattr(_common, "_ROOT_OVERRIDE", tmp_path)

    scan = quality.scan_secrets()
    assert scan["count"] >= 1
    assert any("password" in hit["kind"] or "口令" in hit["kind"] for hit in scan["hits"])


def test_checklist_accepts_common_filename_aliases(tmp_path, monkeypatch):
    """`LICENSE.txt` / `CHANGES.md` 是标准写法，不能判成缺失。

    曾经误报：期望清单只写死 `LICENSE` 和 `CHANGELOG.md`，
    pallets/click（用 LICENSE.txt + CHANGES.md）被评成「缺 LICENSE」。
    """
    from agent_kit.mcp_servers import _common

    (tmp_path / "LICENSE.txt").write_text("MIT", encoding="utf-8")
    (tmp_path / "CHANGES.md").write_text("# changes", encoding="utf-8")
    monkeypatch.setattr(_common, "_ROOT_OVERRIDE", tmp_path)

    checklist = doc_audit.project_checklist()
    assert "LICENSE" in checklist["present"]
    assert "CHANGELOG.md" in checklist["present"]
    assert "LICENSE" not in checklist["missing"]


def test_set_root_accepts_git_bash_paths(tmp_path, monkeypatch):
    """`--path /e/WorkBuddy/foo`（Git Bash 写法）必须能落到 `E:\\WorkBuddy\\foo`。

    否则 Windows Python 会解析成 `E:\\e\\WorkBuddy\\foo`，评审直接报目录不存在。
    """
    from agent_kit.mcp_servers import _common

    try:
        if os.name == "nt":
            drive = Path(tmp_path).drive or "C:"
            posix = "/" + drive[0].lower() + "/" + str(tmp_path).replace(drive, "").lstrip("\\/")
            assert _common.set_root(posix) == tmp_path.resolve()
        else:
            assert _common.set_root(str(tmp_path)) == tmp_path.resolve()
    finally:
        _common.reset_root()


def test_dependency_audit_understands_extras(tmp_path, monkeypatch):
    """`psycopg[binary]==3.3.5` 是钉了版本的，不能算未钉。

    曾经误报：判定时用 `line.split("[")[0]` 去 extras，把 `==3.3.5` 一起砍没了。
    是评审本项目自己时实测发现的，这条用例就是那个坑的钉子。
    """
    from agent_kit.mcp_servers import _common

    (tmp_path / "requirements.txt").write_text(
        "psycopg[binary]==3.3.5\nuvicorn[standard]==0.53.0\nflask\n", encoding="utf-8"
    )
    monkeypatch.setattr(_common, "_ROOT_OVERRIDE", tmp_path)

    audit = quality.dependency_audit()
    assert audit["pinned"] == 2
    assert audit["unpinned"] == ["flask"]


def test_check_tests_finds_suite():
    tests = quality.check_tests()
    assert tests["has_tests_dir"] is True
    assert tests["test_files"] > 0
    assert tests["test_cases"] > 0


def test_dependency_audit_shape():
    dep = quality.dependency_audit()
    assert dep["found"] is True
    assert dep["total"] >= dep["pinned"]


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def test_git_status_on_real_repo():
    status = git_history.git_status()
    assert status["is_repo"] is True
    assert status["branch"]
    assert isinstance(status["changed_files"], int)


def test_git_log_returns_commits():
    log = git_history.git_log(limit=3)
    assert log["is_repo"] is True
    assert log["count"] >= 1
    assert {"hash", "author", "date", "subject"} <= set(log["commits"][0])


def test_git_search_commits_tolerates_no_match():
    """没匹配到不能崩，count 为 0 即可。"""
    found = git_history.git_search_commits("不存在的关键词zzz")
    assert found["count"] == 0


def test_git_ahead_behind_never_crashes_on_missing_upstream():
    """无上游分支时 rev-list 返回的是错误信息，不能当数字解析。"""
    status = git_history.git_status()
    assert isinstance(status["ahead"], int)
    assert isinstance(status["behind"], int)


# ---------------------------------------------------------------------------
# docs
# ---------------------------------------------------------------------------
def test_readme_outline_has_headings():
    outline = doc_audit.readme_outline()
    assert outline["found"] is True
    assert outline["count"] > 5


def test_all_readme_anchors_resolve():
    """中文标题靠 GitHub 自动 slug 不可靠，本项目用显式 <a id>，这里防止锚点腐烂。"""
    result = doc_audit.check_anchors()
    assert result["found"] is True
    assert result["broken"] == []


def test_changelog_has_unreleased():
    result = doc_audit.changelog_status()
    assert result["found"] is True
    assert result["has_unreleased"] is True


def test_project_checklist_complete():
    result = doc_audit.project_checklist()
    assert result["missing"] == []


def test_project_tree_hides_noise():
    tree = doc_audit.project_tree(depth=2)
    # 目录名必须动态取：CI 里 checkout 出来的目录是仓库名，不是本机的项目目录名。
    # 硬编码目录名的测试在 CI 上必挂——这是「测试依赖本机环境」的典型形态。
    assert tree["tree"].startswith(f"{PROJECT_ROOT.name}/")
    assert "agent_kit/" in tree["tree"]
    # 噪音目录与隐藏文件都不该出现，否则模型收到的结构图会失真
    assert ".venv" not in tree["tree"]
    assert ".git" not in tree["tree"]


# ---------------------------------------------------------------------------
# 输出不泄漏本机路径
# ---------------------------------------------------------------------------
def test_outputs_use_relative_paths_only():
    combined = str(quality.scan_debt_markers(limit=5)) + str(doc_audit.project_tree(depth=1))
    assert str(PROJECT_ROOT).replace("\\", "/") not in combined


# ---------------------------------------------------------------------------
# Skill 与注册
# ---------------------------------------------------------------------------
def test_engineering_skill_has_three_layers():
    assert PROJECT_ENGINEERING["name"]
    assert PROJECT_ENGINEERING["description"]
    assert len(PROJECT_ENGINEERING["content"]) > 500
    assert PROJECT_ENGINEERING in BUILTIN_SKILLS


def test_engineering_skill_points_at_mcp_tools():
    """Skill 必须明确指向取证工具，否则又变成空谈规范。"""
    content = PROJECT_ENGINEERING["content"]
    for tool in ("project_code_stats", "scan_secrets", "git_status", "check_anchors"):
        assert tool in content


def test_all_registered_servers_exist():
    """注册表里的文件必须真实存在——写错路径要在这里炸，而不是运行时 Connection closed。"""
    for name, path in ALL_SERVERS.items():
        assert path.exists(), f"{name} 的 server 文件不存在：{path}"


def test_logic_functions_stay_plain_callables():
    """注册后原函数必须仍可直调——@mcp.tool 会把它包成 FunctionTool（带 .fn），那样就测不了了。"""
    for fn in (quality.project_code_stats, git_history.git_status, doc_audit.check_anchors):
        assert callable(fn)
        assert not hasattr(fn, "fn")


def test_secret_patterns_are_compiled():
    assert all(hasattr(p, "search") for _, p in quality.SECRET_PATTERNS)
    assert re.search(r"@localhost", "postgresql://u:p@localhost:5432/db") is not None
