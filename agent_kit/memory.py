"""记忆层：短期记忆（消息窗口 / Redis）与长期记忆（事实 / PostgreSQL）。

这两层解决的问题完全不同，别混为一谈：

  短期记忆 checkpointer —— 按 thread_id 保存**对话状态**，回答「这轮聊到哪了」。
      本项目用 **Redis** 存，并叠加**消息窗口**（只把最近 N 条喂给模型）：
        · Redis 负责「可恢复 + 可过期」（TTL），进程重启不丢；
        · 消息窗口负责「上下文不膨胀」，避免长对话把 token 吃光。
      无 Redis 时自动降级为 InMemorySaver（进程内，重启即失）。

  长期记忆 store —— 按 namespace 保存**跨会话事实**，回答「这个用户是谁」。
      本项目用 **PostgreSQL** 存（需要落盘、可 SQL 查询、能跨服务共享）。
      无 PG 时自动降级为 InMemoryStore。

环境变量（全部可选）：
    SHORT_TERM_BACKEND   memory | sqlite | redis       默认 sqlite（配了 REDIS_URL 则 redis）
    LONG_TERM_BACKEND    memory | sqlite | postgres    默认 sqlite（配了 PG_DSN 则 postgres）
    ATLAS_HOME          运行态家目录，默认 ~/.atlas；SQLite 库落在这里
    SQLITE_PATH         直接指定 SQLite 库文件路径（优先级高于 ATLAS_HOME）
    MEMORY_STRICT       1 = 后端连不上直接报错；0（默认）= 告警并降级内存
    REDIS_URL            redis://localhost:6379/0
    REDIS_TTL_MINUTES    checkpoint 过期分钟数，默认 10080（7 天）
    MEMORY_WINDOW        短期记忆消息窗口大小，默认 20 条
    PG_DSN               postgresql://user:pwd@host:5432/dbname
                         （也认 POSTGRES_URL / DATABASE_URL）
    MEMORY_STRICT        1 = 后端连不上直接报错；0（默认）= 告警并降级内存
"""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from agent_kit.config import load_atlas_config
from agent_kit.home import resolve_db_path
from agent_kit.logging_conf import get_logger

log = get_logger("agent.memory")

# 本次进程实际解析出的后端，供 `main.py info` / `ui.py /info` 展示
RESOLVED: dict[str, str] = {"short": "?", "long": "?"}

# 需要在进程退出时释放的资源（Postgres 连接池等）
_CLEANUPS: list[Any] = []


# ---------------------------------------------------------------------------
# 环境变量辅助
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r 不是整数，回落到 %d", name, raw, default)
        return default


def _dsn(*names: str) -> str | None:
    """按顺序找第一个非空的环境变量。"""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def _strict() -> bool:
    return os.getenv("MEMORY_STRICT", "0") in {"1", "true", "True", "yes"}


def _degrade(target: str, backend: str, exc: BaseException) -> bool:
    """后端不可用时怎么办。返回 True 表示调用方应改用内存实现。"""
    msg = f"{target} 后端 {backend} 不可用（{type(exc).__name__}: {exc}）"
    if _strict():
        raise RuntimeError(f"{msg}；MEMORY_STRICT=1 要求严格失败。") from exc
    log.warning("%s，已降级为内存模式（进程重启即丢失）", msg)
    return True


def default_dir() -> Path:
    """SQLite 落盘目录（runs/ 已被 .gitignore 忽略）。"""
    return Path(__file__).resolve().parent.parent / "runs"


def short_backend() -> str:
    """短期记忆后端：环境变量 > atlas.toml > **默认 sqlite**。

    默认改成 sqlite 的原因：Redis/PostgreSQL 都要另起服务，本机跑一次 demo 就得装；
    而 SQLite 标准库自带、零配置、进程重启不丢。要换回 Redis，设 SHORT_TERM_BACKEND=redis
    或在 atlas.toml 里写 `[memory] short_term = "redis"`。
    """
    env = (os.getenv("SHORT_TERM_BACKEND") or "").lower()
    if env:
        return env
    toml_value = (load_atlas_config().memory.short_term or "").lower()
    if toml_value:
        return toml_value
    return "redis" if _dsn("REDIS_URL") else "sqlite"


