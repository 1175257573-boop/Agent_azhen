"""凭据 Controller —— 在 Web 界面里配置 / 查看 / 清除模型 API Key。

这是全项目**唯一**能写入密钥的入口，所以它的安全约束比其他路由严一档：

    1. **仅限本机来源**。这个服务没有登录、没有鉴权，只要能被外部访问，
       这个接口就等于"任何人都能读走你的 Key"。所以这里直接按来源地址拦。
    2. **默认只返回掩码**。完整密钥只有一个出口：`/reveal`，
       而且它要求调用方显式指名 provider（前端必须二次确认才发这个请求）。
    3. **明文不进 URL**。`/reveal` 用 POST + 请求体，不用 GET + 路径参数——
       否则密钥会同时进入浏览器历史、访问日志和任何中间代理的记录。
    4. **响应禁止缓存**。返回明文的响应带 `Cache-Control: no-store`，
       避免磁盘缓存里留下一份。

Controller 仍然只做协议转换，真正的存取逻辑在 `agent_kit/credentials.py`。
"""

from __future__ import annotations

import json
import os
import time

from fastapi import APIRouter, HTTPException, Request, Response

from agent_kit import credentials as creds
from agent_kit.logging_conf import get_logger
from agent_kit.redact import mask, redact
from server.schemas import (
    CredentialIn,
    CredentialState,
    RevealIn,
    RevealOut,
    VerifyIn,
    VerifyOut,
)

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

log = get_logger("web.credentials")

# 允许访问凭据接口的来源。这是"服务没鉴权时的最后一道门"，
# 所以只认回环地址——不做任何网段放宽。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _local_only(request: Request) -> None:
    """非本机来源一律拒绝。

    注意这挡不住"本机上另一个用户/进程"——那是操作系统权限层的事，
    不是 Web 层能解决的。界面上的保密说明会把边界讲清楚，不夸大这里的保护力。
    """
    host = (request.client.host if request.client else "") or ""
    if host not in _LOOPBACK:
        log.warning("拒绝非本机来源的凭据访问：%s %s", host or "(未知)", request.url.path)
        raise HTTPException(status_code=403, detail="密钥接口仅允许本机访问（127.0.0.1 / ::1）")


def _active_provider() -> str:
    """当前实际会用的 provider，口径与 `config.AgentSettings` 完全一致。"""
    from agent_kit.config import AgentSettings

    try:
        return AgentSettings(provider=None).provider
    except Exception:  # noqa: BLE001 —— 展示用途，算不出来不该让接口挂掉
        return creds.store.active_provider()


def _snapshot() -> CredentialState:
    return CredentialState(
        items=[item.to_dict() for item in creds.store.list()],
        active_provider=_active_provider(),
        storage_path=str(creds.store.path),
        file_exists=creds.store.file_exists(),
        storage_note=creds.store.storage_note(),
        notes=creds.store.notes(),
    )


async def _invalidate_agents() -> None:
    """密钥变了 → 已装配的 Agent 必须作废。

    不做这一步的话，缓存里那些用旧 Key 装配好的图会继续被复用，
    用户会看到"我明明改了 Key，怎么还在报鉴权失败"。
    MCP 那份要额外关掉——每条连接都挂着一个 stdio 子进程，只清缓存会漏进程。
    """
    from server.app import get_agent_service

    svc = get_agent_service()
    try:
        await svc.aclose_mcp()
    except Exception as exc:  # noqa: BLE001
        log.warning("关闭 MCP 连接失败（不影响凭据生效）：%s", exc)
    svc.reload()


@router.get("", response_model=CredentialState)
def state(request: Request) -> CredentialState:
    """当前各 provider 的凭据状态。只含掩码，可安全地反复轮询。"""
    _local_only(request)
    return _snapshot()


@router.post("", response_model=CredentialState)
async def save(request: Request, body: CredentialIn) -> CredentialState:
    """保存并立即生效。

    `remember=false`（默认）时密钥只进进程内存，服务一停就没了；
    勾选后才会落盘到 ATLAS_HOME/credentials.json。
    """
    _local_only(request)
    # ValueError 交给全局异常处理器映射成 400，这里不重复造错误格式
    creds.store.set(body.provider, body.api_key, remember=body.remember)
    await _invalidate_agents()
    return _snapshot()


