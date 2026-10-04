"""把一个模块压成一段可控长度的摘要，供模型判断"它是干什么的"。

为什么需要这一层
----------------
取证工具给的是数字（多少行、多少测试、多少依赖），这**回答不了**
"这个模块负责什么"。要让模型说出模块职责，就必须让它看见：
入口在哪、对外导出什么、README 怎么自述、被谁依赖。

但不能把整个模块塞进 prompt——一个 5 万行的模块读进去，
上下文会被代码塞满，模型反而忽略结构信息。所以这里做**抽取 + 限长**：

    目录骨架 → 入口与导出符号 → README 自述 → 内部依赖 → 最大的几个文件

顺序是按信息密度排的：目录告诉模型"有什么"，导出符号告诉它"能做什么"，
README 是作者自己的说法（最可信），依赖关系告诉它"在系统里的位置"。

一个刻意的取舍：**不贴大段实现代码**。模型要看的是"结构与职责"，
不是逐行实现；真要核验某段逻辑时，应该由专家按需定点读文件。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_kit.logging_conf import get_logger
from agent_kit.mcp_servers._common import SKIP_DIRS
from agent_kit.modules import VENDOR_DIRS, ModuleProfile

log = get_logger("module_digest")

#: 导出符号的抽取规则：Python 的 def/class，JS/TS 的 export，
#: Go 的 func + 首字母大写（导出即公开），Java/Rust 的 pub/公开修饰。
_SYMBOL_RULES: dict[str, re.Pattern] = {
    ".py": re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", re.MULTILINE),
    ".ts": re.compile(
        r"^\s*export\s+(?:default\s+)?(?:abstract\s+)?"
        r"(?:async\s+)?(?:class|function|const|let|var|interface|type|enum)\s+(\w+)",
        re.MULTILINE,
    ),
    ".tsx": re.compile(
        r"^\s*export\s+(?:default\s+)?(?:abstract\s+)?"
        r"(?:class|function|const|interface|type)\s+(\w+)",
        re.MULTILINE,
    ),
    ".js": re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:class|function)\s+(\w+)", re.MULTILINE),
    ".jsx": re.compile(r"^\s*(?:export\s+)?(?:class|function)\s+(\w+)", re.MULTILINE),
    ".go": re.compile(r"^func\s+([A-Z]\w*)", re.MULTILINE),
    ".rs": re.compile(r"^\s*pub\s+(?:async\s+)?(?:fn|struct|enum|trait|mod)\s+(\w+)", re.MULTILINE),
    ".java": re.compile(
        r"^\s*public\s+(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+(\w+)", re.MULTILINE),
    ".rb": re.compile(r"^\s*(?:class|module|def)\s+(\w+)"),
    ".php": re.compile(r"^\s*(?:final\s+|abstract\s+)?(?:class|interface|trait)\s+(\w+)"),
}

#: 目录树里跳过的目录（与 modules.py 保持一致口径）
_SKIP = SKIP_DIRS | VENDOR_DIRS

_MAX_TREE_ENTRIES = 60
_MAX_SYMBOLS = 40
_MAX_BIG_FILES = 6


def build_module_digest(
    root: Path,
    profile: ModuleProfile,
    *,
    max_chars: int = 2600,
) -> str:
    """生成模块摘要。长度硬约束在 `max_chars` 内（超了会截断并标注）。

    Args:
        root: 仓库根（用于解析相对路径）
        profile: 来自 `modules.scan_modules` 的画像
        max_chars: 摘要上限。默认 2600 ≈ 800 token，8 个模块也就 6k token，
            不会挤掉代码本身的空间。
    """
    mod = (root / profile.path).resolve()
    if not mod.is_dir():
        return f"（模块目录不存在：{profile.path}）"

    parts: list[str] = []
    parts.append(_headline(profile))
    parts.append(_tree(mod, profile.path))
    symbols = _symbols(mod)
    if symbols:
        parts.append(symbols)
    if profile.readme_head:
        parts.append(f"【自述】{profile.readme_head[:600]}")
    deps = _relations(profile)
    if deps:
        parts.append(deps)
    big = _biggest_files(mod)
    if big:
        parts.append(big)

    text = "\n\n".join(p for p in parts if p)
    if len(text) > max_chars:
        # 按段落砍：从信息密度最低的末尾开始丢，而不是硬截断成半句话
        kept: list[str] = []
        total = 0
        for para in text.split("\n\n"):
            if total + len(para) > max_chars and kept:
                break
            kept.append(para)
            total += len(para) + 2
        text = "\n\n".join(kept) + f"\n（摘要过长，已省略约 {len(text) - total} 字符）"
    return text


# ---------------------------------------------------------------- 各段


def _headline(p: ModuleProfile) -> str:
    """一句话画像：模型第一眼看到的东西。"""
    bits = [f"模块 {p.name}", f"路径 {p.path}", f"{p.loc} 行", p.language]
    if p.test_files:
        bits.append(f"{p.test_files} 个测试文件")
    else:
        bits.append("无测试")
    if p.entry_points:
        bits.append(f"入口 {', '.join(p.entry_points[:2])}")
    if p.exports:
        bits.append(f"导出 {', '.join(p.exports[:2])}")
    return "【画像】" + " | ".join(b for b in bits if b)


def _tree(mod: Path, rel: str) -> str:
    """目录骨架：2 层、限条目数。

    两个刻意的取舍：
    · 深层目录对判断职责没帮助，只列 2 层；
    · **测试目录只报名字不展开**——实测 deepseek-harness 的 apps/cli 下有
      30 多个 e2e/spec 文件，展开后把 src/ 的结构挤了出去，而模型要判断
      "这个模块是干什么的"，靠的是 src 而不是测试文件名。
    """
    lines: list[str] = []
    budget = [_MAX_TREE_ENTRIES]
    test_dirs = {"test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures", "__mocks__"}

    def walk(directory: Path, depth: int, prefix: str) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in _SKIP:
                continue
            if budget[0] <= 0:
                return
            budget[0] -= 1
            is_dir = e.is_dir()
            if is_dir and e.name.lower() in test_dirs:
                # 折叠成一个条目：存在即可，不展开
                try:
                    n = sum(1 for _ in e.iterdir())
                except OSError:
                    n = 0
                lines.append(f"{prefix}{e.name}/ ({n} 项，测试/夹具)")
                continue
            lines.append(f"{prefix}{e.name}{'/' if is_dir else ''}")
            if is_dir and depth > 0:
                walk(e, depth - 1, prefix + "  ")

    walk(mod, depth=1, prefix="  ")
    return "【结构】\n" + "\n".join(lines) if lines else ""


def _symbols(mod: Path) -> str:
    """抽取导出符号 —— 「这个模块对外提供什么」的直接证据。

    只对入口文件和根目录下的源文件做：深读一个模块不需要知道它内部
    每一个辅助函数，只需要知道它的公共表面。
    """
    found: list[str] = []
    files: list[Path] = []
    for e in mod.iterdir():
        if e.is_file() and e.suffix.lower() in _SYMBOL_RULES:
            files.append(e)
    for sub in ("src", "lib", "app", "."):
        d = mod / sub if sub != "." else mod
        if d.is_dir():
            files += [p for p in sorted(d.glob("*"))[:6]
                      if p.is_file() and p.suffix.lower() in _SYMBOL_RULES]
    seen: set[str] = set()
    for path in files[:8]:
        pattern = _SYMBOL_RULES.get(path.suffix.lower())
        if not pattern:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in pattern.finditer(text):
            name = m.group(1)
            if name in seen or name.startswith("_"):
                continue
            seen.add(name)
            found.append(name)
            if len(found) >= _MAX_SYMBOLS:
                break
        if len(found) >= _MAX_SYMBOLS:
            break
    if not found:
        return ""
    return "【对外提供】" + "、".join(found[:_MAX_SYMBOLS])


def _relations(p: ModuleProfile) -> str:
    bits = []
    if p.deps_internal:
        bits.append("依赖内部模块：" + "、".join(p.deps_internal[:8]))
    if p.dependents:
        bits.append(f"被 {len(p.dependents)} 个模块依赖：" + "、".join(p.dependents[:6]))
    if p.deps_external:
        bits.append("外部依赖：" + "、".join(p.deps_external[:8]))
    return "【关系】" + "；".join(bits) if bits else ""


def _biggest_files(mod: Path) -> str:
    """列出最大的几个源文件——核心逻辑通常在那里。"""
    stats: list[tuple[int, str]] = []
    for path in mod.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SYMBOL_RULES:
            continue
        try:
            if any(part in _SKIP for part in path.relative_to(mod).parts):
                continue
            stats.append((path.stat().st_size, path.relative_to(mod).as_posix()))
        except OSError:
            continue
    if not stats:
        return ""
    stats.sort(reverse=True)
    def _size(n: int) -> str:
        return f"{n // 1024}KB" if n >= 1024 else f"{n}B"

    top = "、".join(f"{name}({_size(size)})" for size, name in stats[:_MAX_BIG_FILES])
    return f"【最大文件】{top}"


# ---------------------------------------------------------------- 批量


def build_digests(
    root: Path,
    profiles: list[ModuleProfile],
    *,
    max_chars: int = 2600,
) -> dict[str, str]:
    """批量生成摘要，key 是模块名。"""
    out: dict[str, str] = {}
    for p in profiles:
        try:
            out[p.name] = build_module_digest(root, p, max_chars=max_chars)
        except Exception as exc:  # noqa: BLE001 —— 一个模块摘要失败不该拖垮整轮
            log.warning("生成模块摘要失败 %s：%s", p.name, exc)
            out[p.name] = f"（摘要生成失败：{type(exc).__name__}）"
    return out
