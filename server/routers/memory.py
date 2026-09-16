"""记忆层 Controller：查看后端状态、读写长期偏好。"""

from __future__ import annotations

from fastapi import APIRouter, Body

from server.schemas import PreferenceIn

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _svc():
    from server.app import get_memory_service

    return get_memory_service()


@router.get("/status")
def status():
    """短期/长期记忆后端、消息窗口、实际解析结果。"""
    return _svc().status()


@router.get("/preferences")
def preferences(user_id: str = "demo"):
    return _svc().preferences(user_id)


@router.post("/preferences")
def put_preference(body: PreferenceIn):
    return _svc().put_preference(body.user_id, body.key, body.value)


@router.delete("/preferences")
def delete_preference(user_id: str = "demo", key: str = Body(..., embed=True)):
    return {"ok": _svc().delete_preference(user_id, key)}