@router.delete("/{provider}", response_model=CredentialState)
async def remove(request: Request, provider: str, forget: bool = True) -> CredentialState:
    """清除某个 provider 的密钥。

    `forget=true`（默认）连本机凭据文件里的记录一起删；
    如果这个 provider 原本在系统环境变量里配过，原值会被恢复回去。
    """
    _local_only(request)
    if not creds.store.clear(provider, forget=forget):
        raise HTTPException(status_code=404, detail=f"未知 provider：{provider}")
    await _invalidate_agents()
    return _snapshot()


@router.post("/reveal", response_model=RevealOut)
def reveal(request: Request, response: Response, body: RevealIn) -> RevealOut:
    """取回**完整**密钥，供用户在界面上核对。

    这个接口是刻意保留的：用户填进去的东西，得能自己看回来，否则只能靠删掉重填。
    但它是整个系统里唯一会回传明文的地方，所以：
      * 只有本机来源能调（`_local_only`）；
      * 响应禁止缓存，不给磁盘留副本；
      * 服务端记一条审计日志（含掩码不含明文）。
    前端必须"点击 → 二次确认 → 限时展示、到点自动隐藏"，不得写入任何浏览器存储。
    """
    _local_only(request)
    value = creds.store.reveal(body.provider)
    if not value:
        raise HTTPException(status_code=404, detail=f"{body.provider} 尚未配置密钥")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return RevealOut(provider=body.provider.strip().lower(), api_key=value, masked=mask(value))


@router.post("/verify", response_model=VerifyOut)
def verify(request: Request, body: VerifyIn) -> VerifyOut:
    """真发一次最小请求，确认密钥可用。

    为什么值得单独做个接口：密钥"存进去了"和"能用"是两回事——
    配额耗尽、权限没开、复制时少了一位、区域站点选错，都只在真正调用时才暴露。
    与其让用户在对话框里试错，不如在这里一次问清楚。

    注意这是**同步阻塞**的网络调用：FastAPI 会把 def 路由丢进线程池，
    不会卡住事件循环。（写成 async def 反而不行——同步 SDK 会把整个服务卡住。）
    """
    _local_only(request)
    provider = (body.provider or "").strip().lower()

    if provider == "fake":
        return VerifyOut(ok=True, provider=provider, model="scripted", message="fake 是离线脚本模型，不需要密钥")

    if provider not in creds.MANAGED_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"未知 provider：{provider}，可选：{'、'.join(creds.MANAGED_PROVIDERS)} 或 fake",
        )

    if not os.environ.get(creds.ENV_NAME[provider]):
        raise HTTPException(status_code=400, detail=f"{provider} 尚未配置密钥，请先保存再测试")

    from langchain_core.messages import HumanMessage

    from agent_kit.config import AgentSettings, build_chat_model

    settings = AgentSettings(provider=provider, model_name=body.model_name or "")
    settings.max_tokens = 8          # 只为验证连通性，不浪费额度
    model_name = settings.model_name

    try:
        model = build_chat_model(settings)
    except Exception as exc:  # noqa: BLE001
        return VerifyOut(ok=False, provider=provider, model=model_name,
                         message=redact(f"{type(exc).__name__}: {exc}")[:400])

    started = time.perf_counter()
    try:
        reply = model.invoke([HumanMessage(content="ping")])
    except Exception as exc:  # noqa: BLE001 —— 探测失败是正常结果，不是服务异常
        cost = int((time.perf_counter() - started) * 1000)
        return VerifyOut(
            ok=False, provider=provider, model=model_name, latency_ms=cost,
            message=redact(f"{type(exc).__name__}: {exc}")[:400],
        )

    cost = int((time.perf_counter() - started) * 1000)
    content = getattr(reply, "content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, default=str)

    return VerifyOut(
        ok=True, provider=provider, model=model_name, latency_ms=cost,
        message="链路正常", sample=redact(content)[:80],
    )
