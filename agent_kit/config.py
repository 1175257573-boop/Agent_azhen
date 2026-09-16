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

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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

    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER") or detect_provider())
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
            self.model_name = os.getenv("LLM_MODEL") or DEFAULT_MODELS[self.provider]

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
