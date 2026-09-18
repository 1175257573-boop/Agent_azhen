"""统一启动入口 —— 对标 Spring Boot 里带 @SpringBootApplication 的那个 Application 类。

Python 没有启动注解，约定俗成的等价物是「入口文件 + if __name__ == "__main__" 守卫」。

用法：
    python main.py web                       # 启动 FastAPI Web 服务 + 前端页面
    python main.py chat                      # 交互式对话（真实模型，需配置 Key）
    python main.py chat --mode skills        # 指定能力模式
    python main.py ask "现在几点"             # 单次问答
    python main.py demo                      # 离线能力演示（8 个场景）
    python main.py check [--ping]            # 环境变量自查 / 连通性探测
    python main.py mcp                       # MCP 工具演示
    python main.py guards                    # Multi-Agent 防护演示（跑偏 / 循环拦截）
    python main.py info                      # 打印运行环境概况

等价写法：
    python -m agent_kit chat
    .\\run.ps1 chat
    agent-demo chat                          # pip install -e . 之后
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

# langchain 1.4.0 + langgraph 1.2.x 在序列化 runtime context 时会刷 Pydantic 警告，
# 属上游已知噪音，与业务代码无关，这里显式忽略以保持输出干净。
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_chat(args: argparse.Namespace) -> int:
    from agent_kit.app import MODE_HELP, AppConfig
    from ui import ChatSession

    if args.mode not in MODE_HELP:
        print(f"未知模式：{args.mode}。可用：{'、'.join(MODE_HELP)}")
        return 1

    cfg = AppConfig(
        provider=args.provider or None,
        model_name=args.model or "",
        mode=args.mode,
        # 不传 --no-hitl 时留空（None），让 atlas.toml / 环境变量决定审批档位；
        # 只有显式传了 --no-hitl 才强制 never
        enable_hitl=False if args.no_hitl else None,
        approval=getattr(args, "approval", None),
        sandbox=getattr(args, "sandbox", None),
        thread_id=args.thread,
        user_id=args.user,
        role=args.role,
        enable_mcp=args.mcp,
    )
    try:
        session = ChatSession(cfg, source="chat" if args.ask else "cli")
        session.load()
    except RuntimeError as exc:
        print(f"[配置错误] {exc}")
        return 2

    # 单次问答模式：不进 REPL
    try:
        if args.ask:
            session._talk(args.ask)
            return 0

        session.loop()
        return 0
    finally:
        session.close()   # MCP 会话持有 stdio 子进程与常驻事件循环，退出时要收干净


def cmd_demo(args: argparse.Namespace) -> int:
    from run_demo import main as run_demo_main

    argv = []
    if args.scenario:
        argv += ["--scenario", args.scenario]
    argv += ["--provider", args.provider or "fake"]
    old = sys.argv
    sys.argv = ["run_demo.py", *argv]
    try:
        run_demo_main()
    finally:
        sys.argv = old
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from check_env import main as check_env_main

    old = sys.argv
    sys.argv = ["check_env.py"] + (["--ping"] if args.ping else [])
    try:
        return check_env_main()
    finally:
        sys.argv = old


def cmd_web(args: argparse.Namespace) -> int:
    """启动 FastAPI Web 服务（CLI 之外的第二种入口）。"""
    import uvicorn

    from server.app import app as fastapi_app

    print(f"Atlas Web  →  http://{args.host}:{args.port}")
    print(f"接口文档   →  http://{args.host}:{args.port}/docs")
    uvicorn.run(fastapi_app, host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    import asyncio

    from mcp_demo import main as mcp_main

    asyncio.run(mcp_main())
    return 0


def cmd_guards(_: argparse.Namespace) -> int:
    """Multi-Agent 防护演示：跑偏拦截 + 循环拦截（离线，零 API Key）。"""
    from examples.guards_demo import main as guards_main

    guards_main()
    return 0


def cmd_retrievers(args: argparse.Namespace) -> int:
    """检索扩展点自检：列出已注册检索器，并用默认检索器跑一次查询（离线）。"""
    from agent_kit.retrieval import create, default_name, describe

    print("已注册检索器：")
    for name, doc in describe().items():
        mark = "（默认）" if name == default_name() else ""
        print(f"  - {name}{mark}：{doc}")

    query = getattr(args, "q", "") or "遗忘曲线"
    hits = create().search(query, top_k=3)
    print(f"\n默认检索器查「{query}」命中 {len(hits)} 条：")
    for hit in hits:
        snippet = " ".join(hit.text.split())[:80]
        print(f"  [{hit.score:g}] {hit.doc_id}：{snippet}...")
    if not hits:
        print("  （无命中。想接语义检索？实现 Retriever 协议并注册即可）")
    return 0


def memories_top_n() -> int:
    """Phase 2 默认参与合并的条数，唯一真相源在 memories.py。"""
    from agent_kit.memories import DEFAULT_TOP_N

    return DEFAULT_TOP_N


def cmd_memories(args: argparse.Namespace) -> int:
    """记忆产出管线：Phase 1 抽取（每会话一条）+ Phase 2 合并（出 MEMORY.md）。"""
    from agent_kit import memories

    if args.show:
        records = memories.all_records()
        if not records:
            print("还没有抽取过任何记忆。跑一次 `python main.py memories` 试试（需要真实模型）。")
            return 0
        print(f"已抽取 {len(records)} 条记忆：")
        for record in records:
            print(f"  {record.thread_id:24} 用 {record.usage_count:>2} 次  {record.summary[:60]}")
        return 0

    if args.leases:
        leases = memories.list_leases()
        if not leases:
            print("还没有任何抽取台账。跑一次 Phase 1 就会有。")
            return 0
        print(f"{len(leases)} 条会话的抽取台账：")
        for item in leases:
            print(f"  {item.thread_id:24} {item.state:8} 失败 {item.attempts} 次  "
                  f"持有者 {item.owner or '-'}  更新于 {item.updated_at}")
            if item.last_error:
                print(f"      上次错误：{item.last_error[:100]}")
        print("\n想让某条会话重跑：手工删台账或用 `memories --thread <id> --force`")
        return 0

    if args.thread:
        report = memories.run_phase1(
            threads=[args.thread],
            force=args.force,
        )
        print(report.to_text())
        return 0

    if args.background:
        # Codex 形态：后台线程跑（含资格筛选 + 并行），本进程不等它
        from agent_kit import memory_jobs

        worker, bag = memory_jobs.spawn(phase2=not args.no_bg_phase2)
        print(f"已在后台启动记忆管线（{memory_jobs.Eligibility.from_env().describe()}）")
        worker.join(timeout=args.wait)
        if bag["done"].is_set():
            print((bag.get("report") or memory_jobs.PipelineReport()).to_text())
        else:
            print(f"（{args.wait} 秒内未跑完，任务仍在后台；进程退出时会被丢弃）")
        return 0

    phase = args.phase
    if phase in ("1", "both"):
        print(memories.run_phase1(force=args.force).to_text())
    if phase in ("2", "both"):
        print(memories.run_phase2(top_n=args.top, use_git=not args.no_git).to_text())
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    """会话流水（rollout）：列会话 / 回放 / 导出 JSONL / 删除。"""
    from agent_kit import rollout

    thread = getattr(args, "thread", None)
    if not thread:
        sessions = rollout.list_sessions()
        if not sessions:
            print("还没有会话流水。先跑 `python main.py chat --ask \"你好\"` 聊一轮，再来看这里。")
            return 0
        print("最近会话（按最后活动排序）：")
        for item in sessions:
            print(f"  {item['thread_id']:24} {item['turns']:>4} 条   最后活动 {item['last_at']}")
        print("\n回放：python main.py sessions --thread <thread_id>")
        return 0

    if args.export:
        count = rollout.export(thread, args.export)
        print(f"已导出 {count} 条流水 → {args.export}")
        return 0

    if args.clear:
        count = rollout.clear(thread)
        print(f"已删除会话 {thread} 的 {count} 条流水")
        return 0

    records = rollout.load(thread)
    if not records:
        print(f"会话 {thread} 没有流水记录。")
        return 0
    print(f"会话 {thread}（共 {len(records)} 条）：")
    for item in records:
        name = f" [{item['tool_name']}]" if item["tool_name"] else ""
        text = item["content"].replace("\n", " ")[:100]
        print(f"  {item['seq']:>3} {item['role']:<8}{name} {text}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Agent 效果评估：离线自检（默认）或真机评估（--real）。"""
    from examples.eval_demo import main as eval_main

    argv = []
    if getattr(args, "real", False):
        argv.append("--real")
    if getattr(args, "json", False):
        argv.append("--json")
    if getattr(args, "show_all", False):
        argv.append("--show-all")
    if getattr(args, "save", None):
        argv += ["--save", args.save]
    if getattr(args, "compare", None):
        argv += ["--compare", args.compare]
    return eval_main(argv)


