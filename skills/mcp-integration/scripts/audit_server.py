#!/usr/bin/env python3
"""MCP Server 源码只读审查。

接入第三方 MCP Server 之前先跑一遍，把静态问题排掉。
只读取、不联网、不修改任何文件、不用第三方库。

用法:
    python audit_server.py <server.py 或目录>
    python audit_server.py <目录> --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".venv", "venv", "env", "node_modules", "__pycache__", ".git",
    "site-packages", ".mypy_cache", ".ruff_cache", "dist", "build",
}

# 与标准库重名的模块会被优先导入，症状是第三方库 import 失败
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "queue", "types", "select", "json", "http", "logging", "token",
    "copy", "enum", "abc", "io", "os", "re", "sys", "time", "socket",
}

# 工具名按 _ 分词后，命中这些「词」才说明有副作用。
# 用整词匹配而非子串：否则 git_search_commits 会因含 commit 被误判成写操作。
WRITE_TOKENS = {
    "write", "writes", "save", "saves", "create", "creates", "update", "updates",
    "delete", "deletes", "remove", "removes", "drop", "drops", "send", "sends",
    "push", "pushes", "commit", "exec", "execute", "run", "kill", "start",
    "stop", "modify", "insert", "append", "move", "rename", "install",
    "post", "put", "patch", "trigger", "launch", "apply",
}

PLACEHOLDER = re.compile(
    r"^(your|xxx+|\.\.\.|<|\$\{|os\.environ|getenv|example|placeholder|dummy|changeme)",
    re.IGNORECASE,
)

SECRET_ASSIGN = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\s*[:=]\s*[\"']([^\"']{8,})[\"']",
    re.IGNORECASE,
)
SECRET_LITERAL = re.compile(r"sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
CONN_WITH_PWD = re.compile(r"[a-z]+://[^/\s:@]+:[^/\s:@]+@(?!(localhost|127\.0\.0\.1|\[::1\]))", re.IGNORECASE)

PATH_PARAM = re.compile(r"\b(path|dir|directory|subdir|folder|root|target|file)\b", re.IGNORECASE)
GUARD = re.compile(r"resolve\(|relative_to\(|is_relative_to\(|startswith\(|commonpath\(")
DEF_RE = re.compile(r"^def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)


def strip_comments(src: str) -> str:
    """去掉行注释。

    不做这一步会把「注释里提到的 @mcp.tool」当成真的装饰器——
    本项目 quality.py 就有一行注释写着「不用 @mcp.tool 装饰器的原因」。
    """
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def iter_py_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".py" else []
    out = []
    for p in target.rglob("*.py"):
        if not any(part in SKIP_DIRS for part in p.parts):
            out.append(p)
    return sorted(out)


class Report:
    def __init__(self) -> None:
        self.findings: list[dict] = []
        self.tools: list[str] = []
        self.unidentified = False

    def add(self, level: str, check: str, detail: str, where: str = "") -> None:
        self.findings.append({"level": level, "check": check, "detail": detail, "where": where})

    def worst(self, check: str) -> str:
        levels = [f["level"] for f in self.findings if f["check"] == check]
        for lv in ("FAIL", "WARN"):
            if lv in levels:
                return lv
        return "PASS" if levels else "SKIP"


def rel(p: Path, root: Path) -> str:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return p.name


def check_shadowing(files: list[Path], root: Path, rep: Report) -> None:
    hits = [f for f in files if f.stem in STDLIB]
    if hits:
        rep.add(
            "FAIL", "标准库遮蔽",
            "模块名与标准库重名：" + ", ".join(f"{rel(f, root)}({f.stem})" for f in hits[:5])
            + " —— 症状是第三方库 import 到你的模块，报错里会出现自己项目的路径",
        )
    else:
        rep.add("PASS", "标准库遮蔽", "未发现与标准库同名的模块")


def check_sys_path(files: list[Path], root: Path, rep: Report) -> None:
    """以脚本方式启动时 sys.path[0] 是脚本目录，导入项目内模块需要显式补路径。"""
    bad = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names = set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", src, re.MULTILINE))
        local = []
        for n in names:
            if n in STDLIB:
                continue
            for anc in f.parents:
                if (anc / n).with_suffix(".py").exists() or (anc / n / "__init__.py").exists():
                    local.append(n)
                    break
        if local and "sys.path" not in src:
            bad.append(f"{rel(f, root)} -> {', '.join(sorted(local)[:3])}")
    if bad:
        rep.add(
            "WARN", "sys.path 引导",
            "导入了项目内模块但没补 sys.path，以脚本启动时会 ImportError"
            "（发生在握手之前，客户端只报 Connection closed）：" + "; ".join(bad[:4]),
        )
    else:
        rep.add("PASS", "sys.path 引导", "未发现缺失 sys.path 引导的项目内导入")


def check_tool_registration(files: list[Path], root: Path, rep: Report) -> None:
    decorated, plain = [], []
    for f in files:
        try:
            src = strip_comments(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if "mcp" not in src and "tool" not in src.lower():
            continue
        if re.search(r"@\w+\.tool\b", src):
            decorated.append(rel(f, root))
        elif re.search(r"^\s*\w+\.tool\(", src, re.MULTILINE):
            plain.append(rel(f, root))
    if decorated:
        rep.add(
            "WARN", "工具可测性",
            "用了 @x.tool 装饰器，原函数会被包成 FunctionTool 导致无法单测："
            + ", ".join(decorated[:4]) + "。改为末尾 for fn in (...): mcp.tool(fn) 注册",
        )
    elif plain:
        rep.add("PASS", "工具可测性", "采用末尾统一注册，原函数保持可调用")
    else:
        rep.add("SKIP", "工具可测性", "未识别到工具注册写法")


def check_path_guard(files: list[Path], root: Path, rep: Report) -> None:
    """接受路径参数的函数有没有越界校验。

    关键是要做调用链感知：项目里常见写法是 `root = resolve_dir(subdir)`，
    防护在被调用的函数里，只看本函数体会误报。
    """
    # 第一遍：找出所有自带防护的函数名（跨文件，因为防护常抽到 _common.py）
    guarded: set[str] = set()
    for f in files:
        try:
            src = strip_comments(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        for m in DEF_RE.finditer(src):
            body = src[m.end(): m.end() + 1500]
            if GUARD.search(body):
                guarded.add(m.group(1))

    bad = []
    for f in files:
        try:
            src = strip_comments(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        for m in DEF_RE.finditer(src):
            name, params = m.group(1), m.group(2)
            if not PATH_PARAM.search(params):
                continue
            body = src[m.end(): m.end() + 1500]
            if GUARD.search(body):
                continue
            # 间接防护：调用了自带防护的函数（如 resolve_dir）
            called = set(re.findall(r"([A-Za-z_]\w*)\s*\(", body))
            if called & guarded:
                continue
            bad.append(f"{rel(f, root)}:{name}()")
    if bad:
        rep.add(
            "FAIL", "路径越界防护",
            "接受路径参数但没有越界校验，等于把整个文件系统暴露给模型："
            + ", ".join(bad[:5]),
        )
    else:
        rep.add("PASS", "路径越界防护", "未发现无防护的路径参数")


def collect_tools(files: list[Path], rep: Report) -> None:
    """只认「真正注册过的」工具，不要把辅助函数一起列进来。

    识别三种注册写法：末尾 for 循环统一注册、直接 mcp.tool(name)、@mcp.tool 装饰。
    都识别不出来才回退到「所有非下划线开头的顶层函数」，并标注为未识别。
    """
    names: list[str] = []
    detected = False
    for f in files:
        try:
            src = strip_comments(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if "tool" not in src.lower() and "mcp" not in src:
            continue
        # for _fn in (a, b, c): mcp.tool(_fn)
        for m in re.finditer(r"for\s+\w+\s+in\s*\(([^)]*)\)\s*:", src):
            names += re.findall(r"[A-Za-z_]\w*", m.group(1))
            detected = True
        # mcp.tool(name)
        names += re.findall(r"^\s*\w+\.tool\(\s*([A-Za-z_]\w*)\s*\)", src, re.MULTILINE)
        if re.search(r"^\s*\w+\.tool\(\s*[A-Za-z_]\w*\s*\)", src, re.MULTILINE):
            detected = True
        # @mcp.tool 装饰的函数
        decorated = re.findall(r"@\w+\.tool[^\n]*\n\s*def\s+(\w+)", src)
        if decorated:
            names += decorated
            detected = True

    if not detected:
        for f in files:
            try:
                src = strip_comments(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            names += [m.group(1) for m in DEF_RE.finditer(src) if not m.group(1).startswith("_")]
        rep.unidentified = True

    rep.tools = sorted({n for n in dict.fromkeys(names) if not n.startswith("_")})


def check_write_tools(rep: Report) -> None:
    risky = []
    for t in rep.tools:
        tokens = [w for w in re.split(r"[_\W]+", t.lower()) if w]
        if tokens and tokens[0] in {"get", "list", "read", "scan", "check", "search", "show", "find"}:
            continue
        if any(w in WRITE_TOKENS for w in tokens):
            risky.append(t)
    if risky:
        rep.add(
            "WARN", "写操作工具",
            "这些工具名暗示有副作用，必须挂人工确认：" + ", ".join(risky[:12]),
        )
    elif rep.unidentified:
        rep.add("SKIP", "写操作工具", "未识别注册方式，工具清单为推测，需人工核对")
    else:
        rep.add("PASS", "写操作工具", f"未在 {len(rep.tools)} 个工具名中发现写操作迹象")


def check_stdout(files: list[Path], root: Path, rep: Report) -> None:
    """stdio 传输下，任何 print 到 stdout 都会污染换行分隔的 JSON-RPC 流。"""
    # 只要这个目录里确实存在 MCP server，就对所有文件查 stdout——
    # 被 stdio 启动的进程里，任何一个模块的 print 都会污染协议流，
    # 不限于写了 mcp 字样的那个文件。
    sources = {}
    for f in files:
        try:
            sources[f] = strip_comments(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    if not any("mcp" in s for s in sources.values()):
        rep.add("SKIP", "stdout 污染", "目标不像 MCP server，跳过")
        return

    bad = []
    for f, src in sources.items():
        for i, line in enumerate(src.splitlines(), 1):
            if re.match(r"print\s*\(", line):
                bad.append(f"{rel(f, root)}:{i}")
    if bad:
        rep.add(
            "FAIL", "stdout 污染",
            "模块级 print 会污染 stdio 的 JSON-RPC 流（日志必须走 stderr）：" + ", ".join(bad[:5]),
        )
    else:
        rep.add("PASS", "stdout 污染", "未发现模块级 print")


def check_secrets(files: list[Path], root: Path, rep: Report) -> None:
    hits = []
    for f in files:
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(src.splitlines(), 1):
            for m in SECRET_ASSIGN.finditer(line):
                if PLACEHOLDER.match(m.group(2)):
                    continue
                hits.append(f"{rel(f, root)}:{i} 疑似 {m.group(1)} 硬编码")
            if SECRET_LITERAL.search(line) or CONN_WITH_PWD.search(line):
                hits.append(f"{rel(f, root)}:{i} 疑似密钥字面量")
    if hits:
        rep.add("FAIL", "硬编码凭据", "; ".join(hits[:5]))
    else:
        rep.add("PASS", "硬编码凭据", "未发现硬编码凭据迹象")


def audit(target: Path) -> dict:
    files = iter_py_files(target)
    root = target if target.is_dir() else target.parent
    rep = Report()

    if not files:
        return {"target": str(target), "files": 0, "findings": [], "tools": [], "checks": {}}

    check_shadowing(files, root, rep)
    check_sys_path(files, root, rep)
    check_tool_registration(files, root, rep)
    check_path_guard(files, root, rep)
    collect_tools(files, rep)
    check_write_tools(rep)
    check_stdout(files, root, rep)
    check_secrets(files, root, rep)

    names = ["标准库遮蔽", "sys.path 引导", "工具可测性", "路径越界防护",
             "写操作工具", "stdout 污染", "硬编码凭据"]
    return {
        "target": str(target),
        "files": len(files),
        "tools": rep.tools,
        "tool_count": len(rep.tools),
        "checks": {n: rep.worst(n) for n in names},
        "findings": rep.findings,
        "blocked": any(f["level"] == "FAIL" for f in rep.findings),
    }


ICON = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}


def render(res: dict) -> None:
    if not res["files"]:
        print(f"未找到 Python 文件：{res['target']}")
        return
    print(f"审查目标：{res['target']}")
    print(f"文件数：{res['files']}    识别工具：{res['tool_count']} 个")
    if res["tools"]:
        print("工具清单：" + ", ".join(res["tools"][:20]) + (" ..." if len(res["tools"]) > 20 else ""))
    print()
    print(f"{'结果':<7}检查项")
    print("-" * 46)
    for name, lv in res["checks"].items():
        print(f"{ICON[lv]:<8}{name}")
    print()
    for f in res["findings"]:
        if f["level"] in ("FAIL", "WARN"):
            print(f"[{f['level']}] {f['check']}：{f['detail']}")
    print()
    if res["blocked"]:
        print("结论：存在阻断项，先修再接入。")
    else:
        print("结论：静态审查通过，仍须做真实连通性验证（tools/list + 逐个冒烟）后才能接进 agent。")


def main() -> None:
    ap = argparse.ArgumentParser(description="MCP Server 源码只读审查")
    ap.add_argument("target", help="server.py 或项目/目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    target = Path(args.target)
    if not target.exists():
        raise SystemExit(f"路径不存在：{target}")

    res = audit(target)
    print(json.dumps(res, ensure_ascii=False, indent=2)) if args.json else render(res)


if __name__ == "__main__":
    main()
