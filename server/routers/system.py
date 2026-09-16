"""系统 Controller：健康检查、环境概况、能力模式列表。"""

from __future__ import annotations

import os
import platform
import sys

import langchain
from fastapi import APIRouter

from agent_kit import memory as mem
from agent_kit.app import MODE_HELP
from agent_kit.config import DEFAULT_MODELS, detect_provider

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(probe_backends: bool = True):
    """给探针 / 前端首屏用。

    `probe_backends=true`（默认）会真的拨一次 Redis PING、连一次 PostgreSQL，
    而不是只回报配置里的后端名——否则 Redis 挂了这里照样返回 ok。
    """
    from agent_kit import memory as memory_mod

    short = mem.RESOLVED.get("short") or mem.short_backend()
    long = mem.RESOLVED.get("long") or mem.long_backend()

    payload = {
        "ok": True,
        "python": sys.version.split()[0],
        "langchain": langchain.__version__,
        "platform": platform.platform(),
        "memory": {"short_term": short, "long_term": long},
    }

    if probe_backends:
        try:
            probed = memory_mod.probe()
        except Exception as exc:  # noqa: BLE001 —— 探针本身不能把健康检查拖垮
            payload["backends"] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            payload["backends"] = probed
            alive = all(v.get("alive") == "True" for v in probed.values())
            payload["ok"] = alive       # 后端挂了就不再自称 ok

    return payload


@router.get("/info")
def info():
    provider = os.getenv("LLM_PROVIDER") or detect_provider()
    return {
        "provider": provider,
        "model": os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(provider, "-"),
        "providers": list(DEFAULT_MODELS),
        "modes": MODE_HELP,
        "has_key": bool(os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
                        or os.getenv("DEEPSEEK_API_KEY") or os.getenv("ANTHROPIC_API_KEY")),
        "report": mem.report(),
    }


@router.get("/modes")
def modes():
    return MODE_HELP