def long_backend() -> str:
    """长期记忆后端：环境变量 > atlas.toml > **默认 sqlite**（理由同上）。"""
    env = (os.getenv("LONG_TERM_BACKEND") or "").lower()
    if env:
        return env
    toml_value = (load_atlas_config().memory.long_term or "").lower()
    if toml_value:
        return toml_value
    has_pg = bool(_dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL"))
    return "postgres" if has_pg else "sqlite"


def window_size() -> int:
    """短期记忆消息窗口大小（最近 N 条进入上下文）。"""
    return _env_int("MEMORY_WINDOW", 20)


# ---------------------------------------------------------------------------
# 短期记忆
# ---------------------------------------------------------------------------
def build_checkpointer(mode: str | None = None) -> Any:
    """短期记忆。redis 为主，sqlite 次之，兜底内存。"""
    mode = (mode or short_backend()).lower()

    if mode == "redis":
        url = _dsn("REDIS_URL")
        if not url:
            log.warning("SHORT_TERM_BACKEND=redis 但未设置 REDIS_URL，降级为内存模式")
        else:
            try:
                from langgraph.checkpoint.redis import RedisSaver

                _ping_redis(url)
                ttl_minutes = _env_int("REDIS_TTL_MINUTES", 60 * 24 * 7)

                if _redis_has_json(url):
                    saver = RedisSaver(
                        url,
                        ttl={"default_ttl": ttl_minutes, "refresh_on_read": True},
                        # redis-py 6+ 默认走 RESP3（先发 HELLO），Redis 5.x 不认识，
                        # 显式指定 RESP2 向下兼容（6/7 同样支持 RESP2）。
                        connection_args={"protocol": _env_int("REDIS_PROTOCOL", 2)},
                    )
                    try:
                        saver.setup()   # 建 RediSearch 索引；没有该模块只影响检索
                    except Exception as idx_exc:  # noqa: BLE001
                        log.warning("Redis 索引创建失败（%s），基础读写仍可用", idx_exc)
                    RESOLVED["short"] = f"redis[官方](ttl={ttl_minutes}min)"
                else:
                    from agent_kit.redis_checkpointer import PlainRedisSaver

                    saver = PlainRedisSaver(
                        url,
                        ttl_seconds=ttl_minutes * 60,
                        protocol=_env_int("REDIS_PROTOCOL", 2),
                    )
                    RESOLVED["short"] = f"redis[免模块](ttl={ttl_minutes}min)"
                    log.info("当前 Redis 无 RedisJSON，已启用免模块实现 redis_checkpointer.py")

                log.info("短期记忆 → Redis %s，TTL=%d 分钟", _hide_pwd(url), ttl_minutes)
                return saver
            except Exception as exc:  # noqa: BLE001
                _degrade("短期记忆", "redis", exc)

    if mode == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            db_path = resolve_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # from_conn_string 返回的是上下文管理器（和 PostgresSaver 一样），
            # 直接把它当 saver 传给 create_agent 会报类型错误，必须先 __enter__
            cm = SqliteSaver.from_conn_string(str(db_path))
            saver = cm.__enter__()
            atexit.register(_close_quietly, cm)
            RESOLVED["short"] = f"sqlite[{db_path.name}]"
            log.info("短期记忆 → SQLite %s", db_path)
            return saver
        except Exception as exc:  # noqa: BLE001
            _degrade("短期记忆", "sqlite", exc)

    RESOLVED["short"] = "memory"
    return InMemorySaver()


async def build_async_checkpointer(mode: str | None = None) -> Any:
    """异步链路专用的短期记忆。**不要拿 build_checkpointer() 顶替**。

    为什么需要单独一个（2026-09 实测踩出来的坑）：
      同步 SqliteSaver 没实现异步方法，一旦图走 `astream` / `ainvoke`，
      langgraph 会去调 saver 的 async 接口，直接抛
      `NotImplementedError: The SqliteSaver does not support async methods`。
      ——MCP 场景（`enable_mcp=True` / `mode=mcp`）必然走异步，
      所以那条链路必须用 AsyncSqliteSaver（底层依赖 aiosqlite）。

    `redis` 同理走 its own async saver；都不可用则退回 InMemorySaver（支持异步）。
    """
    from agent_kit.home import resolve_db_path

    mode = (mode or short_backend()).lower()

    if mode == "redis":
        url = _dsn("REDIS_URL")
        if not url:
            log.warning("SHORT_TERM_BACKEND=redis 但未设置 REDIS_URL，异步链路降级")
        else:
            try:
                from langgraph.checkpoint.redis.aio import AsyncRedisSaver

                saver = AsyncRedisSaver(
                    url,
                    connection_args={"protocol": _env_int("REDIS_PROTOCOL", 2)},
                )
                RESOLVED["short"] = "redis[async]"
                log.info("短期记忆（异步）→ Redis")
                return saver
            except Exception as exc:  # noqa: BLE001
                _degrade("短期记忆（异步）", "redis", exc)

    if mode == "sqlite":
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            db_path = resolve_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # 与同步版一样：from_conn_string 给的是上下文管理器，得先 __aenter__
            cm = AsyncSqliteSaver.from_conn_string(str(db_path))
            saver = await cm.__aenter__()
            atexit.register(_close_async_quietly, cm)
            RESOLVED["short"] = f"sqlite[async:{db_path.name}]"
            log.info("短期记忆（异步）→ SQLite %s", db_path)
            return saver
        except Exception as exc:  # noqa: BLE001
            _degrade("短期记忆（异步）", "sqlite", exc)

    RESOLVED["short"] = "memory"
    return InMemorySaver()


def _close_async_quietly(cm: Any) -> None:
    """关异步上下文管理器。进程退出时事件循环通常已经没了，
    所以这里新开一个 loop 跑完就关，而不是依赖当时那个 loop。"""
    async def _close() -> None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            log.debug("异步记忆连接关闭时出错（可忽略）：%s", exc)

    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_close())
        finally:
            loop.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("异步记忆连接未能优雅关闭（可忽略）：%s", exc)


