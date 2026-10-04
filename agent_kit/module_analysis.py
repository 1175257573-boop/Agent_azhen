"""模块职责分析：让模型基于摘要说出「这个模块是干什么的」。

这是评审链路的最后一环
----------------------
取证工具给数字，画像给结构，摘要给线索，三者都**回答不了**
"这个模块负责什么"。只有把摘要交给模型，才第一次真正得到语义——
而这正是「分析各模块功能」的落点。

为什么要分批
------------
把所有模块的摘要塞进一次调用，会有两个问题：
    · **互相干扰**：模型会把 A 模块的职责套到 B 上（它们通常相邻且相似）
    · **注意力被摊薄**：一次看 8 个模块，每个都只能给几十个字

所以按 `batch_size` 切批（默认 3），每批独立判断。

为什么强调「信息不足就说不充足」
--------------------------------
陌生仓库的摘要经常不足以判断职责——一个只有 80 行、没有 README、
导出符号只有两个的工具包，凭空编一句"负责提供配置解析能力"是最坏的结果：
它看起来像结论，实际上是幻觉。所以 prompt 里明确要求承认信息不足，
且每条结论必须带 `evidence`（依据摘要里的哪些内容），便于人工复核。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_kit.logging_conf import get_logger
from agent_kit.module_digest import build_module_digest
from agent_kit.modules import ModuleProfile, ModuleScan
from agent_kit.multi_agent.fanout import TaskResult, TaskSpec, run_fanout
from agent_kit.multi_agent.review import _short_reason

log = get_logger("module_analysis")

#: 每批几个模块。3 是折中：太少会让调用次数多，太多会互相干扰。
DEFAULT_BATCH_SIZE = 3

#: 默认分析多少个模块。取 Top N（按重要性）而不是全量——
#: 200 个模块全部分析一次要几百次调用，那不是评审，是扫货。
DEFAULT_TOP_N = 9

_SYSTEM_PROMPT = """\
你是一个资深工程分析专家。下面给出若干个模块的结构摘要（来自真实代码扫描）。

请为**每一个**模块回答：这个模块是干什么的？它在整体系统里扮演什么角色？

硬性要求：
1. **只根据给出的摘要作答**。摘要里没有的信息不要补充、不要想象。
2. 如果摘要不足以判断职责，就写"信息不足"，并在 evidence 里说明缺什么。
   这比编一个听起来合理的答案有价值得多。
3. `evidence` 必须指出摘要里的具体依据（哪个结构、哪些导出符号、哪段自述）。
4. `concerns` 只填你**确有依据**的疑点（例如"只有入口没有测试"），
   没有就给空数组，不要凑数。
5. 用与代码相同的语言书写（中文摘要就用中文）。

只输出 JSON，不要任何解释文字或代码围栏：
{"modules": [{"name": "模块名", "purpose": "职责说明", "concerns": ["疑点"], "evidence": "依据"}]}

