"""三个 MCP Server 的公共部分：根目录解析与路径越界防护。

为什么要单独抽一层：
  MCP Server 是**独立进程**，工具参数来自模型 —— 也就是说，参数是不可信输入。
  `subdir="../../Windows/System32"` 这类穿越必须在一进入就被挡掉，
  否则「只读体检工具」会变成任意文件读取口子。

规则很简单：**任何路径都必须落在项目根目录内**，越界直接抛 ValueError，
由 fastmcp 转成工具错误返回给模型，不会让服务端进程崩掉。
"""

from __future__ import annotations

from pathlib import Path

# agent_kit/mcp_servers/xxx.py → 上两级是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

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
    root = PROJECT_ROOT.resolve()
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
    """相对路径字符串，输出里用它——绝对路径会把本机目录结构泄漏给模型。"""
    return path.relative_to(root).as_posix()