def _redis_client(url: str, **extra: Any) -> Any:
    import redis

    # 用 dict 合并而不是直接写死参数：调用方（如健康检查探针）要能覆盖超时，
    # 写成 `from_url(url, socket_connect_timeout=3, **extra)` 会因为
    # extra 里再带同名参数直接抛 "got multiple values for keyword argument"。
    opts: dict[str, Any] = {
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
        "protocol": _env_int("REDIS_PROTOCOL", 2),   # 兼容 Redis 5.x
    }
    opts.update(extra)
    return redis.Redis.from_url(url, **opts)


def _ping_redis(url: str, timeout: float | None = None) -> None:
    """拨一次 PING，让「连不上」在启动时就暴露，而不是等到写 checkpoint 时才炸。"""
    client = _redis_client(url, **({"socket_connect_timeout": timeout} if timeout else {}))
    try:
        client.ping()
    finally:
        client.close()


def probe(*, timeout: float | None = None) -> dict[str, dict[str, str]]:
    """探测记忆后端的连通性，**并区分「实际生效」与「只是装了扩展」**。

    返回形如：
        {"short_term": {"backend": "sqlite", "active": True, "alive": "True", ...}, ...}

    语义（Redis / PostgreSQL 是**可选的系统拓展**，不是核心必需组件）：
      · `active=True`  —— 这个后端**正在被使用**，它挂了就是真故障；
      · `active=False` —— 只是环境变量里留了地址、或装了扩展但没启用，
                          属于「拓展未启用」，**既不探测也不影响健康判定**。

    为什么要这么分：早先的实现只看「环境变量里有没有 REDIS_URL / PG_DSN」就去连，
    于是本机没起 Redis/PG 的人（实际用的是 SQLite）也会被探测拖住 ——
    一次 `/api/health` 要 7 秒以上。更糟的是它还会让整体 `ok` 变成 false，
    相当于「没装插件」被算成了「系统坏了」。

    `timeout`（秒）：给单侧探测设上限。健康检查要传短值，
    否则本机没起服务时 libpq 默认 5 秒超时会拖垮整个探活。
    """
    result: dict[str, dict[str, str]] = {}

    def _probe_redis() -> dict[str, str]:
        short = short_backend()
        url = os.getenv("REDIS_URL")
        info: dict[str, str] = {"backend": short}
        if short != "redis":
            # 可选拓展没启用 —— 别去连，连了只是白等几秒
            hint = "检测到 REDIS_URL，启用请设 SHORT_TERM_BACKEND=redis" if url else "未启用"
            return {**info, "active": "False", "alive": "N/A", "reason": f"可选拓展未启用（{hint}）"}
        if not url:
            return {**info, "active": "True", "alive": "False", "reason": "已启用但未配置 REDIS_URL"}
        try:
            _ping_redis(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {**info, "active": "True", "alive": "False", "reason": f"{type(exc).__name__}: {exc}"}
        return {**info, "active": "True", "alive": "True", "reason": ""}

    def _probe_pg() -> dict[str, str]:
        long = long_backend()
        dsn = _dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL")
        info: dict[str, str] = {"backend": long}
        if long != "postgres":
            hint = "检测到 PG_DSN，启用请设 LONG_TERM_BACKEND=postgres" if dsn else "未启用"
            return {**info, "active": "False", "alive": "N/A", "reason": f"可选拓展未启用（{hint}）"}
        if not dsn:
            return {**info, "active": "True", "alive": "False", "reason": "已启用但未配置 PG_DSN"}
        try:
            import psycopg

            conn = psycopg.connect(_pg_dsn_with_timeout(dsn, timeout=timeout))
            conn.close()
        except Exception as exc:  # noqa: BLE001
            return {**info, "active": "True", "alive": "False", "reason": f"{type(exc).__name__}: {exc}"}
        return {**info, "active": "True", "alive": "True", "reason": ""}

    # 两边并发：串行是「Redis 超时 + PG 超时」相加，并发只取较慢的那个
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_short = pool.submit(_probe_redis)
        f_long = pool.submit(_probe_pg)
        result["short_term"] = f_short.result()
        result["long_term"] = f_long.result()

    return result


def _redis_has_json(url: str) -> bool:
    """探测 Redis 是否带 RedisJSON 模块。

    官方 langgraph RedisSaver 依赖 JSON.SET / JSON.GET；
    Windows 上的 Redis 构建基本都不带这个模块，此时要走免模块实现。
    """
    client = _redis_client(url)
    try:
        names = {m.get("name", "").lower() for m in client.module_list()}
        return any("json" in n for n in names)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------
def build_store(mode: str | None = None) -> BaseStore:
    """长期记忆。postgres 为主，sqlite 次之，兜底内存。"""
    mode = (mode or long_backend()).lower()

    if mode == "postgres":
        dsn = _dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL")
        if not dsn:
            log.warning("LONG_TERM_BACKEND=postgres 但未设置 PG_DSN，降级为内存模式")
        else:
            try:
                from langgraph.store.postgres import PostgresStore

                # from_conn_string 返回的是上下文管理器；REPL 是长生命周期进程，
                # 这里手动 __enter__ 并在退出时关闭连接池。
                cm = PostgresStore.from_conn_string(_pg_dsn_with_timeout(dsn))
                store = cm.__enter__()
                atexit.register(_close_quietly, cm)
                store.setup()  # 建表（幂等）
                RESOLVED["long"] = "postgres"
                log.info("长期记忆 → PostgreSQL %s", _hide_pwd(dsn))
                return store
            except Exception as exc:  # noqa: BLE001
                _degrade("长期记忆", "postgres", exc)

    if mode == "sqlite":
        try:
            # 官方没有 SQLite 版 store（详见 sqlite_store.py 的模块说明），用自建实现
            from agent_kit.sqlite_store import SqliteStore

            db_path = resolve_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            store = SqliteStore(db_path)
            atexit.register(store.close)
            RESOLVED["long"] = f"sqlite[{db_path.name}]"
            log.info("长期记忆 → SQLite %s", db_path)
            return store
        except Exception as exc:  # noqa: BLE001
            _degrade("长期记忆", "sqlite", exc)

    RESOLVED["long"] = "memory"
    return InMemoryStore()


def _close_quietly(cm: Any) -> None:
    """关连接池 / 上下文管理器，失败也别把进程退出流程崩掉。"""
    try:
        cm.__exit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        # 静默 ≠ 无声：至少落一条 debug，真出问题才有线索
        log.debug("关闭资源时出错（已忽略）：%s: %s", type(exc).__name__, exc)


def _pg_dsn_with_timeout(dsn: str, timeout: int | None = None) -> str:
    """给 PG 连接串补 connect_timeout。

    libpq 默认没有连接超时，机器/端口不通时会一直挂着，
    让「数据库没起来」表现为卡死而不是报错。这里强制补一个短超时。

    显式传入 `timeout` 会覆盖 dsn 里已有的 connect_timeout —— 健康检查要用
    自己的短上限，不能让 dsn 里的 5 秒拖垮整个探活。
    """
    seconds = timeout if timeout is not None else _env_int("PG_CONNECT_TIMEOUT", 5)
    # 先剥掉原有的，再按本次要求补
    base = re.sub(r"[?&]connect_timeout=\d+", "", dsn)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}connect_timeout={max(1, int(seconds))}"


