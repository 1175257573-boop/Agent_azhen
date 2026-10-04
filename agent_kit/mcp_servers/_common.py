"""三个 MCP Server 的公共部分：根目录解析与路径越界防护。

为什么要单独抽一层：
  MCP Server 是**独立进程**，工具参数来自模型 —— 也就是说，参数是不可信输入。
  `subdir="../../Windows/System32"` 这类穿越必须在一进入就被挡掉，
  否则「只读体检工具」会变成任意文件读取口子。

规则很简单：**任何路径都必须落在项目根目录内**，越界直接抛 ValueError，
由 fastmcp 转成工具错误返回给模型，不会让服务端进程崩掉。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# agent_kit/mcp_servers/xxx.py → 上两级是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 评审「别的仓库」时要能换根。为什么用环境变量而不是函数参数：
#   MCP Server 是独立进程，工具签名由模型调用、不便临时加参；
#   而 `subdir` 参数受越界校验保护，不能用来指向项目外的目录。
#   环境变量是进程级、一次性、可审计的换根方式。
ROOT_ENV_VAR = "ATLAS_REVIEW_ROOT"
_ROOT_OVERRIDE: Path | None = None


def _normalize_root(path: str | Path) -> Path:
    """把 `/e/WorkBuddy/...` 这类 POSIX 路径还原成 `E:\\WorkBuddy\\...`。

    为什么需要：在 Git Bash 里调 `python main.py review --path /e/foo`，
    Windows 上的 Python 会把 `/e/foo` 当成相对路径拼到当前盘符，
    解析出 `E:\\e\\foo` —— 目录不存在，评审直接失败。
    评审入口要接受各种 shell 传进来的路径，这一层转换必须在最前面做掉。
    """
    text = str(path)
    # 两种形式都要认：`/e/foo`（shell 原样传入）和 `\e\foo`（已经被 Path() 转过一道）
    match = re.match(r"^[/\\]([a-zA-Z])[/\\](.*)$", text) if os.name == "nt" else None
    if match:
        rest = match.group(2).replace("\\", "/")
        return Path(f"{match.group(1).upper()}:/{rest}")
    return Path(text)


def set_root(path: str | Path) -> Path:
    """把取证根目录切到另一个仓库；返回实际生效的根。

    只有先换根，「评审任意仓库」这个业务才成立 ——
    否则 14 个取证工具永远只能评本项目自己。
    """
    global _ROOT_OVERRIDE
    target = _normalize_root(path).resolve()
    if not target.is_dir():
        raise ValueError(f"目录不存在：{target}")
    _ROOT_OVERRIDE = target
    return target


def reset_root() -> None:
    """切回默认根（项目自身）。"""
    global _ROOT_OVERRIDE
    _ROOT_OVERRIDE = None


def project_root() -> Path:
    """当前生效的取证根目录：显式 set_root > 环境变量 > 项目自身。"""
    if _ROOT_OVERRIDE is not None:
        return _ROOT_OVERRIDE
    env = os.environ.get(ROOT_ENV_VAR)
    if env:
        target = Path(env).resolve()
        if target.is_dir():
            return target
    return PROJECT_ROOT

# 扫目录时必须跳过的：体量大且与工程质量无关
SKIP_DIRS = {
    ".venv",
    "venv",
    "runs",
    "__pycache__",
    ".git",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
}

# 只统计这些后缀，避免把二进制 / 锁文件算进代码量
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".vue", ".java", ".go", ".rs", ".sql", ".sh", ".yml", ".yaml", ".toml"}


def resolve_dir(subdir: str = ".") -> Path:
    """把相对路径解析成项目内的绝对目录，越界则报错。"""
    root = project_root().resolve()
    target = (root / subdir).resolve() if subdir and subdir != "." else root
    if target == root or root in target.parents:
        if target.is_dir():
            return target
        raise ValueError(f"目录不存在：{subdir}")
    raise ValueError(f"路径越界，只允许访问项目目录内：{subdir}")


def iter_code_files(root: Path, suffixes: set[str] | None = None):
    """递归产出项目内的代码文件（自动跳过 SKIP_DIRS）。"""
    wanted = suffixes or CODE_SUFFIXES
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in wanted:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def rel(path: Path, root: Path) -> str:
    """相对路径字符串，输出里用它——绝对路径会把本机目录结构泄漏给模型。

    换根之后 `path` 可能不在 `root` 之下（理论上不该发生：resolve_dir 已校验），
    这时退化成绝对路径而不是抛异常：取证工具因为路径问题崩掉是最差的结果，
    报告少一条路径好过整个 server 起不来。
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
