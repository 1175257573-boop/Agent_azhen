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

    `probe_backends=true`（默认）会真的探一次**正在生效的**记忆后端，
    而不是只回报配置里的后端名——否则 Redis 挂了这里照样返回 ok。

    可选拓展（Redis / PostgreSQL）的语义：
      · 生效的后端不可用 → `ok=false`（真故障）；
      · 拓展没启用（`active=false`）→ 只汇报状态，**不影响 `ok`**。
        「没装插件」不等于「系统坏了」。
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
            # 短超时（2s）且两边并发：探活接口会被桌面端启动轮询调用，
            # 本机 Redis/PG 没起时，5 秒的 libpq 超时会让这里拖到 7 秒以上，
            # 调用方先超时放弃 → 后端明明活着却被反复判为「连不上」。
            probed = memory_mod.probe(timeout=2)
        except Exception as exc:  # noqa: BLE001 —— 探针本身不能把健康检查拖垮
            payload["backends"] = {"error": f"{type(exc).__name__}: {exc}"}
        else:
            payload["backends"] = probed
            # 只看「正在生效」的后端：没启用的拓展不参与健康判定
            active = [v for v in probed.values()
                      if str(v.get("active", "True")).lower() == "true"]
            payload["ok"] = all(v.get("alive") == "True" for v in active)
            payload["extensions"] = {
                "inactive": [k for k, v in probed.items()
                             if str(v.get("active", "True")).lower() != "true"],
            }

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
