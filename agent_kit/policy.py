"""审批与沙箱策略（对标 Codex 的 `approval_policy` + `sandbox_mode`）。

以前这两个开关是散的：`--role admin` 管写工具、`--no-hitl` 管二次确认、
运行时目录写死在 `workspace/runs`。三处各管一段，谁也说不清「当前到底放不放行」。
现在收敛成两个档位，一处解析、处处可见（`main.py info` 会打印）。

审批档位（**何时需要人来拍板**）：
    untrusted  —— 写操作**调用前**一律先问（HumanInTheLoop 开，可 approve / edit / reject）
    on-failure —— 默认放行；写工具**执行失败**后不再让模型自己绕路，标记需人工复核
    never      —— 全程不拦（等价于旧的 `--no-hitl`）

沙箱档位（**能写到哪**）：
    read-only          —— 压根不装载写工具
    workspace-write    —— 写工具可用，落盘限制在 workspace 内（默认）
    danger-full-access —— 装载全部写工具并放宽到 workspace 根（**仍在 workspace 内**，
                          启动时会打警告）

⚠️ 与 Codex 的差异要如实说明：Codex 的沙箱是 OS 级隔离（macOS seatbelt /
Linux landlock / Windows 沙箱），本项目只是**路径约束 + 中间件拦截**，
不是真沙箱。`danger-full-access` 在 Codex 里是「关掉隔离」，在这里只是
「写到工作区根目录」，两者强度不同，别在简历/文档里混为一谈。

取值优先级：**CLI 显式传参 > 环境变量 > atlas.toml > 代码默认值**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent_kit.config import load_atlas_config

APPROVAL_POLICIES = ("untrusted", "on-failure", "never")
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

ENV_APPROVAL = "ATLAS_APPROVAL"
ENV_SANDBOX = "ATLAS_SANDBOX"

# 默认取最严的一档：**写操作一律先问**。改宽要在 atlas.toml 里显式声明，
# 让"放宽"这件事留下痕迹，而不是默认就放行。
DEFAULT_APPROVAL = "untrusted"
DEFAULT_SANDBOX = "workspace-write"


@dataclass(frozen=True)
class Policy:
    """一次运行的最终策略。**派生字段全部在这里算好**，调用方不用自己拼规则。"""

    approval: str
    sandbox: str
    hitl: bool              # 是否装载 HumanInTheLoopMiddleware
    write_tools: bool       # 是否装载写工具
    readonly: bool          # 是否装载只读强制器
    escalate_on_failure: bool   # 写工具失败是否升级为「需人工复核」
    source: str             # 取值来源，便于排查「为什么没拦住我」
    warnings: tuple[str, ...] = ()


def _pick(cli_value: str | None, env_name: str, toml_value: str, allowed: tuple[str, ...], default: str):
    """按优先级挑一个合法值，非法值一律回落默认并记录来源。"""
    for value, where in (
        (cli_value, "CLI 参数"),
        (os.getenv(env_name, "").strip() or None, f"环境变量 {env_name}"),
        (toml_value, "atlas.toml"),
    ):
        if value:
            if value in allowed:
                return value, where
            return default, f"{where} 的 {value!r} 非法，已回落"
    return default, "代码默认值"


def resolve(
    *,
    cli_approval: str | None = None,
    cli_sandbox: str | None = None,
    cli_hitl: bool | None = None,
    role: str = "admin",
) -> Policy:
    """解析最终策略。

    Args:
        cli_approval: CLI `--approval`，最高优先级
        cli_sandbox: CLI `--sandbox`，最高优先级
        cli_hitl: 旧开关的语义（True=开二次确认 / False=显式关闭 / None=未指定，走配置）
        role: 角色；非 admin 一律按只读处理（与旧行为一致）
    """
    cfg = load_atlas_config()

    # 旧开关 --no-hitl 等价于 approval=never，仍然认，优先级同 CLI 参数
    if cli_hitl is False and cli_approval is None:
        cli_approval = "never"
    elif cli_hitl is True and cli_approval is None:
        cli_approval = "untrusted"

    # 只有**显式写进 atlas.toml** 的字段才算数；用 pydantic 的 model_fields_set 区分
    # 「用户写了」与「schema 的默认值」，否则来源会永远显示 atlas.toml
    toml_approval = cfg.approval_policy.value if "approval_policy" in cfg.model_fields_set else ""
    toml_sandbox = cfg.sandbox.mode if "sandbox" in cfg.model_fields_set else ""

    approval, approval_from = _pick(
        cli_approval, ENV_APPROVAL, toml_approval, APPROVAL_POLICIES, DEFAULT_APPROVAL
    )
    sandbox, sandbox_from = _pick(
        cli_sandbox, ENV_SANDBOX, toml_sandbox, SANDBOX_MODES, DEFAULT_SANDBOX
    )

    warnings: list[str] = []
    if role != "admin" and sandbox != "read-only":
        sandbox = "read-only"
        warnings.append(f"角色 {role} 非 admin，沙箱已强制降为 read-only")
    if sandbox == "danger-full-access":
        warnings.append("danger-full-access：写范围放宽到工作区根目录（仍不出工作区，非 OS 级沙箱）")

    return Policy(
        approval=approval,
        sandbox=sandbox,
        hitl=approval == "untrusted",
        write_tools=sandbox != "read-only",
        readonly=sandbox == "read-only" or role != "admin",
        escalate_on_failure=approval == "on-failure",
        source=f"approval←{approval_from}；sandbox←{sandbox_from}",
        warnings=tuple(warnings),
    )


def describe(policy: Policy) -> str:
    """给 `main.py info` 用的一行说明。"""
    hitl = "调用前需人工确认" if policy.hitl else ("失败后转人工复核" if policy.escalate_on_failure else "不拦截")
    return (
        f"审批 {policy.approval}（{hitl}）｜沙箱 {policy.sandbox}"
        f"（{'装载写工具' if policy.write_tools else '不装载写工具'}）"
    )
