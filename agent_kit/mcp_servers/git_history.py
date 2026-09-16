"""MCP Server · 版本控制只读查询（Git）。

刻意**只提供只读操作**：状态、日志、变更统计、贡献者。
不提供 commit / push / reset —— Agent 不该在无人确认的情况下改写版本历史，
写操作留给人类在终端里做。这是「能力边界」比「能力多少」更重要的典型例子。

实现要点：
  · 全部走 `subprocess` 调 git，不引入 GitPython（少一个依赖，也少一个版本雷区）
  · 显式 `encoding="utf-8", errors="replace"`：Windows 中文提交信息不这么处理会乱码
  · 超时 15 秒：仓库异常大时不能把 MCP 进程挂死

启动：
    python agent_kit/mcp_servers/git_history.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 以脚本方式启动时（MCP 客户端就是这么拉起本文件的），项目根不在 sys.path 里，
# 不补这一行会 ModuleNotFoundError，且客户端只会看到 "Connection closed"。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agent_kit.mcp_servers._common import PROJECT_ROOT, resolve_dir

mcp = FastMCP("atlas-git-mcp")

TIMEOUT = 15


def _git(root, *args: str) -> str:
    """在项目目录里执行 git 命令；非 0 退出码转成可读错误，不让异常冒到协议层。"""
    # check=False 是刻意的：git 的非 0 退出码（如没有上游分支）由下面转成可读文本，
    # 抛异常只会让整个 MCP 工具调用失败。
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        return f"[git {args[0]} 失败] {(proc.stderr or proc.stdout).strip()}"
    return proc.stdout.strip()


def _is_repo(root) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"


# ---------------------------------------------------------------------------
# 工具 1：仓库状态
# ---------------------------------------------------------------------------
def git_status(subdir: str = ".") -> dict:
    """查看当前分支、是否有未提交变更、领先/落后远端多少次。

    动手改代码前先看一眼，避免在脏工作区上叠加改动。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    if not _is_repo(root):
        return {"is_repo": False, "message": "当前目录不是 Git 仓库"}

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    # porcelain 每行格式是「XY 路径」：前 2 位是状态码，第 3 位是空格。
    # 用 [2:].strip() 而不是 [3:] —— 重命名行会写成 "R  old -> new"，
    # 固定切 3 位在个别状态组合下会把路径首字符吃掉。
    porcelain = _git(root, "status", "--porcelain")
    changed = []
    for line in porcelain.splitlines():
        entry = line[2:].strip()
        if not entry:
            continue
        changed.append(entry.split(" -> ")[-1].strip('"'))

    # 没有上游分支时 git 会报错（未 push 过 / 未 fetch），_git 把错误也当字符串返回，
    # 这里必须校验是不是纯数字，否则会把错误信息当计数解析而崩掉。
    ahead_behind = _git(root, "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD")
    ahead = behind = 0
    parts = ahead_behind.split()
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        behind, ahead = int(parts[0]), int(parts[1])

    return {
        "is_repo": True,
        "branch": branch,
        "clean": not changed,
        "changed_files": len(changed),
        "changed_sample": changed[:15],
        "ahead": ahead,
        "behind": behind,
    }


# ---------------------------------------------------------------------------
# 工具 2：提交历史
# ---------------------------------------------------------------------------
def git_log(subdir: str = ".", limit: int = 10) -> dict:
    """查看最近的提交记录（哈希、作者、时间、标题）。

    用于回答「这个项目最近在做什么」「这个改动是哪次提交引入的」。

    Args:
        subdir: 相对项目根目录的子目录
        limit: 返回条数，默认 10
    """
    root = resolve_dir(subdir)
    if not _is_repo(root):
        return {"is_repo": False, "message": "当前目录不是 Git 仓库"}

    sep = "\x1f"
    raw = _git(root, "log", f"-{limit}", f"--pretty=format:%h{sep}%an{sep}%ad{sep}%s", "--date=short")
    commits = []
    for line in raw.splitlines():
        parts = line.split(sep)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return {"is_repo": True, "count": len(commits), "commits": commits}


# ---------------------------------------------------------------------------
# 工具 3：变更统计
# ---------------------------------------------------------------------------
def git_diff_stat(subdir: str = ".") -> dict:
    """统计当前未提交改动的增删行数（按文件）。

    「改了多少」比「改了几个文件」更能说明改动风险。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    if not _is_repo(root):
        return {"is_repo": False, "message": "当前目录不是 Git 仓库"}

    raw = _git(root, "diff", "--stat", "HEAD")
    files: list[dict] = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        name, _, rest = line.partition("|")
        files.append({"file": name.strip(), "changes": rest.strip()})
    return {"is_repo": True, "files": files, "count": len(files)}


# ---------------------------------------------------------------------------
# 工具 4：贡献者
# ---------------------------------------------------------------------------
def git_contributors(subdir: str = ".", limit: int = 10) -> dict:
    """按提交数列出主要贡献者。

    Args:
        subdir: 相对项目根目录的子目录
        limit: 返回条数
    """
    root = resolve_dir(subdir)
    if not _is_repo(root):
        return {"is_repo": False, "message": "当前目录不是 Git 仓库"}

    raw = _git(root, "shortlog", "-sn", "--all")
    people = []
    for line in raw.splitlines():
        count, _, name = line.strip().partition("\t")
        if name:
            people.append({"commits": int(count), "name": name})
    return {"is_repo": True, "contributors": people[:limit]}


# ---------------------------------------------------------------------------
# 工具 5：按关键词找提交
# ---------------------------------------------------------------------------
def git_search_commits(keyword: str, subdir: str = ".", limit: int = 10) -> dict:
    """在提交信息里搜索关键词，定位「某个功能/修复是哪次提交做的」。

    Args:
        keyword: 搜索关键词
        subdir: 相对项目根目录的子目录
        limit: 返回条数
    """
    root = resolve_dir(subdir)
    if not _is_repo(root):
        return {"is_repo": False, "message": "当前目录不是 Git 仓库"}

    raw = _git(root, "log", f"-{limit}", "--oneline", "--grep", keyword, "-i")
    commits = []
    for line in raw.splitlines():
        hash_, _, subject = line.partition(" ")
        if hash_:
            commits.append({"hash": hash_, "subject": subject})
    return {"is_repo": True, "keyword": keyword, "count": len(commits), "commits": commits}


@mcp.resource("git://policy")
def git_policy() -> str:
    """本 Server 的能力边界（MCP 资源）。"""
    return "\n".join(
        [
            "# Git MCP 使用边界",
            "",
            "只提供只读查询：状态 / 日志 / 变更统计 / 贡献者 / 提交搜索。",
            "不提供 commit、push、reset、checkout 等任何写操作——版本历史由人类决定。",
            f"仓库根目录：{PROJECT_ROOT.name}",
        ]
    )


# ---------------------------------------------------------------------------
# 注册：先写普通函数、最后统一注册（便于单元测试直接调用）
# ---------------------------------------------------------------------------
for _fn in (git_status, git_log, git_diff_stat, git_contributors, git_search_commits):
    mcp.tool(_fn)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = 8012
        for arg in args:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")
