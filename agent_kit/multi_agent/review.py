"""工程健康度评审（Engineering Health Review）：接手陌生仓库时的第一次体检。

## 业务定义

给一个本地仓库路径，回答三个问题：
  1. 这个仓库工程化程度如何？（八项健康度，逐条取证）
  2. 哪些是必须改的阻断项，哪些只是建议？
  3. 哪些部分本次没查、需要人工确认？

适用：接手同事留下的项目、外包交付验收、开源项目选型、课程作业批阅。

## 这一层为什么必须「取证与判断分离」

评审类 Agent 最容易失真的一步，是**让模型凭印象下结论**——
「这个项目没有日志」「测试覆盖不足」，听着专业，但可能完全是编的。

所以本模块把流程拆成两段，边界是硬的：

  取证（确定性 Python）──→ 判断（模型）
  14 个 MCP 工具跑出来的事实      只做「归类 + 给建议」

模型拿到的只有取证结果，**看不到仓库本体**，也就无从编造。
没有取到证的维度一律落「未覆盖」，**绝不判为通过**——
评审报告里最危险的一行是「未发现问题」，因为它读起来像查过了。

## 为什么必须先串行取证、再并行判断

`set_root()` 切换的是**进程级**的取证根目录（见 `_common.py`），
四个专家并发各调一次就会互相踩，取证结果串到别人的仓库上。
所以 `run_review` 严格两段式：先一次性取完证，再把证据分发给专家并行判断。

## 降级

`judge=None`（没有模型 / 没配 Key）时仍产出报告，但**只给证据清单、不下结论**，
并在报告里把 `degraded` 标记为 True。宁可少说，不可编。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_kit.logging_conf import get_logger
from agent_kit.mcp_servers import doc_audit, git_history, quality
from agent_kit.mcp_servers._common import _normalize_root, reset_root, set_root
from agent_kit.multi_agent.fanout import TaskResult, TaskSpec, detect_conflicts, run_fanout

log = get_logger("agent.review")

# 结论的四档。**「未覆盖」是正规结论，不是失败** ——
# 评审报告不说清自己没查什么，读者会误以为是全量检查。
BLOCKER = "阻断"
SUGGESTION = "建议"
GOOD = "良好"
UNCOVERED = "未覆盖"
VERDICTS = (BLOCKER, SUGGESTION, GOOD, UNCOVERED)

# 八项维度（与内置 Skill `project_engineering` 的检查清单一致，不要各写一套）
DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("layering", "分层与依赖方向"),
    ("config", "配置外置"),
    ("errors", "统一错误处理"),
    ("logging", "日志"),
    ("tests", "测试"),
    ("ci", "CI"),
    ("docs", "文档"),
    ("deps", "依赖与安全"),
)
DIMENSION_NAMES = dict(DIMENSIONS)

# 八项分给四个专家：每人领两项，**只拿自己那部分证据**。
# 上下文越小，模型越不容易跑偏，也越便宜——这是 fan-out 在这里真正的收益。
EXPERT_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "name": "结构与配置",
        "dimensions": ("layering", "config"),
        "tools": ("project_code_stats", "project_tree", "project_checklist", "scan_secrets"),
    },
    {
        "name": "质量与测试",
        "dimensions": ("errors", "logging", "tests"),
        "tools": ("check_tests", "scan_debt_markers", "scan_secrets", "project_code_stats"),
    },
    {
        "name": "交付与文档",
        "dimensions": ("ci", "docs"),
        "tools": ("project_checklist", "readme_outline", "check_anchors", "changelog_status"),
    },
    {
        "name": "依赖与安全",
        "dimensions": ("deps",),
        "tools": ("dependency_audit", "scan_secrets", "git_status"),
    },
)

# 取证工具是直接 import 的函数，不起 MCP 子进程：
# 这些 server 刻意把「逻辑」与「协议注册」分开了（见 quality.py 末尾注释），
# 所以本地直调既快又可测，是离线评审能跑起来的前提。
TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "project_code_stats": quality.project_code_stats,
    "scan_debt_markers": quality.scan_debt_markers,
    "scan_secrets": quality.scan_secrets,
    "check_tests": quality.check_tests,
    "dependency_audit": quality.dependency_audit,
    "git_status": git_history.git_status,
    "git_log": git_history.git_log,
    "readme_outline": doc_audit.readme_outline,
    "check_anchors": doc_audit.check_anchors,
    "changelog_status": doc_audit.changelog_status,
    "project_checklist": doc_audit.project_checklist,
    "project_tree": doc_audit.project_tree,
}


# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------
class Finding(BaseModel):
    """一条评审结论。

    `evidence` 是硬约束：为空就说明这条结论没有依据，
    汇总时会被强制改判为「未覆盖」（见 `_enforce_evidence`）。
    """

    dimension: str = Field(description="八项维度之一，填 id（如 tests）或中文名")
    verdict: str = Field(description=f"结论档位：{' / '.join(VERDICTS)}")
    fact: str = Field(description="事实：是什么 + 在哪，一句话")
    evidence: str = Field(default="", description="证据：工具名 + 具体输出（数字 / 文件名）")
    suggestion: str = Field(default="", description="建议：怎么改 + 为什么；良好与未覆盖可不填")


class ReviewReport(BaseModel):
    """评审报告。"""

    repo: str = Field(description="被评仓库路径")
    generated_at: str = Field(description="生成时间")
    headline: str = Field(description="一句话总评")
    findings: list[Finding] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list, description="专家之间打架的结论")
    uncovered: list[str] = Field(default_factory=list, description="本次没查到证的维度")
    degraded: bool = Field(default=False, description="True = 没有模型，只出证据不下结论")

    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == BLOCKER]

    def suggestions(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == SUGGESTION]

    def goods(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == GOOD]


# ---------------------------------------------------------------------------
# 一、取证（确定性，不调模型）
# ---------------------------------------------------------------------------
def collect_evidence(root: str | Path, tools: tuple[str, ...] | None = None) -> dict[str, dict]:
    """跑一遍取证工具，返回 {工具名: {ok, data|error}}。

    单个工具失败（比如不是 git 仓库）**不影响其他工具** ——
    失败会被如实记进结果，后面该维度就落「未覆盖」。
    """
    set_root(root)
    try:
        wanted = tools or tuple(TOOL_REGISTRY)
        evidence: dict[str, dict] = {}
        for name in wanted:
            fn = TOOL_REGISTRY.get(name)
            if fn is None:
                evidence[name] = {"ok": False, "error": f"未知工具：{name}"}
                continue
            try:
                evidence[name] = {"ok": True, "data": fn()}
            except Exception as exc:  # noqa: BLE001 - 取证失败要降级，不能带崩整轮
                log.warning("取证 %s 失败：%s", name, exc)
                evidence[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return evidence
    finally:
        reset_root()


def evidence_digest(evidence: dict[str, dict], *, max_chars: int = 1200) -> str:
    """把取证结果压成给模型看的文本。

    截断的意义不只是省钱：**证据太长模型反而不看**（中间内容被忽略），
    每段限长能显著提高「结论引用了证据」的比例。
    """
    lines: list[str] = []
    for name, item in evidence.items():
        if not item.get("ok"):
            lines.append(f"- {name}：取证失败（{item.get('error')}）")
            continue
        payload = json.dumps(item.get("data"), ensure_ascii=False, default=str)
        if len(payload) > max_chars:
            payload = payload[:max_chars] + f"…（截断，共 {len(payload)} 字符）"
        lines.append(f"- {name}：{payload}")
    return "\n".join(lines)


def repo_digest(evidence: dict[str, dict]) -> str:
    """仓库概况：规模 + 是否 git 仓库。判断失真多半源于规模判断错了。"""
    stats = evidence.get("project_code_stats", {})
    data = stats.get("data") if stats.get("ok") else None
    if not isinstance(data, dict):
        return "规模未知（project_code_stats 取证失败）"
    git = evidence.get("git_status", {})
    dirty = ""
    if git.get("ok") and isinstance(git.get("data"), dict):
        dirty = "；工作区有未提交改动" if git["data"].get("dirty") else "；工作区干净"
    return (
        f"代码文件 {data.get('files', '?')} 个，代码行 {data.get('code_lines', '?')} 行"
        f"{dirty}"
    )


# ---------------------------------------------------------------------------
# 二、判断（模型，可缺席）
# ---------------------------------------------------------------------------
def _build_expert_prompt(group: dict[str, Any], evidence: dict[str, dict], overview: str) -> str:
    dims = "、".join(f"{DIMENSION_NAMES.get(d, d)}（id={d}）" for d in group["dimensions"])
    return (
        f"你负责评审「{group['name']}」这一组，覆盖维度：{dims}。\n\n"
        f"仓库概况：{overview}\n\n"
        "以下是已经跑完的取证结果（这些是事实，不要质疑也不要补充你没有看到的）：\n"
        f"{evidence_digest(evidence)}\n\n"
        "要求：\n"
        "1. 每个维度至少一条结论；结论的 evidence 字段必须引用上面的工具名与具体数字/文件名。\n"
        "2. **没有取到证的维度，verdict 填「未覆盖」，evidence 留空** —— 不许凭印象判「没问题」。\n"
        "3. verdict 只能是：阻断 / 建议 / 良好 / 未覆盖。\n"
        "4. 区分事实与建议：fact 写是什么，suggestion 写怎么改。\n"
        "5. 做得好的也要写出来，只报问题的评审没人愿意看第二遍。\n\n"
        "只输出 JSON，不要解释、不要代码围栏：\n"
        '{"findings":[{"dimension":"tests","verdict":"阻断","fact":"...","evidence":"check_tests: ...","suggestion":"..."}]}'
    )


def parse_findings(text: str) -> list[Finding]:
    """解析模型输出。模型经常不老实（加代码围栏、加前后缀），这里都要能兜住。

    解析不出来的部分**丢弃而不是猜**，丢了的维度会在汇总时落「未覆盖」。
    """
    if not text or not text.strip():
        return []
    cleaned = _strip_fence(text)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        log.warning("模型输出里没有 JSON 对象，本组结论丢弃")
        return []
    try:
        payload = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        log.warning("模型输出不是合法 JSON（%s），本组结论丢弃", exc)
        return []

    raw = payload.get("findings", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []

    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(Finding(**item))
        except Exception as exc:  # noqa: BLE001 - 单条不合规不该拖垮整组
            log.warning("跳过一条无法解析的结论：%s", exc)
    return findings


def _strip_fence(text: str) -> str:
    """剥掉 ```json ... ``` 围栏。真实模型几乎必然加，prompt 约束不住。"""
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    return match.group(1) if match else stripped


# 模型写档位时几乎不照抄：会写「阻断项」「建议改进」「做得好的地方」。
# 只做子串匹配会漏（「做得好的地方」里没有「良好」），所以要一张别名表。
# 顺序即优先级：先判阻断，最后兜底未覆盖——**兜底方向必须是「少说」**。
_VERDICT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (BLOCKER, ("阻断", "必须改", "blocker", "critical")),
    (SUGGESTION, ("建议", "改进", "suggestion", "优化")),
    (GOOD, ("良好", "做得好", "优点", "good", "没问题")),
)


def rule_based_findings(evidence: dict[str, dict]) -> list[Finding]:
    """不经过模型、由取证结果直接推出的结论。

    **能确定的就别问模型**——`has_tests_dir=false` 就是没有测试，
    让模型去「判断」一遍，只是花钱买一次幻觉的机会。
    这类结论天然带证据，成本为零，而且没有模型时也能出。

    覆盖四类（有没有测试、依赖钉没钉版本、有无疑似密钥、交付清单齐不齐）；
    像「分层是否合理」这种必须读代码才能说的，仍然留给模型或人工。

    ⚠️ **规则也要能判「良好」**，不能只报问题：
    只报问题的规则会让一个好项目被评为「八项全未覆盖」——
    明明查了且没问题，读起来却像没查。这和「未发现问题」一样危险，方向相反而已。
    """

    def _data(name: str) -> dict | None:
        item = evidence.get(name)
        return item["data"] if item and item.get("ok") and isinstance(item.get("data"), dict) else None

    findings: list[Finding] = []

    tests = _data("check_tests")
    if tests:
        n_files = tests.get("test_files", 0) or 0
        n_cases = tests.get("test_cases", 0) or 0
        # 判据必须是「有目录 **或** 有测试文件」，早先只看 has_tests_dir，
        # 于是 monorepo（顶层无 tests/、测试散在各 package 内）会被误判成
        # 「零测试」这个阻断项 —— 而且 fact 文案说「没有任何用例」，
        # 紧跟的 evidence 里却写着 test_cases=17，报告自相矛盾。
        if not tests.get("has_tests_dir") and n_files == 0:
            findings.append(Finding(
                dimension="tests", verdict=BLOCKER,
                fact="既没有测试目录，也没有任何测试文件",
                evidence=f"check_tests: has_tests_dir=false, test_files=0, test_cases={n_cases}",
                suggestion="至少补一条冒烟用例，钉住「能跑起来」这条底线",
            ))
        elif n_cases == 0:
            findings.append(Finding(
                dimension="tests", verdict=BLOCKER,
                fact=f"找到 {n_files} 个测试文件，但没识别出任何用例（可能是空壳或未被收录）",
                evidence=f"check_tests: test_files={n_files}, test_cases=0",
                suggestion="确认测试是否真的会被执行（CI 里跑一遍）",
            ))
        else:
            langs = tests.get("cases_by_lang") or {}
            lang_txt = "、".join(f"{k} {v}" for k, v in list(langs.items())[:4]) or "—"
            findings.append(Finding(
                dimension="tests", verdict=GOOD,
                fact=f"有测试：识别到 {n_cases} 处测试声明 / {n_files} 个测试文件",
                evidence=f"check_tests: has_tests_dir={tests.get('has_tests_dir')}, "
                         f"test_cases={n_cases}, 按语言={lang_txt}",
            ))

    deps = _data("dependency_audit")
    if deps and deps.get("found"):
        unpinned = deps.get("unpinned") or []
        if unpinned:
            findings.append(Finding(
                dimension="deps", verdict=BLOCKER,
                fact=f"{len(unpinned)} 个依赖未钉版本，构建不可复现",
                evidence=f"dependency_audit: unpinned={unpinned[:5]}",
                suggestion="钉到具体版本，或用 lock 文件",
            ))
        else:
            findings.append(Finding(
                dimension="deps", verdict=GOOD,
                fact="requirements.txt 里的依赖全部钉住版本",
                evidence=f"dependency_audit: pinned={deps.get('pinned')}, unpinned=[]",
            ))
        # 没有 requirements.txt（改用 pyproject / Poetry 等）时不判——
        # 判「没有依赖声明」是错的，只是声明方式不同

    secrets = _data("scan_secrets")
    if secrets:
        if secrets.get("count"):
            hits = secrets.get("hits") or []
            where = "、".join(f"{h.get('file')}:{h.get('line')}" for h in hits[:3])
            findings.append(Finding(
                dimension="config", verdict=BLOCKER,
                fact=f"疑似硬编码密钥 {secrets['count']} 处（命中 ≠ 泄密，示例值与占位符已剔除，仍需人工复核）",
                evidence=f"scan_secrets: count={secrets['count']}（{where}）",
                suggestion="逐条人工复核；确认为真密钥的改为环境变量读取并立即轮换",
            ))
        else:
            findings.append(Finding(
                dimension="config", verdict=GOOD,
                fact="未发现疑似硬编码密钥（示例值/占位符、环境变量引用、测试夹具、localhost 默认凭据均已降级）",
                evidence=f"scan_secrets: count=0, local_defaults={secrets.get('local_defaults', 0)}, "
                         f"env_refs={secrets.get('env_refs', 0)}, test_file_hits={secrets.get('test_file_hits', 0)}",
            ))

    checklist = _data("project_checklist")
    if checklist:
        missing = set(checklist.get("missing") or [])
        present = set(checklist.get("present") or [])
        ci_key = ".github/workflows"
        if ci_key in missing:
            findings.append(Finding(
                dimension="ci", verdict=BLOCKER,
                fact="没有 CI 配置，改动没有自动验证",
                evidence=f"project_checklist: missing 含 {ci_key}",
                suggestion="先加一条 lint + test 的流水线，跑不起来就别谈质量",
            ))
        elif ci_key in present:
            findings.append(Finding(
                dimension="ci", verdict=GOOD,
                fact="有 CI 配置（.github/workflows）",
                evidence="project_checklist: .github/workflows 存在",
            ))
        others = sorted(missing - {ci_key, "LICENSE"})
        if others:
            findings.append(Finding(
                dimension="docs", verdict=SUGGESTION,
                fact=f"交付清单缺 {len(others)} 项：{'、'.join(others)}",
                evidence=f"project_checklist: missing={sorted(missing)}",
                suggestion="CHANGELOG 与 CONTRIBUTING 是别人愿意参与的前提，建议补齐",
            ))

    return findings


def _keep_owned_dimensions(
    findings: list[Finding], owned: tuple[str, ...], expert: str
) -> list[Finding]:
    """只保留这个专家被分配到的维度，其余丢弃。

    实测（deepseek，评 pallets/click）里模型会把结论挂到组内的别的维度上：
    拿「注释率 5.3%」讲测试、拿「未发现硬编码密钥」讲统一错误处理。
    dimension 字段没填错（是它组内的），但**内容与维度不相关**。
    内容相关性没法用规则判，能做且该做的是先把越界的维度挡掉，
    免得一个专家的臆测污染它根本没被分配去评审的维度。
    """
    kept: list[Finding] = []
    for item in findings:
        if _dimension_id(item.dimension) in owned:
            kept.append(item)
        else:
            log.warning("专家 %s 给出了不属于它的维度（%s），丢弃", expert, item.dimension)
    return kept


def _normalize_verdict(value: str) -> str:
    """把模型写的各种变体统一回四档；认不出来就落「未覆盖」。"""
    text = value or ""
    for verdict, aliases in _VERDICT_ALIASES:
        for alias in aliases:
            if alias in text:
                return verdict
    return UNCOVERED


def _enforce_evidence(findings: list[Finding], *, degrade_without_evidence: bool = True) -> list[Finding]:
    """硬约束：没有证据的结论一律改判「未覆盖」。

    这是整个评审防幻觉的最后一道闸。**宁可少说，不可编。**
    """
    enforced: list[Finding] = []
    for item in findings:
        verdict = _normalize_verdict(item.verdict or "")
        if degrade_without_evidence and not item.evidence.strip() and verdict != UNCOVERED:
            log.info("结论缺证据，改判未覆盖：%s", item.fact[:40])
            item = item.model_copy(update={"verdict": UNCOVERED, "suggestion": item.suggestion or "需人工确认"})
        else:
            item = item.model_copy(update={"verdict": verdict})
        enforced.append(item)
    return enforced


# ---------------------------------------------------------------------------
# 三、编排
# ---------------------------------------------------------------------------
def run_review(
    root: str | Path,
    *,
    judge: Callable[[str], str] | None = None,
    max_workers: int = 4,
    timeout: float = 60.0,
) -> ReviewReport:
    """跑一次评审。

    Args:
        root: 被评仓库路径
        judge: 判断函数，输入 prompt 输出模型文本。
               **传 None 则降级**：只出证据清单，不下结论（`degraded=True`）
        max_workers: 同时几个专家在判断
        timeout: 整轮判断的墙钟上限

    两段式：先串行取证（进程级 set_root，不能并发），再并行判断。
    """
    # 用规范化后的路径：Git Bash 传进来的 `/e/foo` 若原样显示，报告里会印成 `\e\foo`
    root_path = _normalize_root(root).resolve()
    all_tools = tuple(dict.fromkeys(t for g in EXPERT_GROUPS for t in g["tools"]))
    evidence = collect_evidence(root_path, all_tools)
    overview = repo_digest(evidence)

    degraded = judge is None
    if degraded:
        log.warning("未提供判断模型，本次只出证据清单，不下结论")

    def _worker(task: TaskSpec) -> list[Finding]:
        group = next(g for g in EXPERT_GROUPS if g["name"] == task.name)
        mine = {name: evidence[name] for name in group["tools"] if name in evidence}
        if judge is None:
            return []
        findings = parse_findings(judge(_build_expert_prompt(group, mine, overview)))
        return _keep_owned_dimensions(findings, group["dimensions"], group["name"])

    tasks = [TaskSpec(name=g["name"], goal=f"评审 {g['name']}", shared=overview) for g in EXPERT_GROUPS]
    results = run_fanout(tasks, _worker, max_workers=max_workers, timeout=timeout)

    model_findings: list[Finding] = []
    for item in results:
        if item.ok and isinstance(item.output, list):
            model_findings.extend(item.output)
        elif not item.ok:
            log.warning("专家 %s 未交回执：%s", item.name, item.error or "超时")

    model_findings = _enforce_evidence(model_findings)

    # 规则结论只补「模型没给出结论」的维度，避免同一个维度出两条打架的结论
    covered = {_dimension_id(f.dimension) for f in model_findings}
    findings = model_findings + [
        f for f in rule_based_findings(evidence) if _dimension_id(f.dimension) not in covered
    ]

    # 冲突：专家互不通信，只有主 agent 能发现结论打架
    conflicts = [
        c.to_text() for c in detect_conflicts(
            [TaskResult(name=f.dimension, ok=True, output=f"{f.fact} {f.suggestion}")
             for f in findings]
        )
    ]

    judged = {_dimension_id(f.dimension) for f in findings if f.verdict != UNCOVERED}
    uncovered = [name for dim, name in DIMENSIONS if dim not in judged]

    headline = _headline(findings, uncovered, degraded)
    return ReviewReport(
        repo=str(root_path),
        # 与 tools._now() 同一套口径：UTC 取值后转本地时区，
        # naive 的 datetime 存进数据库会丢时区，跨时区读必错乱
        generated_at=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        headline=headline,
        findings=findings,
        conflicts=conflicts,
        uncovered=uncovered,
        degraded=degraded,
    )


def _dimension_id(value: str) -> str:
    """模型可能填中文名也可能填 id，两个都认。"""
    if value in DIMENSION_NAMES:
        return value
    for dim, name in DIMENSIONS:
        if name == value:
            return dim
    return value


def _headline(findings: list[Finding], uncovered: list[str], degraded: bool) -> str:
    blockers = [f for f in findings if f.verdict == BLOCKER]
    suffix = ""
    if degraded:
        suffix = "（未接入判断模型：以下结论全部由取证规则直接推出，未经模型判断）"
    if blockers:
        return f"{len(blockers)} 项阻断，需先处理后再进入下一阶段{suffix}"
    if uncovered:
        return f"未发现阻断项，但 {len(uncovered)} 个维度缺证据、需人工确认{suffix}"
    return f"合格：八项均已取证，未发现阻断项{suffix}"


# ---------------------------------------------------------------------------
# 四、渲染
# ---------------------------------------------------------------------------
def render_markdown(report: ReviewReport, *, evidence: dict[str, dict] | None = None) -> str:
    """报告渲染成 Markdown。格式固定，不自由发挥——评审报告要给多人传阅。"""
    lines: list[str] = [
        "# 工程健康度评审报告",
        "",
        f"- 仓库：`{report.repo}`",
        f"- 生成时间：{report.generated_at}",
        "",
        "## 结论",
        "",
        report.headline,
        "",
    ]

    if report.degraded:
        lines += [
            "> ⚠️ 本次为**降级报告**：没有可用的判断模型。",
            "> 下面的结论全部由取证规则直接推出（有没有测试、依赖钉没钉版本、有没有疑似密钥），",
            "> 其余维度需要读代码才能判断，未接入模型前一律记「未覆盖」，不含模型判断。",
            "",
        ]

    def _section(title: str, items: list[Finding]) -> None:
        if not items:
            return
        lines.append(f"## {title}")
        lines.append("")
        for item in items:
            head = f"- [{DIMENSION_NAMES.get(_dimension_id(item.dimension), item.dimension)}] {item.fact}"
            lines.append(head)
            if item.evidence:
                lines.append(f"  - 证据：{item.evidence}")
            if item.suggestion:
                lines.append(f"  - 建议：{item.suggestion}")
        lines.append("")

    _section("阻断项（必须改）", report.blockers())
    _section("建议项（改了更好）", report.suggestions())
    _section("做得好的地方", report.goods())

    lines.append("## 未覆盖")
    lines.append("")
    if report.uncovered:
        for name in report.uncovered:
            lines.append(f"- {name}：本次未取到证据，需人工确认")
    else:
        lines.append("- 无（八项均已取证）")
    lines.append("")

    if report.conflicts:
        lines.append("## 专家结论冲突")
        lines.append("")
        for text in report.conflicts:
            lines.append(f"- {text}")
        lines.append("")

    if evidence:
        lines.append("## 取证原始输出")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
