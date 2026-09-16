"""MCP Server · 文档一致性审计（只读）：README 结构、目录锚点、CHANGELOG、必备文件。

为什么值得单独做一个 server：
  文档腐烂是**静默**的——代码改了、README 没改，CI 也不会红，只有新人和面试官会发现。
  把「文档是否与工程一致」变成可调用的检查项，它才会被定期执行。

能查的四件事：
  1. README 的章节结构（目录里写了哪些章、正文里实际有哪些标题）
  2. 目录锚点是否真的存在（GitHub 中文标题的 slug 极不可靠，本项目用显式 <a id>）
  3. CHANGELOG 有没有 Unreleased 段（改了代码却没记，等于版本历史缺了一块）
  4. 开源项目的必备文件是否齐全（LICENSE / README / CONTRIBUTING / SECURITY / CI）

启动：
    python agent_kit/mcp_servers/doc_audit.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 以脚本方式启动时（MCP 客户端就是这么拉起本文件的），项目根不在 sys.path 里，
# 不补这一行会 ModuleNotFoundError，且客户端只会看到 "Connection closed"。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agent_kit.mcp_servers._common import PROJECT_ROOT, SKIP_DIRS, resolve_dir

mcp = FastMCP("atlas-docs-mcp")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
ANCHOR_RE = re.compile(r"<a\s+id=\"([^\"]+)\"", re.IGNORECASE)
LINK_RE = re.compile(r"\[[^\]]+\]\(#([^)]+)\)")

# 对外公开项目应有的门面文件
EXPECTED_FILES = ["README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", ".gitignore", ".github/workflows"]


# ---------------------------------------------------------------------------
# 工具 1：README 章节结构
# ---------------------------------------------------------------------------
def readme_outline(subdir: str = ".", max_depth: int = 3) -> dict:
    """列出 README 的标题结构（层级 + 标题 + 行号）。

    用它判断「文档结构是否还和实际内容对得上」。

    Args:
        subdir: 相对项目根目录的子目录
        max_depth: 最多展开到第几级标题
    """
    root = resolve_dir(subdir)
    readme = root / "README.md"
    if not readme.exists():
        return {"found": False, "message": "未找到 README.md"}

    text = readme.read_text(encoding="utf-8", errors="ignore")
    headings = []
    for match in HEADING_RE.finditer(text):
        level = len(match.group(1))
        if level > max_depth:
            continue
        headings.append({"level": level, "title": match.group(2), "line": text[: match.start()].count("\n") + 1})
    return {"found": True, "headings": headings, "count": len(headings)}


# ---------------------------------------------------------------------------
# 工具 2：锚点校验
# ---------------------------------------------------------------------------
def check_anchors(subdir: str = ".") -> dict:
    """校验 README 里的 `[章节](#锚点)` 是否都有对应的 `<a id="...">`。

    中文标题交给 GitHub 自动生成 slug 会失效（标点被吞、编码不一致），
    本项目统一改成显式 `<a id>`，这个工具就是用来防止锚点腐烂的。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    readme = root / "README.md"
    if not readme.exists():
        return {"found": False, "message": "未找到 README.md"}

    text = readme.read_text(encoding="utf-8", errors="ignore")
    anchors = set(ANCHOR_RE.findall(text))
    links = LINK_RE.findall(text)
    broken = sorted({link for link in links if link not in anchors})
    return {
        "found": True,
        "anchors": len(anchors),
        "links": len(links),
        "broken": broken,
        "verdict": "全部命中" if not broken else f"{len(broken)} 个锚点失效",
    }


# ---------------------------------------------------------------------------
# 工具 3：CHANGELOG 状态
# ---------------------------------------------------------------------------
def changelog_status(subdir: str = ".") -> dict:
    """检查 CHANGELOG 是否存在、有没有 Unreleased 段、最近几个版本号。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    path = root / "CHANGELOG.md"
    if not path.exists():
        return {"found": False, "message": "未找到 CHANGELOG.md"}

    text = path.read_text(encoding="utf-8", errors="ignore")
    versions = re.findall(r"^##\s+\[([^\]]+)\]", text, re.MULTILINE)
    unreleased = bool(re.search(r"^##\s+\[?Unreleased", text, re.MULTILINE | re.IGNORECASE))
    return {
        "found": True,
        "has_unreleased": unreleased,
        "versions": versions[:8],
        "latest": versions[0] if versions else None,
        "verdict": "有未发布条目" if unreleased else "无 Unreleased 段，改动可能没被记录",
    }


# ---------------------------------------------------------------------------
# 工具 4：必备文件检查
# ---------------------------------------------------------------------------
def project_checklist(subdir: str = ".") -> dict:
    """检查开源项目该有的门面文件是否齐全。

    缺 LICENSE 的项目在法律上默认是「保留所有权利」，别人不能合法使用——
    这是最容易被忽略、后果却最重的一项。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    missing = [name for name in EXPECTED_FILES if not (root / name).exists()]
    return {
        "root": root.name,
        "expected": EXPECTED_FILES,
        "present": [name for name in EXPECTED_FILES if (root / name).exists()],
        "missing": missing,
        "verdict": "齐全" if not missing else f"缺少 {len(missing)} 项：{', '.join(missing)}",
    }


# ---------------------------------------------------------------------------
# 工具 5：目录树
# ---------------------------------------------------------------------------
def project_tree(subdir: str = ".", depth: int = 2, max_entries: int = 60) -> dict:
    """打印项目目录结构（自动跳过 .venv / .git / runs 等噪音目录）。

    给模型讲项目结构时用它，比手写一段描述准确——描述会过期，树不会。

    Args:
        subdir: 相对项目根目录的子目录
        depth: 展开层级
        max_entries: 最多输出多少条
    """
    root = resolve_dir(subdir)
    lines: list[str] = [f"{root.name}/"]
    count = 0

    def walk(cur: Path, prefix: str, level: int) -> None:
        nonlocal count
        if level > depth or count > max_entries:
            return
        entries = sorted(p for p in cur.iterdir() if p.name not in SKIP_DIRS and not p.name.startswith("."))
        dirs = [p for p in entries if p.is_dir()]
        files = [p for p in entries if p.is_file()]
        for p in dirs + files:
            if count > max_entries:
                return
            lines.append(f"{prefix}{p.name}{'/' if p.is_dir() else ''}")
            count += 1
            if p.is_dir():
                walk(p, prefix + "  ", level + 1)

    walk(root, "  ", 1)
    return {"root": root.name, "tree": "\n".join(lines), "truncated": count > max_entries}


@mcp.resource("docs://policy")
def docs_policy() -> str:
    """文档一致性判定口径（MCP 资源）。"""
    return "\n".join(
        [
            "# 文档一致性口径",
            "",
            "1. README 的章节数与目录条目数应一致，新增章节必须同步目录。",
            "2. 所有 `#锚点` 必须有对应 `<a id>`，中文标题不依赖 GitHub 自动 slug。",
            "3. 任何用户可见改动都要进 CHANGELOG 的 Unreleased。",
            "4. 必备文件缺失即为不合格，尤其 LICENSE。",
            f"项目根：{PROJECT_ROOT.name}",
        ]
    )


# ---------------------------------------------------------------------------
# 注册：先写普通函数、最后统一注册（便于单元测试直接调用）
# ---------------------------------------------------------------------------
for _fn in (readme_outline, check_anchors, changelog_status, project_checklist, project_tree):
    mcp.tool(_fn)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = 8013
        for arg in args:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")
