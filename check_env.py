"""环境变量自查工具：确认系统环境变量是否已生效。

用法（**务必在你自己新开的 PowerShell / 终端里跑**，不要在 IDE 内置终端跑旧会话）：
    python check_env.py

它会做三件事：
  1. 列出所有与本工程相关的环境变量，命中则打码显示
  2. 判断当前会默认启用哪个 provider
  3. 若配置了真实 Key，可选做一次最小连通性探测（--ping）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from agent_kit.config import (
        DEFAULT_MODELS,
        OPTIONAL_BASE_URL_ENV,
        REQUIRED_ENV,
        detect_provider,
        get_api_key,
        mask,
    )
except ImportError as exc:  # 依赖没装时给出明确指引
    print(f"[错误] 无法导入 agent_kit.config：{exc}")
    print("       请先 conda activate langchain && pip install -r requirements.txt")
    raise SystemExit(2)

OK = "✅"
MISS = "❌"
WARN = "⚠️"


def _line(name: str, value: str | None, note: str = "") -> str:
    if value is None:
        return f"  {MISS} {name:<22} 未设置"
    return f"  {OK} {name:<22} {mask(value):<16} {note}"


def main() -> int:
    print("=" * 72)
    print("  环境变量自查 · LangChain 1.4 Agent Demo")
    print("=" * 72)

    # ---- 1. provider 与通用设置 ----
    provider_env = os.getenv("LLM_PROVIDER")
    print("\n[通用]")
    print(_line("LLM_PROVIDER", provider_env, note="不设置则自动探测" if not provider_env else "显式指定"))
    print(_line("LLM_MODEL", os.getenv("LLM_MODEL"), note="不设置则用 provider 默认型号" if not os.getenv("LLM_MODEL") else ""))

    # ---- 2. 各 provider 的密钥 ----
    print("\n[密钥]")
    found: list[str] = []
    for provider, names in REQUIRED_ENV.items():
        key = get_api_key(provider)
        if key:
            found.append(provider)
        for i, name in enumerate(names):
            tag = "主变量" if i == 0 else "候选名"
            hint = "" if os.getenv(name) else f"（{tag}，可选）"
            print(_line(name, os.getenv(name), hint))

    # ---- 3. endpoint 覆盖 ----
    print("\n[Endpoint 覆盖（可选）]")
    any_base = False
    for provider, var in OPTIONAL_BASE_URL_ENV.items():
        value = os.getenv(var)
        if value:
            any_base = True
            print(_line(var, value, note=f"→ provider={provider}"))
    if not any_base:
        print(f"  {WARN} 均未设置，各 provider 走官方默认 endpoint")

    # ---- 3.5 记忆后端 ----
    from agent_kit import memory as mem

    print("\n[记忆层]")
    print(f"  短期记忆后端 : {mem.short_backend()}    长期记忆后端 : {mem.long_backend()}")
    print(f"  消息窗口     : {mem.window_size()} 条")
    for var in ("REDIS_URL", "PG_DSN", "POSTGRES_URL", "DATABASE_URL"):
        value = os.getenv(var)
        if value:
            print(_line(var, value))
    if not os.getenv("REDIS_URL"):
        print(f"  {WARN} 未设置 REDIS_URL，短期记忆将落在内存（进程重启即丢失）")
    if not mem._dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL"):
        print(f"  {WARN} 未设置 PG_DSN，长期记忆将落在内存（进程重启即丢失）")

    # ---- 4. 结论 ----
    print("\n[结论]")
    detected = detect_provider()
    final_provider = provider_env or detected
    print(f"  实际生效 provider : {final_provider}")
    print(f"  实际生效 model    : {os.getenv('LLM_MODEL') or DEFAULT_MODELS.get(final_provider, '-')}")
    if final_provider == "fake":
        print(f"  {WARN} 没有检测到任何真实 API Key，将使用离线脚本模型运行演示。")
    else:
        var = REQUIRED_ENV[final_provider][0]
        print(f"  {OK} 已从环境变量 {var} 取到密钥，可直接 python run_demo.py")

    # ---- 4.5 记忆后端连通性（--ping） ----
    if "--ping" in sys.argv:
        print("\n[记忆后端连通性]")
        if os.getenv("REDIS_URL"):
            try:
                mem._ping_redis(os.environ["REDIS_URL"])
                print(f"  {OK} Redis 可达")
            except Exception as exc:  # noqa: BLE001
                print(f"  {MISS} Redis 不可达：{type(exc).__name__}: {exc}")
        else:
            print(f"  {WARN} 未配置 REDIS_URL，跳过 Redis 探测")
        if mem._dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL"):
            try:
                import psycopg

                conn = psycopg.connect(mem._pg_dsn_with_timeout(mem._dsn("PG_DSN", "POSTGRES_URL", "DATABASE_URL")))
                conn.close()
                print(f"  {OK} PostgreSQL 可达")
            except Exception as exc:  # noqa: BLE001
                print(f"  {MISS} PostgreSQL 不可达：{type(exc).__name__}: {exc}")
        else:
            print(f"  {WARN} 未配置 PG_DSN，跳过 PostgreSQL 探测")

    # ---- 5. 可选：真实连通性探测 ----
    if "--ping" in sys.argv:
        print("\n[连通性探测]")
        if final_provider == "fake":
            print(f"  {WARN} provider=fake，无需联网，跳过。")
            return 0
        from agent_kit.config import AgentSettings, build_chat_model

        try:
            model = build_chat_model(AgentSettings(provider=final_provider))
            resp = model.invoke("用一句话回答：1+1=?")
            content = resp.content if isinstance(resp.content, str) else str(resp.content)
            print(f"  {OK} 调用成功，回复：{content[:120]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {MISS} 调用失败：{type(exc).__name__}: {exc}")
            print("       常见原因：Key 无效 / 账号欠费 / endpoint 不对 / 需要走代理。")
            return 1

    print("\n提示：修改了环境变量后，必须**重开终端**才会生效（setx 不会回溯当前会话）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
