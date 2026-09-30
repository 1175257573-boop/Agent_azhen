"""进程级单例容器（≈ Spring 的 Bean 容器）。

拆成三进程之后，单体（server.app）、Agent 执行服务（server.agent_app）、
记忆服务（server.memory_app）都需要拿 AgentService / MemoryService，
但**绝不能** import server.app——那会顺手把单体应用也装配出来。

所以单例单独放这里，谁要谁拿。
"""

from __future__ import annotations

from server.service.agent_service import AgentService
from server.service.memory_service import MemoryService

_agent_service = AgentService()
_memory_service = MemoryService()


def get_agent_service() -> AgentService:
    return _agent_service


def get_memory_service() -> MemoryService:
    return _memory_service


def reset_services() -> None:
    """测试用：清掉缓存的装配物，避免上一个用例的 ATLAS_HOME 泄漏进来。"""
    _agent_service.reload()
