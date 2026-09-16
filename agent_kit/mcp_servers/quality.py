"""MCP Server · 工程质量体检（只读）：代码量、技术债标记、疑似密钥、测试与依赖。

为什么做成 MCP 而不是本地工具：
  1. **跨项目复用** —— 换个仓库只要把 `subdir` 指过去，Agent 侧不用改一行代码；
  2. **进程隔离** —— 扫描上万个文件是 CPU 密集活，放在独立进程里不拖慢 Agent；
  3. **权限收敛** —— 这个 server 全只读，可以给低信任场景挂，写操作由别的 server 提供。

启动：
    python agent_kit/mcp_servers/quality.py            # stdio
    python agent_kit/mcp_servers/quality.py --http     # streamable-http
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

# MCP 客户端是「当脚本启动」的（python .../quality.py），此时 sys.path[0] 是脚本所在目录，
# 项目根并不在搜索路径里，`import agent_kit...` 会直接 ModuleNotFoundError。
# 这个失败发生在 stdio 握手之前，客户端只会看到 "Connection closed"，极难定位，
# 所以这里必须在导入项目模块前把项目根补进去。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agent_kit.mcp_servers._common import PROJECT_ROOT, iter_code_files, rel, resolve_dir

mcp = FastMCP("atlas-quality-mcp")

# 技术债标记：扫这些词能看出「哪里明知有问题却没修」
DEBT_MARKERS = ("TODO", "FIXME", "HACK", "XXX")

# 疑似密钥的规则。刻意写成「宽松匹配 + 人工复核」，宁可多报也不漏报；
# 但会剔除明显的示例值（xxx / your_key / example），避免刷屏。
SECRET_PATTERNS = [
    ("私钥文件", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("云厂商 AK", re.compile(r"\b(?:AKIA|LTAI|ASIA)[0-9A-Z]{12,}\b")),
    ("通用 sk- 令牌", re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}\b")),
    ("硬编码口令", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']")),
    # 主机名也要捕获进来：只匹配到 @ 就结束的话，后面无法判断是不是 localhost 默认凭据
    ("数据库连接串", re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|redis)://[^\s\"']+:[^\s\"'@]+@[^\s\"'/]+")),
]
# 命中但明显是占位的写法：示例值、模板变量、文档里的 user:pwd@host 之类。
# 这一栏会随实践不断补——宁可多剔除一些噪音，也别让真警报被淹没在误报里。
SECRET_ALLOWLIST = re.compile(
    r"(?i)(example|sample|dummy|placeholder|your[_-]|xxx+|changeme|\*+"
    r"|user:(pwd|pass|password)@|<[^>]*>|\$\{)"
)

# 本地开发默认凭据（postgres://atlas:atlas@localhost 这类）不算泄密。
# 但**仍然要单独计数并在结论里提示**——换环境不改就是真事故，只是性质不同。
LOCAL_HOSTS = re.compile(r"@(localhost|127\.0\.0\.1|\[::1\])([:/]|$)")

TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".vue", ".java", ".go", ".rs", ".sql", ".sh", ".yml", ".yaml", ".toml", ".md", ".env", ".cfg", ".ini"}


# ---------------------------------------------------------------------------
# 工具 1：代码量画像
# ---------------------------------------------------------------------------
def project_code_stats(subdir: str = ".") -> dict:
    """统计项目代码规模：文件数、总行数、有效代码行、注释行，并按语言分组。

    判断一个项目「到底多大」时用它，别凭感觉估。

    Args:
        subdir: 相对项目根目录的子目录，默认整个项目
    """
    root = resolve_dir(subdir)
    by_ext: Counter[str] = Counter()
    files = 0
    total = blank = comment = 0

    for path in iter_code_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files += 1
        by_ext[path.suffix.lower()] += 1
        for line in text.splitlines():
            s = line.strip()
            total += 1
            if not s:
                blank += 1
            elif s.startswith(("#", "//", "/*", "*", "--")):
                comment += 1

    return {
        "root": rel(root, PROJECT_ROOT),
        "files": files,
        "total_lines": total,
        "code_lines": total - blank - comment,
        "blank_lines": blank,
        "comment_lines": comment,
        "comment_ratio": round(comment / total, 3) if total else 0.0,
        "by_ext": dict(by_ext.most_common()),
    }


# ---------------------------------------------------------------------------
# 工具 2：技术债标记
# ---------------------------------------------------------------------------
def scan_debt_markers(subdir: str = ".", limit: int = 30) -> dict:
    """扫描 TODO / FIXME / HACK / XXX 标记，报告位置与内容。

    这些标记是「明知有问题却没修」的证据，评审与接手项目时先看它们。

    Args:
        subdir: 相对项目根目录的子目录
        limit: 最多返回多少条
    """
    root = resolve_dir(subdir)
    hits: list[dict] = []
    counter: Counter[str] = Counter()

    for path in iter_code_files(root, TEXT_SUFFIXES):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            for marker in DEBT_MARKERS:
                if marker in line:
                    counter[marker] += 1
                    if len(hits) < limit:
                        hits.append({"file": rel(path, root), "line": lineno, "marker": marker, "text": line.strip()[:120]})
                    break

    return {"total": sum(counter.values()), "by_marker": dict(counter), "hits": hits, "truncated": sum(counter.values()) > limit}


# ---------------------------------------------------------------------------
# 工具 3：疑似密钥扫描
# ---------------------------------------------------------------------------
def scan_secrets(subdir: str = ".", limit: int = 20) -> dict:
    """扫描代码里疑似硬编码的密钥、令牌、口令与带密码的连接串。

    **只报位置不报原文**（内容打码），避免把秘密再泄漏一遍到对话里。
    命中不等于泄密，示例值与占位符已尽力剔除，仍需人工复核。

    Args:
        subdir: 相对项目根目录的子目录
        limit: 最多返回多少条
    """
    root = resolve_dir(subdir)
    hits: list[dict] = []
    local_defaults: list[dict] = []

    for path in iter_code_files(root, TEXT_SUFFIXES):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if SECRET_ALLOWLIST.search(line):
                continue
            for kind, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                raw = match.group(0)
                masked = raw[:6] + "*" * max(len(raw) - 6, 4)
                item = {"file": rel(path, root), "line": lineno, "kind": kind, "masked": masked}
                # 本地默认凭据降级处理：单独统计，不算泄密但明确提示「换环境必须改」
                (local_defaults if LOCAL_HOSTS.search(raw) else hits).append(item)
                break
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break

    return {
        "count": len(hits),
        "hits": hits,
        "local_defaults": len(local_defaults),
        "local_sample": local_defaults[:5],
        "note": "命中项需人工复核；示例值、占位符与 localhost 默认凭据已降级处理。",
    }


# ---------------------------------------------------------------------------
# 工具 4：测试现状
# ---------------------------------------------------------------------------
def check_tests(subdir: str = ".") -> dict:
    """检查测试是否真的存在：tests 目录、测试文件数、用例数，以及无测试的代码目录。

    「有测试」和「测试覆盖了多少」是两回事，这里只回答前者——
    覆盖率要真跑 pytest-cov，静态扫描给不了。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    test_files = [p for p in iter_code_files(root) if p.name.startswith("test_") or p.name.endswith("_test.py")]
    cases = 0
    for path in test_files:
        try:
            cases += len(re.findall(r"^\s*def test_", path.read_text(encoding="utf-8", errors="ignore"), re.MULTILINE))
        except OSError:
            continue

    return {
        "root": rel(root, PROJECT_ROOT),
        "has_tests_dir": (root / "tests").is_dir(),
        "test_files": len(test_files),
        "test_cases": cases,
        "test_files_sample": [rel(p, root) for p in test_files[:10]],
    }


