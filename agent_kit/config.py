"""配置与模型工厂。

**密钥优先从系统环境变量读取**（推荐，不落盘）。
若项目根目录存在 `.env`，则作为**补充来源**：只填充环境变量里缺失的项，
已存在的环境变量优先——这样既方便本地调试，又不会覆盖系统级配置。
`.env` 已在 .gitignore 中，绝不会进版本库。

可用环境变量：
    LLM_PROVIDER        fake | openai | deepseek | dashscope | anthropic   （默认自动探测）
    LLM_MODEL           模型名，不填则使用该 provider 的默认型号
    OPENAI_API_KEY      OpenAI / 任意 OpenAI 兼容服务
    OPENAI_BASE_URL     可选，OpenAI 兼容服务的 endpoint
    DEEPSEEK_API_KEY    DeepSeek
    DEEPSEEK_BASE_URL   可选，默认 https://api.deepseek.com
    DASHSCOPE_API_KEY   阿里云百炼 DashScope（OpenAI 兼容模式）
    DASHSCOPE_BASE_URL  可选，默认 https://dashscope.aliyuncs.com/compatible-mode/v1
    ANTHROPIC_API_KEY   Anthropic Claude
    ANTHROPIC_BASE_URL  可选

Windows 持久化写法（重启终端后依然有效）：
    setx DASHSCOPE_API_KEY "sk-xxxxx"
    setx LLM_PROVIDER dashscope
临时生效（仅当前终端）：
    $env:DASHSCOPE_API_KEY = "sk-xxxxx"
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from agent_kit.home import atlas_home
from agent_kit.logging_conf import get_logger


def _load_dotenv_once() -> None:
    """可选：从**项目根目录**的 .env 补齐环境变量里缺失的项（系统变量优先）。

    只认项目根目录这一处，不向上遍历——避免意外加载到上层目录的 .env。
    没有 .env 文件时本函数是空操作，密钥依然完全走环境变量。
    """
    dotenv_path = Path(__file__).resolve().parent.parent / ".env"
    if not dotenv_path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # 没装 python-dotenv 就跳过，不影响运行
        return
    load_dotenv(dotenv_path, override=False)   # override=False → 已存在的环境变量不被覆盖


_load_dotenv_once()

log = get_logger("agent.config")

DEFAULT_MODELS: dict[str, str] = {
    "fake": "scripted",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    # 阿里云百炼 DashScope（OpenAI 兼容模式）常用型号：qwen-plus / qwen-max / qwen-turbo
    "dashscope": "qwen-plus",
    "anthropic": "claude-sonnet-4-5",
}

# 每个 provider 依赖的必填环境变量；有多个候选名时按顺序取第一个命中的
REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_TOKEN"),
    "dashscope": ("DASHSCOPE_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
}

# 可选环境变量：存在时才用它覆盖 SDK 的默认 endpoint
OPTIONAL_BASE_URL_ENV: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
    "deepseek": "DEEPSEEK_BASE_URL",
    "dashscope": "DASHSCOPE_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
}

# DashScope 的 OpenAI 兼容端点。
# 中国大陆站用下面这个；国际站请设环境变量 DASHSCOPE_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
DASHSCOPE_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def get_env_key_name(provider: str) -> str | None:
    """返回该 provider 实际命中的环境变量名（不含值），用于提示，不泄露密钥。"""
    for name in REQUIRED_ENV.get(provider, ()):
        if os.getenv(name):
            return name
    return None


def get_api_key(provider: str) -> str | None:
    """从环境变量取密钥内容。**永远不要把返回值打印或写进日志。**"""
    for name in REQUIRED_ENV.get(provider, ()):
        value = os.getenv(name)
        if value:
            return value
    return None


def mask(value: str) -> str:
    """把密钥打码成 sk-ab***yz 的形态，仅用于 UI 展示。"""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}***{value[-4:]}"


@dataclass
class AgentSettings:
    """集中管理可调参数。

    优先级：显式传参 > 环境变量 > 代码默认值。
    """

    provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER")
        or load_atlas_config().model_provider
        or detect_provider()
    )
    model_name: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048

    # 安全与稳定性
    model_call_limit: int = 8
    tool_call_limit_per_run: int = 12
    summarize_trigger: int = 12
    summarize_keep: int = 6

    workspace: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    def __post_init__(self) -> None:
        # 显式传 None / 空串时，按「环境变量里配了哪个 Key」自动选 provider
        self.provider = (self.provider or detect_provider()).lower()
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"未知 provider：{self.provider}，可选 {list(DEFAULT_MODELS)}")
        if not self.model_name:
            self.model_name = (
                os.getenv("LLM_MODEL")
                or load_atlas_config().model_name
                or DEFAULT_MODELS[self.provider]
            )

    @property
    def sandbox_dir(self) -> Path:
        """Agent 写文件的唯一允许目录（防止越权落盘）。"""
        p = self.workspace / "runs"
        p.mkdir(parents=True, exist_ok=True)
        return p


def build_chat_model(settings: AgentSettings) -> Any:
    """按 provider 构造 ChatModel，密钥来源为环境变量。"""
    provider = settings.provider

    if provider == "fake":
        from agent_kit.scripted_model import build_scripted_model

        return build_scripted_model()

    api_key = get_api_key(provider)
    base_url_var = OPTIONAL_BASE_URL_ENV.get(provider)
    base_url = os.getenv(base_url_var) if base_url_var else None

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {}
        if base_url:  # OpenAI 兼容服务（如各类中转、本地 vLLM / Ollama）
            kwargs["base_url"] = base_url
        return ChatOpenAI(
            model=settings.model_name,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            api_key=api_key,
            **kwargs,
        )

    if provider == "deepseek":
        # DeepSeek 走 OpenAI 兼容协议，只换 endpoint
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.model_name,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            api_key=api_key,
            base_url=base_url or "https://api.deepseek.com",
        )

    if provider == "dashscope":
        # 阿里云百炼 DashScope：官方提供 OpenAI 兼容模式，所以用 langchain-openai 即可，
        # 关键是 base_url 必须带 /compatible-mode/v1，否则会 404。
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.model_name,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            api_key=api_key,
            base_url=base_url or DASHSCOPE_DEFAULT_BASE_URL,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs2: dict[str, Any] = {}
        if base_url:
            kwargs2["base_url"] = base_url
        return ChatAnthropic(
            model=settings.model_name,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            api_key=api_key,
            **kwargs2,
        )

    raise ValueError(f"未处理的 provider：{provider}")


def detect_provider() -> str:
    """环境变量里配了哪个 Key，就默认用哪个 provider；都没配则回落到 fake。"""
    for provider in ("openai", "deepseek", "dashscope", "anthropic"):
        if get_api_key(provider):
            return provider
    return "fake"


# ---------------------------------------------------------------------------
# TOML 配置层（对标 Codex 的 config.toml + config-schema）
# ---------------------------------------------------------------------------
# 为什么还要一层 TOML：环境变量适合放密钥（不落盘），但不适合放「一改就是一组」的
# 运行参数——记忆后端、检索器、审批策略这些，写在文件里能进版本库、能评审、能复现。
# 分工照旧：**密钥只走环境变量**，TOML 里出现类密钥字段会直接报校验错。
#
# 优先级：**环境变量 > 项目根 atlas.toml > ATLAS_HOME/atlas.toml > 代码默认值**。
# 环境变量仍然最高，保证已有用法不被破坏。
#
# 示例 atlas.toml：
#     model_name = "qwen-plus"
#     model_provider = "dashscope"
#     [memory]
#     short_term = "sqlite"
#     long_term = "sqlite"
#     [retrieval]
#     name = "keyword"
#     [approval_policy]
#     value = "on-failure"
#     [sandbox]
#     mode = "workspace-write"

CONFIG_FILENAME = "atlas.toml"

# 类密钥字段名：出现在 TOML 里直接拒绝，避免把密钥写进版本库
_FORBIDDEN_KEYS = ("api_key", "token", "secret", "password")


def _load_toml_module() -> Any:
    """TOML 解析器：3.11+ 用标准库 tomllib，低版本回落到 tomli（可选依赖）。"""
    import sys

    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib
    try:
        import tomli  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "Python 3.10 及以下读取 atlas.toml 需要 tomli：pip install tomli>=2.0"
        ) from exc
    return tomli


def config_search_paths() -> list[Path]:
    """配置文件搜索顺序：**项目根**在前（项目级覆盖用户级），家目录在后。"""
    project_root = Path(__file__).resolve().parent.parent
    return [project_root / CONFIG_FILENAME, atlas_home() / CONFIG_FILENAME]


def _load_toml() -> tuple[dict[str, Any], Path | None]:
    """读第一个存在的配置文件。全部不存在时返回空字典（走默认值）。"""
    for path in config_search_paths():
        if not path.is_file():
            continue
        module = _load_toml_module()
        try:
            with path.open("rb") as fh:
                return module.load(fh), path
        except (OSError, ValueError) as exc:
            log.warning("配置文件 %s 解析失败，已忽略：%s", path, exc)
            continue
    return {}, None


class MemoryConfig(BaseModel):
    """记忆后端配置。默认 sqlite——标准库自带、零配置、重启不丢。"""

    short_term: Literal["memory", "sqlite", "redis"] = "sqlite"
    long_term: Literal["memory", "sqlite", "postgres"] = "sqlite"


class RetrievalConfig(BaseModel):
    """检索器配置。`name` 指的是检索器注册表里注册过的名字。"""

    name: str = "keyword"


class ApprovalConfig(BaseModel):
    """写操作/敏感操作的审批档位（对标 Codex 的 approval_policy）。"""

    value: Literal["untrusted", "on-failure", "never"] = "on-failure"


class SandboxConfig(BaseModel):
    """写文件的作用域档位。映射到项目内已有的 role 权限体系。"""

    mode: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"


class AtlasConfig(BaseModel):
    """atlas.toml 的完整 schema。字段缺失即取默认值，多余字段会报错（早失败优于静默）。"""

    model_config = {"extra": "forbid"}

    # 字段名叫 model_name 而不是 model：pydantic 的 BaseModel 保留了 model_* 前缀
    # （model_dump / model_validate / model_config...），字段名用 model 会被遮蔽。
    model_name: str | None = None
    model_provider: str | None = None
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    approval_policy: ApprovalConfig = Field(default_factory=ApprovalConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @model_validator(mode="before")
    @classmethod
    def _reject_secrets(cls, data: Any) -> Any:
        """密钥只能走环境变量。TOML 里出现类密钥字段直接拒绝。"""
        if isinstance(data, dict):
            flat = json.dumps(data, ensure_ascii=False).lower()
            for bad in _FORBIDDEN_KEYS:
                if f'"{bad}"' in flat:
                    raise ValueError(f"atlas.toml 不允许出现密钥字段 {bad!r}，请改用环境变量")
        return data


_CACHE: dict[str, Any] = {}


def load_atlas_config(*, reload: bool = False) -> AtlasConfig:
    """加载 atlas.toml（带缓存）。校验失败时**降级为默认配置并告警**，不让启动挂掉。"""
    if not reload and "cfg" in _CACHE:
        return _CACHE["cfg"]
    raw, path = _load_toml()
    try:
        cfg = AtlasConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - 配置错误要兜住，不能让整个 CLI 起不来
        log.warning("atlas.toml 校验失败，已回退默认配置：%s", exc)
        cfg = AtlasConfig()
    log.debug("配置来源：%s", path or "未找到 atlas.toml，使用默认配置")
    _CACHE["cfg"] = cfg
    return cfg


def require_api_key(settings: AgentSettings) -> None:
    """真实模型启动前的预检，把错误提前到入口，而不是等到调用时才炸。"""
    if settings.provider not in REQUIRED_ENV:
        return
    if get_api_key(settings.provider) is None:
        names = " 或 ".join(REQUIRED_ENV[settings.provider])
        primary = REQUIRED_ENV[settings.provider][0]
        raise RuntimeError(
            f"provider={settings.provider} 需要系统环境变量 {names}，但未检测到。\n"
            f"  Windows 持久化：setx {primary} \"你的密钥\"\n"
            f"  仅当前终端生效：$env:{primary} = \"你的密钥\"\n"
            f"  设置后请重开终端；不想配 Key 可以用 --provider fake 离线演示。\n"
            f"  运行状态自查：python check_env.py"
        )