def _hide_pwd(dsn: str) -> str:
    """打码连接串里的密码，避免进日志。"""
    if "@" not in dsn:
        return dsn
    head, _, tail = dsn.partition("://")
    if not tail:
        return dsn
    userinfo, _, host = tail.rpartition("@")
    if ":" in userinfo:
        user, _, _pwd = userinfo.partition(":")
        userinfo = f"{user}:***"
    return f"{head}://{userinfo}@{host}"


# ---------------------------------------------------------------------------
# 消息链修补：tool_calls 与 ToolMessage 必须成对
# ---------------------------------------------------------------------------
def sanitize_tool_call_pairs(msgs: list[Any]) -> list[Any] | None:
    """修补 tool_calls / ToolMessage 不配对的消息链，返回 None 表示无需修补。

    为什么要这道工序（2026-09 实测踩出来的线上级 bug）：
      OpenAI 兼容接口（DeepSeek / 通义 / OpenAI 都一样）要求带 tool_calls 的
      assistant 消息后面**必须**紧跟对应每条 tool_call_id 的 tool 消息，否则直接
      400：`An assistant message with 'tool_calls' must be followed by tool messages
      responding to each 'tool_call_id'`。

    两种坏结构：
      1. **悬空 tool_calls** —— AIMessage 带 tool_calls，后面却没有 ToolMessage。
         成因：工具执行阶段被中断（网络/鉴权失败、进程被关），checkpoint 只落了
         AI 半截，用户下一条消息又被追加进来。这次线上就是这么产生的。
      2. **孤儿 ToolMessage** —— 找不到发起它的 AIMessage。
         成因：消息窗口裁剪把前面的 AIMessage 丢掉了。

    处理办法：
      · 悬空的补一条「未返回结果」的 ToolMessage，保住 tool_call_id 的配对；
      · 孤儿 ToolMessage 直接丢掉（留着同样会 400）。

    幂等：修补过的结果再跑一遍返回 None，不会重复插入。
    """
    from langchain_core.messages import AIMessage, ToolMessage

    MISSING_HINT = "[工具调用未返回结果：上一轮在工具执行阶段中断，结果没有写入记忆]"

    pending: list[tuple[str, str]] = []   # [(tool_call_id, tool_name)] 尚未被响应
    out: list[Any] = []
    changed = False

    def flush() -> None:
        """给还没响应的 tool_calls 补 ToolMessage。"""
        nonlocal changed
        for cid, name in pending:
            out.append(
                ToolMessage(
                    content=MISSING_HINT,
                    tool_call_id=cid,
                    name=name or None,
                    status="error",
                )
            )
            changed = True
        pending.clear()

    for m in msgs:
        # ---- ToolMessage：只对得上号才留 ----
        if isinstance(m, ToolMessage):
            cid = getattr(m, "tool_call_id", None)
            hit = next((p for p in pending if p[0] == cid), None)
            if hit is None:
                changed = True          # 孤儿，丢弃
                continue
            pending.remove(hit)
            out.append(m)
            continue

        # ---- 非 ToolMessage：先把上一轮没配对的补齐 ----
        if pending:
            flush()

        if isinstance(m, AIMessage):
            calls = getattr(m, "tool_calls", None) or []
            ids: list[tuple[str, str]] = []
            for c in calls:
                cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", None)
                name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                if cid:
                    ids.append((str(cid), str(name or "")))
            pending = ids

        out.append(m)

    if pending:
        flush()

    return out if changed else None


