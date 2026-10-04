"""模块切分与画像：把一个仓库拆成「可独立分析的模块」。

为什么评审要先切模块
--------------------
「分析各模块功能」如果对着整个仓库问模型，得到的必然是套话。
所以流程是：**先切分 → 每个模块出结构化画像 → 只对核心模块深读代码**。
画像是筛选器，也是 prompt 素材——它决定「哪些模块值得花 token 读」。

切分策略的优先级（这个顺序是踩出来的）
--------------------------------------
1. **显式声明**：pnpm-workspace / lerna / nx / go.work / Cargo workspace / settings.gradle
2. **子项目标记文件**：含 package.json / go.mod / Cargo.toml / pyproject.toml 的目录
3. **整体兜底**：切不出模块就当单模块

为什么必须优先读声明：deepseek-harness 里有 **357 个 package.json**
（`vendor/**` 全是第三方），而它自己的 `pnpm-workspace.yaml` 只声明了
`apps/*`、`packages/*/*`、`native/system` 等少数几个。纯扫标记文件会
切出 357 个「模块」，其中 99% 是别人的代码。

`vendor/`、`third_party/`、`node_modules/` 这类目录里的东西**不算本项目的模块**——
评审陌生仓库时把第三方代码当成自己写的模块，是最容易出的错。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from agent_kit.logging_conf import get_logger
from agent_kit.mcp_servers._common import SKIP_DIRS

log = get_logger("modules")

#: 这些目录里是别人的代码，不参与模块切分也不计入本项目代码量
VENDOR_DIRS = {
    "vendor", "vendors", "third_party", "thirdparty", "3rdparty", "third-party",
    "external", "externals", "deps", "_deps", "packages-dist",
}

#: 判定「这是一个独立子项目」的标记文件
MARKER_FILES = (
    "package.json", "go.mod", "Cargo.toml", "pyproject.toml", "setup.py",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile",
)

_LOC_SUFFIXES = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP",
    ".c": "C", ".h": "C/C++", ".cpp": "C/C++", ".hpp": "C/C++",
    ".cs": "C#", ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
    ".sql": "SQL", ".vue": "Vue", ".svelte": "Svelte",
}

#: **可执行**入口的文件名。刻意**不含** `index.*` / `mod.rs` / `lib.rs`——
#: 那是库导出的典型写法，不是"能跑起来"的入口。误把它当入口的后果很具体：
#: 一个 500 行的普通库因为多了 40 分，会把 3000 行的真正应用挤出 Top 1
#: （实测 core-lib 99.9 分 vs desktop-app 81.7 分）。
_ENTRY_NAMES = {
    "main.py", "__main__.py", "app.py", "cli.py", "manage.py", "server.py", "run.py",
    "main.go", "main.rs", "main.ts", "main.js", "main.mjs", "main.cjs",
    "app.ts", "app.js", "server.ts", "server.js", "extension.ts",
}

#: 库导出的典型文件名（可执行入口之外的另一种"入口"）
_EXPORT_NAMES = {
    "index.ts", "index.js", "index.mjs", "index.cjs", "mod.rs", "lib.rs", "__init__.py",
}
_TEST_HINTS = ("test", "tests", "spec", "__tests__", "e2e", "fixtures")


@dataclass
class ModuleProfile:
    """一个模块的结构化画像。刻意不含"模块是干什么的"——那要读代码才知道。"""

    name: str
    path: str                     # 相对仓库根
    kind: str                     # workspace-package / subproject / root
    language: str                 # 主要语言
    loc: int
    file_count: int
    test_files: int
    #: **可执行**入口（bin / scripts.start / main.py / cmd/*/main.go）。
    #: 只有它代表"这是个能跑的产品"，才参与重要性评分。
    entry_points: tuple[str, ...]
    #: 库导出入口（main / module / exports）。只说明这是个库，不加分——
    #: 早先把它当 entry_points，结果 monorepo 里几乎每个包都有 lib/index.js，
    #: "有入口"这个信号就此失效（实测 Top 14 全是 ★）。
    exports: tuple[str, ...]
    has_readme: bool
    readme_head: str              # README 前几行，给模型判断模块用途
    deps_internal: tuple[str, ...]   # 依赖了哪些内部模块
    deps_external: tuple[str, ...]   # 外部依赖（包名）
    dependents: tuple[str, ...]     # 被哪些内部模块依赖
    importance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path, "kind": self.kind,
            "language": self.language, "loc": self.loc, "file_count": self.file_count,
            "test_files": self.test_files,
            "entry_points": list(self.entry_points),
            "exports": list(self.exports),
            "has_readme": self.has_readme, "readme_head": self.readme_head,
            "deps_internal": list(self.deps_internal),
            "deps_external": list(self.deps_external)[:12],
            "dependents": list(self.dependents), "importance": round(self.importance, 2),
        }


@dataclass
class ModuleScan:
    strategy: str
    modules: list[ModuleProfile] = field(default_factory=list)
    root_language: str = ""
    total_loc: int = 0

    def top(self, n: int) -> list[ModuleProfile]:
        """按重要性取前 n 个——深读代码的候选。"""
        return sorted(self.modules, key=lambda m: -m.importance)[:max(n, 0)]

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "root_language": self.root_language,
            "total_loc": self.total_loc,
            "module_count": len(self.modules),
            "modules": [m.to_dict() for m in
                        sorted(self.modules, key=lambda m: -m.importance)],
        }


# ---------------------------------------------------------------- 切分


def scan_modules(root: Path, *, max_modules: int = 200) -> ModuleScan:
    """把仓库切成模块并出画像。"""
    root = Path(root).resolve()
    # 先**全部**展开再截断。按声明顺序截断会砍掉 apps/——
    # pnpm-workspace.yaml 里 packages/*/* 排在 apps/* 前面，
    # 341 个模块截到 120 时，应用全没了，而带入口的应用恰恰最该深读。
    paths, strategy = _discover_module_paths(root, max_modules=None)
    if not paths:
        paths, strategy = [root], "single"

    profiles = []
    for path in paths:
        prof = _profile_module(root, path)
        # 平台产物包（native/system/packages/darwin-arm64 这类）里没有源码，
        # 留着只会稀释 Top N —— 它们是构建结果，不是模块。
        if prof.loc == 0 and not prof.has_readme and len(paths) > 1:
            log.debug("跳过无源码的模块：%s", prof.path)
            continue
        profiles.append(prof)

    _link_internal_deps(profiles)
    for m in profiles:
        m.importance = _importance(m)

    if max_modules and len(profiles) > max_modules:
        profiles = _cap_modules(profiles, max_modules)

    return ModuleScan(
        strategy=strategy,
        modules=profiles,
        root_language=_dominant_language(root),
        total_loc=sum(m.loc for m in profiles),
    )


def _cap_modules(profiles: list[ModuleProfile], limit: int) -> list[ModuleProfile]:
    """超出上限时：**带入口的必留**（那是能跑的产品），其余按重要性补。

    单纯按分数砍会让"被很多库依赖的公共包"挤掉"应用本身"——
    而「分析各模块功能」最该讲清楚的就是应用。
    """
    ranked = sorted(profiles, key=lambda m: -m.importance)
    with_entry = [m for m in ranked if m.entry_points]
    if len(with_entry) >= limit:
        return with_entry[:limit]
    rest = [m for m in ranked if not m.entry_points]
    return with_entry + rest[: limit - len(with_entry)]


def _discover_module_paths(root: Path, *, max_modules: int) -> tuple[list[Path], str]:
    """返回（模块目录列表, 用了哪种策略）。"""
    declared = _declared_module_dirs(root)
    if declared:
        return declared[:max_modules], "workspace-declared"

    found: list[Path] = []
    for dirpath, dirnames, filenames in _walk_dirs(root):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        if dirpath == root:
            # 根目录自己带标记文件也算一个模块（单包仓库）
            if any(f in filenames for f in MARKER_FILES):
                found.append(root)
            continue
        if any(f in filenames for f in MARKER_FILES):
            found.append(Path(dirpath))
    if not found:
        return [], ""
    return found[:max_modules], "subproject-markers"


def _declared_module_dirs(root: Path) -> list[Path]:
    """读各生态的 workspace 声明。这是唯一可信的"官方模块划分"。"""
    out: list[Path] = []

    # JS/TS：pnpm-workspace.yaml / lerna.json / package.json 的 workspaces
    pnpm = root / "pnpm-workspace.yaml"
    if pnpm.is_file():
        out += _expand_globs(root, _parse_pnpm_globs(pnpm))
    lerna = root / "lerna.json"
    if lerna.is_file():
        out += _expand_globs(root, _parse_json_globs(lerna, "packages"))
    pkg = root / "package.json"
    if pkg.is_file():
        out += _expand_globs(root, _parse_pkg_workspaces(pkg))
    nx = root / "nx.json"
    if nx.is_file() and not out:
        out += _expand_globs(root, _parse_json_globs(nx, "projects"))

    # Go：go.work
    gowork = root / "go.work"
    if gowork.is_file():
        out += _parse_go_work(root, gowork)

    # Rust：Cargo.toml 的 [workspace] members
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        out += _expand_globs(root, _parse_cargo_members(cargo))

    # Java：settings.gradle 的 include
    for name in ("settings.gradle", "settings.gradle.kts"):
        f = root / name
        if f.is_file():
            out += _expand_globs(root, _parse_gradle_includes(f))

    # vendor/third_party 必须在这里就排掉：pnpm-workspace.yaml 常常**故意**
    # 把 vendor/* 声明成 workspace 成员（为了统一构建），
    # 只在统计代码行时跳过是不够的——模块本身会被切出来，
    # 评审时把第三方代码当成"这个项目的一个模块"，是最难察觉的错误。
    # vendor/third_party 必须在这里就排掉：pnpm-workspace.yaml 常常**故意**
    # 把 vendor/* 声明成 workspace 成员（为了统一构建），
    # 只在统计代码行时跳过是不够的——模块本身会被切出来，
    # 评审时把第三方代码当成"这个项目的一个模块"，是最难察觉的错误。
    # vendor/third_party 必须在这里就排掉：pnpm-workspace.yaml 常常**故意**
    # 把 vendor/* 声明成 workspace 成员（为了统一构建），
    # 只在统计代码行时跳过是不够的——模块本身会被切出来，
    # 评审时把第三方代码当成"这个项目的一个模块"，是最难察觉的错误。
    return _dedup([
        p for p in out
        if p.is_dir() and not any(part in VENDOR_DIRS for part in p.relative_to(root).parts)
    ])


def _parse_pnpm_globs(path: Path) -> list[str]:
    """极简 YAML 读法：只要 `packages:` 下面那串 `- xxx` 就够了。

    不引入 PyYAML 是刻意的——这个项目已经够多依赖了，而 workspace 声明
    的格式在实践中非常规整（列表项就是 glob），正则足够可靠。
    """
    globs: list[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if re.match(r"^packages\s*:", stripped):
            inside = True
            continue
        if inside:
            if stripped.startswith("- "):
                g = stripped[2:].strip().strip("'\"")
                if g and not g.startswith("#"):
                    globs.append(g)
            elif stripped and not stripped.startswith("#"):
                break
    return globs


def _parse_pkg_workspaces(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return []
    ws = data.get("workspaces")
    if isinstance(ws, list):
        return [str(x) for x in ws]
    if isinstance(ws, dict) and isinstance(ws.get("packages"), list):
        return [str(x) for x in ws["packages"]]
    return []


def _parse_json_globs(path: Path, key: str) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return []
    val = data.get(key)
    return [str(x) for x in val] if isinstance(val, list) else []


def _parse_go_work(root: Path, path: Path) -> list[Path]:
    out: list[Path] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r'\s*use\s+"([^"]+)"', line)
        if m:
            p = (root / m.group(1)).resolve()
            if p.is_dir():
                out.append(p)
    return out


def _parse_cargo_members(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"\[workspace\](.*?)(?=\n\[|\Z)", text, re.DOTALL)
    if not m:
        return []
    return re.findall(r'members\s*=\s*\[(.*?)\]', m.group(1), re.DOTALL)[0].split(",") \
        if re.findall(r'members\s*=\s*\[(.*?)\]', m.group(1), re.DOTALL) else []


def _parse_gradle_includes(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return re.findall(r"""include\s*\(?\s*['"]([^'"]+)['"]""", text)


def _expand_globs(root: Path, globs: list[str]) -> list[Path]:
    """把 `apps/*`、`packages/*/*` 这类 glob 展开成实际目录。"""
    out: list[Path] = []
    for raw in globs:
        g = raw.strip().strip("'\"")
        if not g or g.startswith("#"):
            continue
        if any(c in g for c in "*?["):
            out += [p for p in root.glob(g) if p.is_dir()]
        else:
            p = root / g
            if p.is_dir():
                out.append(p)
    return out


# ---------------------------------------------------------------- 画像


def _profile_module(root: Path, mod: Path) -> ModuleProfile:
    rel = "." if mod == root else mod.relative_to(root).as_posix()
    loc, files, lang_counter, test_files = _scan_code(mod, root)
    name = _module_name(mod, root)
    runnable, exports = _entry_points(mod)
    return ModuleProfile(
        name=name,
        path=rel,
        kind="root" if mod == root else ("workspace-package" if rel != "." else "subproject"),
        language=_dominant_language(mod) or lang_counter.most_common(1)[0][0] if lang_counter else "",
        loc=loc,
        file_count=files,
        test_files=test_files,
        entry_points=runnable,
        exports=exports,
        has_readme=_has_readme(mod),
        readme_head=_readme_head(mod),
        deps_internal=(),
        deps_external=_external_deps(mod),
        dependents=(),
    )


def _scan_code(mod: Path, root: Path) -> tuple[int, int, Counter, int]:
    """统计代码行、文件数、语言分布、测试文件数。

    会跳过两样东西：**已是别的模块的子目录**（避免 monorepo 里重复计数）
    和 vendor/第三方目录。
    """
    loc = 0
    files = 0
    tests = 0
    langs: Counter = Counter()
    mod_real = mod.resolve()

    for path in mod.rglob("*"):
        if not path.is_file():
            continue
        try:
            parts = path.relative_to(mod).parts
        except ValueError:
            continue
        if any(_skip(p) for p in parts):
            continue
        # 这个文件其实属于另一个已切出的模块（monorepo 嵌套），别重复算
        owner = path.parent
        nested = False
        while owner != mod and owner.resolve() != mod_real:
            if (owner / "package.json").is_file() or (owner / "go.mod").is_file() \
                    or (owner / "Cargo.toml").is_file() or (owner / "pyproject.toml").is_file():
                nested = True
                break
            owner = owner.parent
        if nested:
            continue
        suffix = path.suffix.lower()
        if suffix not in _LOC_SUFFIXES:
            continue
        files += 1
        langs[_LOC_SUFFIXES[suffix]] += 1
        if any(h in p.lower() for h in _TEST_HINTS for p in parts):
            tests += 1
        try:
            loc += sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return loc, files, langs, tests


def _module_name(mod: Path, root: Path) -> str:
    """模块名优先用包声明里的 name，其次目录名。"""
    pkg = mod / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            name = str(data.get("name") or "").strip()
            if name:
                return name
        except (OSError, ValueError):
            pass
    for f in ("pyproject.toml", "Cargo.toml", "go.mod"):
        if (mod / f).is_file():
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', (mod / f).read_text(encoding="utf-8", errors="ignore"), re.MULTILINE)
            if m:
                return m.group(1)
    return root.name if mod == root else mod.name


def _entry_points(mod: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """找出模块的入口，返回 (可执行入口, 库导出入口)。

    两者必须分开：`main: lib/index.js` 说的是"这个库从哪导出"，
    而 `bin` / `scripts.start` 说的是"这个包能跑起来"。只有后者才说明
    它是个产品，混在一起会让"有入口"这个信号在 monorepo 里彻底失效。
    """
    runnable: list[str] = []
    exports: list[str] = []

    pkg = mod / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            data = {}
        bin_field = data.get("bin")
        if isinstance(bin_field, str):
            runnable.append(bin_field)
        elif isinstance(bin_field, dict):
            runnable += [str(v) for v in bin_field.values()]
        for key in ("main", "module", "browser", "types"):
            val = data.get(key)
            if isinstance(val, str):
                exports.append(val)
                break
        scripts = data.get("scripts")
        if isinstance(scripts, dict):
            for name in ("start", "dev", "serve", "electron", "watch"):
                cmd = scripts.get(name)
                if isinstance(cmd, str):
                    for token in re.findall(r"[\w./\\-]+\.(?:js|ts|mjs|cjs)", cmd):
                        runnable.append(token)
                        break
                    if runnable:
                        break

    # 非 JS 生态的"能跑"标志
    for name in ("main.py", "__main__.py", "app.py", "cli.py", "manage.py", "server.py"):
        if (mod / name).is_file():
            runnable.append(name)
    for name in ("main.go", "main.rs", "cmd"):
        if (mod / name).exists():
            runnable.append(name)
    if not runnable:
        for path in sorted(mod.glob("*")):
            if not path.is_file():
                continue
            if path.name in _ENTRY_NAMES:
                runnable.append(path.name)
            elif path.name in _EXPORT_NAMES:
                exports.append(path.name)
        for rel in ("cmd", "src"):
            d = mod / rel
            if d.is_dir():
                for p in sorted(d.rglob("main.*"))[:2]:
                    runnable.append(p.relative_to(mod).as_posix())

    return tuple(dict.fromkeys(runnable))[:5], tuple(dict.fromkeys(exports))[:3]


def _has_readme(mod: Path) -> bool:
    return any(p.is_file() for p in mod.glob("[Rr][Ee][Aa][Dd][Mm][Ee]*"))


def _readme_head(mod: Path, limit: int = 400) -> str:
    """README 前几行——给模型判断"这个模块是干什么的"的唯一线索。

    刻意不取全文：README 动辄上千行，全塞进 prompt 会挤掉代码信息。
    """
    for p in sorted(mod.glob("[Rr][Ee][Aa][Dd][Mm][Ee]*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        head = " ".join(text[:2000].split())[:limit]
        if head:
            return head
    return ""


def _external_deps(mod: Path) -> tuple[str, ...]:
    deps: list[str] = []
    pkg = mod / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                deps += list(data.get(key) or {})
        except (OSError, ValueError):
            pass
    for f in ("requirements.txt", "pyproject.toml"):
        target = mod / f
        if target.is_file():
            for line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"^\s*([A-Za-z0-9_.\-]+)\s*[=<>~!]", line)
                if m:
                    deps.append(m.group(1))
    go = mod / "go.mod"
    if go.is_file():
        for m in re.finditer(r"^\s*([\w./\-]+\.[\w./\-]+)\s+v", go.read_text(encoding="utf-8", errors="ignore"), re.MULTILINE):
            deps.append(m.group(1))
    return tuple(dict.fromkeys(d for d in deps if d))


def _link_internal_deps(profiles: list[ModuleProfile]) -> None:
    """把「A 依赖 B」的关系算出来（双向都记）。

    有了 dependents 才知道谁是核心——被 5 个模块依赖的包，
    出了问题影响面最大，深读优先级也最高。
    """
    by_name = {p.name: p for p in profiles}
    by_path = {p.path: p for p in profiles}
    for prof in profiles:
        internal = []
        for dep in prof.deps_external:
            if dep in by_name and by_name[dep] is not prof:
                internal.append(dep)
                continue
            for other in profiles:
                if other is prof:
                    continue
                # workspace 内部引用常见写法：@scope/pkg、相对路径 ../pkg
                if other.path and (
                    dep.endswith(other.path)
                    or (other.name and dep.rsplit("/", 1)[-1] == other.name)
                ):
                    internal.append(other.name)
                    break
        prof.deps_internal = tuple(dict.fromkeys(internal))
    for prof in profiles:
        deps = []
        for other in profiles:
            if prof.name in other.deps_internal:
                deps.append(other.name)
        prof.dependents = tuple(dict.fromkeys(deps))
    del by_path


# ---------------------------------------------------------------- 评分


def _importance(prof: ModuleProfile) -> float:
    """重要性评分：决定深读谁。

    四个因子：代码量（对数，避免大仓霸榜）、有没有入口（是应用还是库）、
    测试数（有人管）、被依赖数（影响面）。刻意不用"最近改得多"——
    我们拿到的往往是 tarball，没有历史。
    """
    import math

    score = 0.0
    score += math.log10(prof.loc + 1) * 12          # 代码量
    score += 40 if prof.entry_points else 0         # 有入口 = 能跑的产品 = 最该讲清楚
    # 被依赖数走对数：实测 monorepo 里一个基础库能被 300+ 个模块引用，
    # 线性加权会让它冲到 2600 分，把真正的应用（80 分）压到看不见——
    # 「分析各模块功能」最该讲清楚的是应用，不是被引用最多的库。
    score += math.log1p(len(prof.dependents)) * 8
    score += min(prof.test_files, 10) * 1.5         # 有测试说明是认真维护的
    score += 6 if prof.has_readme else 0            # 有文档
    if prof.path == ".":
        score += 5                                    # 根模块通常承载构建/CI 配置
    return score


# ---------------------------------------------------------------- 工具


def _skip(name: str) -> bool:
    return name in SKIP_DIRS or name in VENDOR_DIRS or name.startswith(".")


def _walk_dirs(root: Path):
    for dirpath, dirnames, _ in _walk(root):
        yield Path(dirpath), dirnames, _


def _walk(root: Path):
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip(d)]
        yield dirpath, dirnames, filenames


def _dominant_language(path: Path) -> str:
    counter: Counter = Counter()
    for p in path.rglob("*"):
        if p.is_file() and p.suffix.lower() in _LOC_SUFFIXES:
            if any(_skip(x) for x in p.parts):
                continue
            counter[_LOC_SUFFIXES[p.suffix.lower()]] += 1
            if sum(counter.values()) >= 400:
                break
    return counter.most_common(1)[0][0] if counter else ""


def _dedup(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out