def cmd_info(_: argparse.Namespace) -> int:
    from agent_kit.app import MODE_HELP
    from agent_kit.config import DEFAULT_MODELS, AgentSettings, detect_provider, get_env_key_name

    print("=" * 72)
    print("  运行环境概况")
    print("=" * 72)
    print(f"  Python        : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  工程根目录    : {PROJECT_ROOT}")

    import langchain

    print(f"  langchain     : {langchain.__version__}")

    provider = os.getenv("LLM_PROVIDER") or detect_provider()
    env_var = get_env_key_name(provider)
    settings = AgentSettings(provider=provider)
    print(f"  生效 provider : {provider}  (Key 来源：{env_var or '未配置'})")
    print(f"  生效 model    : {settings.model_name}")
    print(f"  写文件沙箱    : {settings.sandbox_dir}")
    print(f"  可用 provider : {', '.join(DEFAULT_MODELS)}")

    from agent_kit import memory as mem

    print("\n  记忆层：")
    for line in mem.report_lines():
        print(line)

    from agent_kit.policy import describe, resolve

    policy = resolve()
    print("\n  审批 / 沙箱策略：")
    print(f"    {describe(policy)}")
    print(f"    取值来源：{policy.source}")
    for warning in policy.warnings:
        print(f"    ⚠️ {warning}")

    print("\n  能力模式：")
    for name, desc in MODE_HELP.items():
        print(f"    {name:12} {desc}")
    return 0


