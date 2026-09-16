"""模型策略：静态模型 vs 动态模型。

静态模型  —— create_agent(model="xxx")，全程一个模型。
动态模型  —— @wrap_model_call 里 request.override(model=...) 按条件换：
              对话变长时换「更强但更贵」的模型（动态模型的典型用法）
              state 里打了 deep_think 标记时手动升级
              工具调用失败过多时降级到便宜模型（兜底）

关键点：override 是不可变的，返回一个**新的** request，再交给 handler。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import ModelRequest, ModelResponse

from agent_kit.logging_conf import get_logger
from agent_kit.tool_hooks import dual_model


def _label(model: Any) -> str:
    return getattr(model, "model_name", None) or str(model)


def make_dynamic_model(
    light_model: Any,
    heavy_model: Any,
    *,
    message_threshold: int = 8,
    state_flag: str = "deep_think",
    name: str = "dynamic_model",
):
    """构造「按复杂度自动换模型」的中间件。

    Args:
        light_model: 便宜快模型（qwen-turbo / gpt-4o-mini 一类）
        heavy_model: 贵而强的模型（qwen-max / gpt-4o 一类）
        message_threshold: 历史消息数超过该值就升级
        state_flag: 自定义 state 里的布尔字段名，为 True 时也升级（优先级最高）
    """
    log = get_logger("agent.model")

    def _pick(request: ModelRequest) -> Any:
        # ① 显式标记优先：前端/用户在 state 里打了标就用强模型
        state = request.state or {}
        if isinstance(state, dict) and state.get(state_flag):
            return heavy_model
        # ② 上下文变长：需要更强的推理与记忆整合能力
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        return heavy_model if len(msgs) > message_threshold else light_model

    def sync_dynamic_model(request: ModelRequest, handler) -> ModelResponse:
        model = _pick(request)
        log.debug("选用模型 %s", _label(model))
        return handler(request.override(model=model))

    async def async_dynamic_model(request: ModelRequest, handler) -> ModelResponse:
        model = _pick(request)
        log.debug("选用模型 %s", _label(model))
        return await handler(request.override(model=model))

    return dual_model(sync_dynamic_model, async_dynamic_model, name=name)


def make_fallback_model(primary: Any, fallback: Any, *, max_failures: int = 2, name: str = "fallback_model"):
    """失败降级：同一轮里模型连续抛错达阈值，后续调用换到备用模型。

    生产里模型供应商抖动很常见，这层能显著提升可用性。
    """
    counter = {"failures": 0}

    def sync_fallback_model(request: ModelRequest, handler) -> ModelResponse:
        model = fallback if counter["failures"] >= max_failures else primary
        try:
            response = handler(request.override(model=model))
        except Exception:
            counter["failures"] += 1
            raise
        counter["failures"] = 0
        return response

    async def async_fallback_model(request: ModelRequest, handler) -> ModelResponse:
        model = fallback if counter["failures"] >= max_failures else primary
        try:
            response = await handler(request.override(model=model))
        except Exception:
            counter["failures"] += 1
            raise
        counter["failures"] = 0
        return response

    return dual_model(sync_fallback_model, async_fallback_model, name=name)