`name` 必须**原样复制**上面标题里的模块名（例：`@deepseek-ai/dsh`），
不要附加路径、括号或编号。
"""


@dataclass
class ModuleUnderstanding:
    """一个模块的职责理解。"""

    name: str
    path: str
    purpose: str
    concerns: list[str] = field(default_factory=list)
    evidence: str = ""
    judged: bool = True          # False = 模型没给出结论，只给了画像
    error: str = ""              # judged=False 时的原因

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path, "purpose": self.purpose,
            "concerns": self.concerns, "evidence": self.evidence,
            "judged": self.judged, "error": self.error,
        }


@dataclass
class ModuleAnalysisResult:
    modules: list[ModuleUnderstanding] = field(default_factory=list)
    batches: int = 0
    failures: list[str] = field(default_factory=list)
    degraded: bool = False       # True = 没有判断模型，只给了画像

    def to_dict(self) -> dict:
        return {
            "batches": self.batches,
            "degraded": self.degraded,
            "failures": self.failures,
            "modules": [m.to_dict() for m in self.modules],
        }


# ---------------------------------------------------------------- 主流程


def analyze_modules(
    root: Path,
    scan: ModuleScan,
    judge: Callable[[str], str] | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_chars: int = 2600,
    max_workers: int = 3,
    timeout: float = 120.0,
) -> ModuleAnalysisResult:
    """分析 Top N 模块的职责。

    Args:
        judge: 判断函数；传 None 则**降级**——只给结构化画像，不下职责结论。
            降级时同样要说清"没分析"，不能让读者以为那���职责是结论。
    """
    targets = scan.top(top_n) if top_n else list(scan.modules)
    if not targets:
        return ModuleAnalysisResult(degraded=judge is None)

    digests = {p.name: build_module_digest(root, p, max_chars=max_chars) for p in targets}
    result = ModuleAnalysisResult(
        modules=[_placeholder(p) for p in targets],
        degraded=judge is None,
    )
    if judge is None:
        log.warning("未提供判断模型，模块职责只给画像不下结论")
        return result

    batches = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]
    result.batches = len(batches)
    # TaskResult 里没有任务上下文（只有 name/ok/output/error），
    # 所以批次与模块的对应关系要自己记一份，供回执阶段定位。
    batch_members: dict[str, list[str]] = {}
    tasks = []
    for i, group in enumerate(batches):
        name = f"模块批{i + 1}"
        batch_members[name] = [p.name for p in group]
        tasks.append(TaskSpec(
            name=name,
            goal=f"分析 {len(group)} 个模块的职责",
            shared=[p.name for p in group],
        ))

    def _worker(task: TaskSpec) -> list[dict]:
        group = [p for p in targets if p.name in (task.shared or [])]
        return _parse(judge(_build_prompt(group, digests)))

    results: list[TaskResult] = run_fanout(
        tasks, _worker, max_workers=max_workers, timeout=timeout)

    understood: dict[str, ModuleUnderstanding] = {}
    for item in results:
        names = batch_members.get(item.name, [])
        group = [p for p in targets if p.name in names]
        if not item.ok or not isinstance(item.output, list):
            reason = _short_reason(item.error)
            result.failures.append(f"{item.name}：{reason}")
            log.warning("模块批 %s 未交回执：%s", item.name, reason)
            for p in group:
                understood[p.name] = _failed(p, reason)
            continue
        returned = {str(x.get("name", "")).strip(): x for x in item.output if isinstance(x, dict)}
        for p in group:
            data = _match_returned(returned, p)
            if not data:
                # 模型漏了这个模块（或把名字写成了别的形式）：
                # 明确标成未给出，而不是替它编一个
                understood[p.name] = _failed(p, "模型未返回该模块的结论")
                continue
            understood[p.name] = _from_model(p, data)

    # 模型可能报出不在目标里的模块名，忽略即可（不要凭它编造模块）
    result.modules = [understood.get(p.name) or _failed(p, "未处理") for p in targets]
    if result.failures and not any(m.judged for m in result.modules):
        result.degraded = True
    return result


# ---------------------------------------------------------------- 组装


def _placeholder(p: ModuleProfile) -> ModuleUnderstanding:
    """降级时的占位：只说画像，不编职责。"""
    bits = [f"{p.language or '未知语言'}，{p.loc} 行"]
    if p.entry_points:
        bits.append("含可执行入口")
    return ModuleUnderstanding(
        name=p.name, path=p.path,
        purpose=f"（未接入判断模型，只给出结构画像：{'，'.join(bits)}）",
        judged=False, error="no-judge",
    )


def _failed(p: ModuleProfile, reason: str) -> ModuleUnderstanding:
    return ModuleUnderstanding(
        name=p.name, path=p.path,
        purpose=f"（未取得结论：{reason}）",
        judged=False, error=reason,
    )


def _build_prompt(group: list[ModuleProfile], digests: dict[str, str]) -> str:
    blocks = [_SYSTEM_PROMPT]
    for p in group:
        blocks.append(f"### 模块 {p.name}（路径 {p.path}）\n{digests.get(p.name, '（摘要缺失）')}")
    return "\n\n".join(blocks)


def _parse(raw: str) -> list[dict]:
    """解析模型返回。只接受 dict 列表，别的形状一律丢弃。"""
    if not raw:
        return []
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = _loads_lenient(text)
    if data is None:
        return []
    if isinstance(data, dict):
        for key in ("modules", "results", "items", "data"):
            val = data.get(key)
            if isinstance(val, list):
                data = val
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        purpose = str(item.get("purpose") or item.get("role") or item.get("description") or "").strip()
        if not purpose:
            continue
        concerns = item.get("concerns") or item.get("risks") or []
        out.append({
            "name": str(item.get("name") or item.get("module") or "").strip(),
            "purpose": purpose,
            "concerns": [str(c) for c in concerns if str(c).strip()] if isinstance(concerns, list) else [],
            "evidence": str(item.get("evidence") or item.get("basis") or "").strip(),
        })
    return out


def _strip_paren(text: str) -> str:
    """去掉尾部的中英文括号内容：`@deepseek-ai/dsh（apps/cli）` → `@deepseek-ai/dsh`。"""
    return re.sub(r"\s*[（(][^)）]*[)）]\s*$", "", text).strip()


def _match_returned(returned: dict[str, dict], p: ModuleProfile) -> dict | None:
    """把模型返回的条目对回目标模块。

    为什么不能直接 `returned.get(p.name)`——实测模型会把 name 写成
    `@deepseek-ai/dsh（apps/cli）`（照抄 prompt 里的标题格式），
    精确匹配直接失配，于是三个模块被误判成"模型没返回"，
    明明有高质量结论却丢掉了（端到端实测踩过）。

    这里按可靠性从高到低逐级回退，**不做模糊包含**——
    `dsh` 是 `dsh-session` 的前缀，模糊匹配会把结论串到别的模块上，
    那比匹配失败严重得多：结论张冠李戴却看不出来。
    """
    base = p.name.rsplit("/", 1)[-1]
    for key in (p.name, p.path, f"{p.name}（{p.path}）", f"{p.name}({p.path})"):
        if key in returned:
            return returned[key]
    for key, value in returned.items():
        norm = _strip_paren(key)
        if norm in (p.name, p.path, base) or _strip_paren(norm) in (p.name, base):
            return value
    return None


def _loads_lenient(text: str):
    """尽量把模型的输出解成 JSON。

    模型的返回形态实测有四种：纯 JSON、包在 ```json 围栏里、
    JSON 前后带一句客套话、以及**顶层直接是数组**。
    早先只找第一个 `{` 去截，数组形态会连不上（`[{...}]` 截出来尾部多一个 `]`）。
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener in ("{", "["):
        start = text.find(opener)
        if start < 0:
            continue
        for closer in ("}", "]"):
            end = text.rfind(closer)
            if end > start:
                try:
                    return json.loads(text[start:end + 1])
                except ValueError:
                    continue
    return None


def _from_model(p: ModuleProfile, data: dict) -> ModuleUnderstanding:
    return ModuleUnderstanding(
        name=p.name,
        path=p.path,
        purpose=str(data.get("purpose", "")).strip(),
        concerns=list(data.get("concerns") or []),
        evidence=str(data.get("evidence", "")).strip(),
        judged=True,
    )
