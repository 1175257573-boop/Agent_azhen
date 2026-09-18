"""记忆产物的 git 基线（对标 Codex：`~/.codex/memories/.git` 做 workspace diff）。

为什么要在 memories 目录里塞一个 git 仓库：
    Phase 2 的合并本质上是「读一堆原始记忆 → 重写 MEMORY.md」。如果没有基线，
    每次都得全量重读、全量重写，既不知道这次到底变了什么，也没法判断「其实没变」。
    Codex 的做法是先落一个基线 commit，同步产物后做 workspace diff，把**变更清单**
    交给 consolidation agent —— 本项目照抄这个结构，带来两个实际收益：
      1. 合并时能告诉模型「这次新增/修改了哪些会话」，合成结果更贴合增量
      2. 产物相对基线没有变化时**直接跳过 LLM 调用**（省一次请求，也不会反复重写文件）

git 不可用时**明确报出来**，由调用方写进 report 决定降级——不假装成功。
这个仓库始终是本地家目录里的小仓库，不 push 到任何远端。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# 提交身份走命令行参数注入而不是依赖全局 git config：
# CI / 容器里通常没配 user.email，直接 commit 会失败
_IDENTITY = ("user.name=atlas", "user.email=atlas@localhost")
# 行尾统一 LF：Windows 默认 core.autocrlf=true 会让签进去的内容来回抖
_EOL = ("core.autocrlf=false", "core.safecrlf=false")

_TIMEOUT = 30


def git_available() -> bool:
    """git 在不在 PATH 里。"""
    return shutil.which("git") is not None


def _run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """跑一条 git 命令。始终用列表参数（不拼字符串），避免路径里有空格出岔子。"""
    options: list[str] = []
    for setting in (*_IDENTITY, *_EOL):
        options.extend(("-c", setting))
    process = subprocess.run(
        ["git", *options, *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=_TIMEOUT,
    )
    if check and process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"git {args[0]} 失败")
    return process


def is_repo(root: Path | str) -> bool:
    return (Path(root) / ".git").exists()


def ensure_repo(root: Path | str) -> tuple[bool, str]:
    """确保 memories 目录是个 git 仓库。返回 (可用, 说明)。

    不可用一定要把原因带回去，让调用方写进运行报告。
    """
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    if not git_available():
        return False, "没有找到 git 可执行文件，跳过基线 diff"
    if is_repo(directory):
        return True, "已存在基线仓库"

    try:
        _run(directory, "init", "--quiet")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return False, f"初始化基线仓库失败：{exc}"

    attributes = directory / ".gitattributes"
    if not attributes.exists():
        attributes.write_text("* text=auto eol=lf\n", encoding="utf-8")
    return True, "已新建基线仓库"


def changes(root: Path | str) -> list[tuple[str, str]]:
    """相对 HEAD 的工作区变更：[("added"|"modified"|"deleted", 相对路径)]。

    用 `status --porcelain` 而不是 `diff`：新产物文件是未跟踪状态，diff 看不见。
    """
    directory = Path(root)
    if not git_available() or not is_repo(directory):
        return []
    try:
        result = _run(directory, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return []

    items: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:  # 状态两位 + 空格 + 至少一个字符的路径
            continue
        code = line[:2]
        path = line[3:]
        if " -> " in path:   # 重命名：取新名字
            path = path.split(" -> ")[-1]
        items.append((_classify(code), path))
    return items


def _classify(code: str) -> str:
    if code.startswith("??"):
        return "added"
    if code[1] == "D" or code[0] == "D":
        return "deleted"
    return "modified"


def snapshot(root: Path | str, message: str) -> bool:
    """把当前产物固化成一个 commit。没有变化就什么都不做并返回 False。

    区分「有变更」和「没变更」正是这套机制的价值所在，所以返回值要给准。
    """
    directory = Path(root)
    if not git_available() or not is_repo(directory):
        return False
    if not changes(directory):
        return False
    try:
        _run(directory, "add", "-A")
        # 空 commit 会以 rc=1 退出并打印 "nothing to commit"，这里按返回码判断而不是抛错
        outcome = _run(directory, "commit", "--quiet", "-m", message, check=False)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise RuntimeError(f"落基线快照失败：{exc}") from exc
    return outcome.returncode == 0


def describe(items: list[tuple[str, str]]) -> str:
    """把变更清单渲染成能给模型看的几行文本。"""
    if not items:
        return "（无变化）"
    labels = {"added": "新增", "modified": "修改", "deleted": "删除"}
    return "\n".join(f"  - {labels.get(kind, kind)} {path}" for kind, path in items)


def history(root: Path | str, *, limit: int = 5) -> list[str]:
    """看最近几次快照的提交说明（排查用）。"""
    directory = Path(root)
    if not git_available() or not is_repo(directory):
        return []
    try:
        result = _run(directory, "log", f"-{limit}", "--format=%h %ad %s", "--date=iso-strict")
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]