# ---------------------------------------------------------------------------
# 消息窗口：短期记忆的「上下文侧」策略
# ---------------------------------------------------------------------------
def make_message_window(window: int | None = None) -> Any:
    """只把最近 N 条消息喂给模型，其余仍留在 Redis 里可回溯。

    为什么不用「直接截断 state」：
      · AI 消息带 tool_calls 时，紧跟的 ToolMessage 必须一起留，否则 API 报错；
      · 系统消息要保留。
    所以这里用 langchain 的 trim_messages，而不是 list[-N:]。

    为什么这么挂载（2026-09 实测修正了两个坑）：
      1. 不能用 before_model：它返回的 {"messages": [...]} 要过 state 的
         add_messages reducer，语义是「按 id 合并后追加」——**没有删除能力**，
         被裁掉的消息照样留在 state 里，窗口形同虚设（日志在报「22 → 14」，
         可 state 里一直是 28 条）；
      2. 不能直接 @wrap_model_call：装饰器只生成同步实现，走 astream（MCP 场景）
         会抛 `awrap_model_call is not available`。所以统一用 messages_transform
         包一层，同步与异步链路都用得上。
    """
    from langchain_core.messages import trim_messages

    size = window or window_size()

    def _trim(msgs: list[Any]) -> list[Any] | None:
        if len(msgs) <= size:
            return None
        trimmed = trim_messages(
            msgs,
            token_counter=len,          # 按「条数」而不是 token 数计
            max_tokens=size,
            strategy="last",
            start_on="human",           # 从一条人类消息开始，不切断对话语义
            end_on=("human", "tool"),   # 结尾落在完整轮次边界
            include_system=True,        # 系统消息永远保留
            allow_partial=False,
        )
        dropped = len(msgs) - len(trimmed)
        if dropped > 0:
            log.info(
                "消息窗口：%d 条 → %d 条（本次请求丢弃最早的 %d 条，仍在记忆库中可回溯）",
                len(msgs), len(trimmed), dropped,
            )
        # trim_messages 只在「整轮」层面保证边界，切在 tool_calls 与 ToolMessage
        # 之间时它并不总能把对一起保留，所以裁完再统一修补一次（幂等）。
        fixed = sanitize_tool_call_pairs(trimmed)
        if fixed is not None:
            log.info("消息链修补：裁剪后 %d 条 → %d 条（补齐缺失的 tool 响应 / 丢弃孤儿工具消息）", len(trimmed), len(fixed))
        return fixed or trimmed

    # 同样要用 messages_transform：包出来的中间件同步 + 异步都可用
    from agent_kit.middleware import messages_transform

    return messages_transform(_trim, name="message_window")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def seed_long_term_memory(store: BaseStore, user_id: str = "demo", overwrite: bool = False) -> None:
    """预置几条用户偏好，用来演示「跨会话」效果。真实场景里由工具写入。

    接了 PostgreSQL 后每次启动都覆盖写一遍没有意义（还会刷新 updated_at），
    所以默认 overwrite=False：该用户已有偏好就直接跳过。
    """
    if not overwrite and store.search(("preferences", user_id)):
        return

    now = datetime.now(timezone.utc).astimezone().isoformat()
    for key, value in {
        "language": "简体中文",
        "tone": "结构化表达，善用表格对比",
        "domain": "区块链安全 / 联邦图神经网络",
    }.items():
        store.put(("preferences", user_id), key, {"value": value, "updated_at": now})