# ---------------------------------------------------------------------------
# 工具 5：依赖声明审计
# ---------------------------------------------------------------------------
def dependency_audit(subdir: str = ".") -> dict:
    """审计依赖声明：逐行看 requirements.txt 有没有钉住版本。

    没钉版本 = 下次安装可能装到不兼容的新版本，CI 明明绿了、本地却跑不起来。

    Args:
        subdir: 相对项目根目录的子目录
    """
    root = resolve_dir(subdir)
    req = root / "requirements.txt"
    if not req.exists():
        return {"found": False, "message": "未找到 requirements.txt"}

    pinned: list[str] = []
    unpinned: list[str] = []
    for raw in req.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        (pinned if re.search(r"[=><~]=?", line.split("[")[0]) else unpinned).append(line)

    return {
        "found": True,
        "total": len(pinned) + len(unpinned),
        "pinned": len(pinned),
        "unpinned": unpinned[:20],
        "verdict": "全部钉住版本" if not unpinned else f"{len(unpinned)} 个依赖未钉版本",
    }


# ---------------------------------------------------------------------------
# 资源：把体检口径写下来，避免人和 Agent 各说各话
# ---------------------------------------------------------------------------
@mcp.resource("quality://rubric")
def quality_rubric() -> str:
    """工程质量体检的判定口径（MCP 资源）。"""
    return (
        "# 工程质量体检口径\n"
        "\n"
        "1. 代码规模：看 files / code_lines，判断是否与文档描述一致（文档写 6000 行、实际 2000 行就是失真）。\n"
        "2. 技术债：TODO/FIXME 数量与集中度，集中在少数文件说明可控，遍地都是说明失控。\n"
        "3. 密钥：命中即处理；localhost 默认凭据虽降级，换环境不改仍要改。\n"
        "4. 测试：has_tests_dir 为假直接判不合格；test_cases 过少（< 10）视为形同虚设。\n"
        "5. 依赖：unpinned 非空则 CI 的「可复现」不成立。"
    )


# ---------------------------------------------------------------------------
# 注册：先写普通函数、最后统一注册
# ---------------------------------------------------------------------------
# 不用 `@mcp.tool` 装饰器的原因：装饰后变量指向的是 FunctionTool 对象，
# 原函数被包住，单元测试就没法直接调用逻辑了。
# 「逻辑」与「协议注册」分开，是这个 server 能被测试的前提。
for _fn in (project_code_stats, scan_debt_markers, scan_secrets, check_tests, dependency_audit):
    mcp.tool(_fn)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--http" in args:
        port = 8011
        for arg in args:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run(transport="stdio")
