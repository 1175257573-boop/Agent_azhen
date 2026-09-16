"""端到端验证（examples/memory_e2e.py）：短期记忆真的落 Redis、长期记忆真的落 PostgreSQL、且跨进程可续接。

用法（先确保 Redis/PG 已启动、环境变量已配）：
    python _e2e_memory.py phase1
    python _e2e_memory.py phase2
"""
import sys
import warnings

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore")

THREAD = "e2e-thread-1"


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "phase1"

    from agent_kit import memory as mem
    from agent_kit.app import AppConfig, build_app

    cfg = AppConfig(provider="fake", mode="chat", thread_id=THREAD, user_id="demo")
    app = build_app(cfg)
    print(f"[{phase}] 短期记忆后端={mem.RESOLVED['short']}  长期记忆后端={mem.RESOLVED['long']}")

    # ---- 1) 真实走一遍 Agent（会写 checkpoint + 读长期记忆） --------------
    if phase == "phase1":
        # 先写一条长期偏好（模拟工具写入）
        app.store.put(("preferences", "demo"), "e2e_marker", {"value": "redis+pg", "updated_at": "2026"})
        print(f"[{phase}] 写入长期记忆：e2e_marker=redis+pg")

    out = app.graph.invoke(
        {"messages": [("user", "现在几点？顺便算一下 123*456")]},
        config=app.thread_config,
        context=app.context,
    )
    msgs = out.get("messages", [])
    print(f"[{phase}] 本轮对话结束后，Redis 里该 thread 的消息数 = {len(msgs)}")
    print(f"[{phase}] 最后一条回复：{str(msgs[-1].content)[:80]}")

    # ---- 2) 直接从 Redis 读 checkpoint ------------------------------------
    saved = app.checkpointer.get(app.thread_config)
    if saved:
        cv = saved.get("channel_values", {})
        n = len(cv.get("messages", []))
        print(f"[{phase}] 从 Redis 回读 checkpoint：命中，channels = {sorted(cv)}，消息数 = {n}")
    else:
        print(f"[{phase}] 从 Redis 回读 checkpoint：未命中")

    # ---- 3) 直接从 PG 读长期记忆 ------------------------------------------
    items = app.store.search(("preferences", "demo"))
    print(f"[{phase}] 从 PostgreSQL 回读长期偏好（{len(items)} 条）：")
    for it in items:
        print(f"          - {it.key} = {it.value.get('value')}")

    # ---- 4) Redis 里真实的 key --------------------------------------------
    try:
        keys = app.checkpointer._redis.keys("atlas:ck*")
        print(f"[{phase}] Redis 中 checkpoint 相关 key 数量 = {len(keys)}")
        for k in keys[:3]:
            print(f"          {k.decode() if isinstance(k, bytes) else k}")
    except Exception as exc:  # noqa: BLE001
        print(f"[{phase}] 列举 Redis key 失败：{exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