# ---------------------------------------------------------------------------
# CLI 装配
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Atlas · LangChain 1.4 Agent 统一启动入口",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别；排查工具调用链路时用 DEBUG",
    )
    parser.add_argument(
        "--log-level",
        dest="top_log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（顶层写法，写在子命令后亦可）",
    )

    # 不设 required=True：直接 `python main.py`（PyCharm 里右键 Run 就是这种）
    # 时自动进入 chat 交互模式，而不是抛「the following arguments are required」。
    sub = parser.add_subparsers(dest="command", required=False)

    p_chat = sub.add_parser("chat", parents=[common], help="交互式对话（真实模型）")
    # 模式清单以 agent_kit.app.MODE_HELP 为唯一真相源，避免这里重复一份字符串
    from agent_kit.app import MODE_HELP

    modes = "、".join(MODE_HELP)
    p_chat.add_argument("--mode", default="chat", help=f"能力模式：{modes}")
    p_chat.add_argument("--provider", "-p", help="fake|openai|deepseek|dashscope|anthropic")
    p_chat.add_argument("--model", "-m", help="模型名，覆盖默认")
    p_chat.add_argument("--thread", default="atlas-main", help="会话 thread_id")
    p_chat.add_argument("--user", default="demo", help="用户标识（长期记忆按此隔离）")
    p_chat.add_argument("--role", default="admin", help="角色：admin 可写文件，其余只读")
    p_chat.add_argument("--no-hitl", action="store_true", help="关闭写操作的二次确认（等价于 --approval never）")
    p_chat.add_argument(
        "--approval",
        help="审批档位：untrusted（写前必问，默认）/ on-failure（失败才转人工）/ never（全放行）",
    )
    p_chat.add_argument(
        "--sandbox",
        help="沙箱档位：read-only / workspace-write（默认）/ danger-full-access",
    )
    p_chat.add_argument("--mcp", action="store_true", help="在当前模式上叠加 MCP Server 的工具")
    p_chat.add_argument("--ask", help="单次问答，答完即退出（不进交互）")
    p_chat.set_defaults(func=cmd_chat)

    p_demo = sub.add_parser("demo", parents=[common], help="离线能力演示（8 个场景）")
    p_demo.add_argument("--scenario", "-s", help="指定场景，不填则全部跑")
    p_demo.add_argument("--provider", "-p", help="默认 fake（离线脚本模型）")
    p_demo.set_defaults(func=cmd_demo)

    p_check = sub.add_parser("check", parents=[common], help="环境变量自查")
    p_check.add_argument("--ping", action="store_true", help="额外做一次真实调用探测连通性")
    p_check.set_defaults(func=cmd_check)

    p_web = sub.add_parser("web", parents=[common], help="启动 FastAPI Web 服务（含前端页面）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    p_web.add_argument("--reload", action="store_true", help="代码改动自动重启（开发用）")
    p_web.set_defaults(func=cmd_web)

    p_mcp = sub.add_parser("mcp", parents=[common], help="MCP 工具接入演示")
    p_mcp.set_defaults(func=cmd_mcp)

    p_guards = sub.add_parser("guards", parents=[common], help="Multi-Agent 防护演示（跑偏 / 循环拦截，离线）")
    p_guards.set_defaults(func=cmd_guards)

    p_ret = sub.add_parser("retrievers", parents=[common], help="检索扩展点自检（列出已注册检索器）")
    p_ret.add_argument("--q", default="遗忘曲线", help="用默认检索器试查一句话")
    p_ret.set_defaults(func=cmd_retrievers)

    p_mem = sub.add_parser("memories", parents=[common], help="记忆产出：Phase1 抽取 + Phase2 合并出 MEMORY.md")
    p_mem.add_argument("--show", action="store_true", help="只列出已抽取的记忆")
    p_mem.add_argument("--leases", action="store_true", help="列出 Phase1 的抽取台账（锁状态 / 失败次数）")
    p_mem.add_argument("--phase", choices=("1", "2", "both"), default="both", help="只跑某个阶段")
    p_mem.add_argument("--top", type=int, default=memories_top_n(), help="Phase2 参与合并的记忆条数")
    p_mem.add_argument("--thread", help="只抽取这一个会话")
    p_mem.add_argument("--force", action="store_true", help="强制重抽：忽略「流水没变就跳过」和已完成状态")
    p_mem.add_argument("--no-git", action="store_true", help="Phase2 不用 git 基线 diff，退回全量重写")
    p_mem.add_argument("--background", action="store_true", help="Codex 形态：后台线程跑记忆管线（含资格筛选与并行）")
    p_mem.add_argument("--wait", type=int, default=0, help="配合 --background：最多等多少秒")
    p_mem.add_argument("--no-bg-phase2", action="store_true", help="配套后台模式：本轮不接着跑 Phase2")
    p_mem.set_defaults(func=cmd_memories)

    p_sess = sub.add_parser("sessions", parents=[common], help="会话流水（rollout）：列会话 / 回放 / 导出")
    p_sess.add_argument("--thread", help="会话 thread_id；不填则列出最近会话")
    p_sess.add_argument("--export", metavar="PATH", help="把该会话导出为 JSONL")
    p_sess.add_argument("--clear", action="store_true", help="删除该会话的流水")
    p_sess.set_defaults(func=cmd_sessions)

    p_eval = sub.add_parser("eval", parents=[common], help="Agent 效果评估（离线自检 / --real 真机）")
    p_eval.add_argument("--real", action="store_true", help="真机评估：调用真实模型，需要 API Key")
    p_eval.add_argument("--json", action="store_true", help="输出 JSON")
    p_eval.add_argument("--show-all", action="store_true", help="列出全部用例而非仅失败项")
    p_eval.add_argument("--save", metavar="PATH", help="把结果存为基线 JSON")
    p_eval.add_argument("--compare", metavar="PATH", help="与基线对比，通过率下降则返回非零")
    p_eval.set_defaults(func=cmd_eval)

    p_info = sub.add_parser("info", parents=[common], help="打印运行环境概况")
    p_info.set_defaults(func=cmd_info)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    # 无子命令（PyCharm 直接右键 Run、或裸敲 `python main.py`）→ 默认当作 chat。
    # 重新解析一遍，让 chat 子命令的默认值（--mode/--thread/--user/...）都补上，
    # 否则 args 里没有这些字段，后面 ChatSession 会 AttributeError。
    if args.command is None:
        args = parser.parse_args(["chat", *argv])

    level = getattr(args, "log_level", "INFO")
    if level == "INFO":
        level = getattr(args, "top_log_level", "INFO")
    from agent_kit.logging_conf import setup_logging

    setup_logging(level=level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