def thread_config(thread_id: str = "demo-thread") -> dict[str, Any]:
    """thread_id 是短期记忆的唯一主键。

    同一个 thread_id = 同一段对话；换一个 thread_id = 全新会话。
    """
    return {"configurable": {"thread_id": thread_id}}


def new_thread_id(prefix: str = "thread") -> str:
    """生成一个新的会话 ID。"""
    return os.getenv("THREAD_ID") or f"{prefix}-{uuid.uuid4().hex[:8]}"


def pad_display(text: str, width: int = 14) -> str:
    """按「显示宽度」补空格（中文占 2 列，直接 ljust 会歪）。"""
    import unicodedata

    used = sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)
    return text + " " * max(0, width - used)


def report() -> dict[str, Any]:
    """给 `info` / `/mem` 用的记忆层概况。"""
    short = RESOLVED.get("short", "?")
    long_ = RESOLVED.get("long", "?")
    return {
        "短期记忆后端": short if short != "?" else f"{short_backend()}（未初始化）",
        "长期记忆后端": long_ if long_ != "?" else f"{long_backend()}（未初始化）",
        "消息窗口": f"{window_size()} 条",
        "REDIS_URL": _hide_pwd(_dsn("REDIS_URL") or "未配置"),
        "PG_DSN": _hide_pwd(_dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL") or "未配置"),
        "严格模式": "开" if _strict() else "关（连不上则降级内存）",
    }


def report_lines() -> list[str]:
    """已经按显示宽度对齐好的行，直接 print 即可。"""
    return [f"    {pad_display(k)}{v}" for k, v in report().items()]
