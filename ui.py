"""交互式终端（REPL）—— 这个项目的「软件外壳」。

    python main.py chat                     # 默认模式
    python main.py chat --mode skills       # 指定能力模式
    python main.py chat --mcp               # 在 chat 模式上叠加 MCP 工具
    python main.py chat --mode mcp          # 纯 MCP 模式

会话内支持斜杠命令：
    /help          显示帮助
    /mode <名称>   切换能力模式（chat/structured/dynamic/mcp/skills/handoffs/subagents/router）
    /mcp [on|off]  开关 MCP 工具叠加（在任意模式上并入 MCP Server 的工具）
    /tools         列出当前模式可用工具
    /thread <id>   切换会话（换 id = 全新对话；换回来 = 继续上次）
    /stream        切换流式/整段输出
    /reset         清空当前会话
    /exit          退出

写文件类操作会触发人工确认（HITL），在终端里直接回答：
    a=同意  e=改写后执行  r=拒绝并说明理由  s=自己回答
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_kit.app import MODE_HELP, AppConfig, build_app, build_app_async
from agent_kit.logging_conf import get_logger
from agent_kit.runtime import AgentRuntime
from agent_kit.streaming import text_of_message

log = get_logger("agent.chat")

# 一次回复可能连着触发多个待审批项，问完再跑又可能再触发；给个上限防死循环
_MAX_APPROVAL_ROUNDS = 5

HELP = """\
可用命令：
  /help              显示本帮助
  /mode <名称>       切换能力模式，当前支持：{modes}
  /mcp [on|off]      开关 MCP 工具叠加（把 MCP Server 的工具并入当前模式）
  /tools             列出当前模式的可用工具
  /thread <id>       切换会话 thread（同一 id 会续接历史）
  /stream            切换流式输出开关
  /reset             清空当前会话历史
  /info              打印当前配置（provider / 模型 / 模式）
  /mem               打印记忆层状态（短期/长期后端、消息窗口）
  /exit 或 /quit     退出

直接输入文字即与 Agent 对话。
遇到写文件等敏感操作会暂停等待你确认：a=同意 / e=改写 / r=拒绝 / s=代答
"""


class ChatSession:
    """一次终端会话的状态与循环。"""

    def __init__(self, cfg: AppConfig, source: str = "cli") -> None:
        self.cfg = cfg
        self.app = None
        self.streaming = True
        self._loop: Any = None
        self._pending: list[Any] = []
        # 流水来源：REPL 是 cli，单次 --ask 是 chat，两者都算交互会话（记忆 Phase 1 只抽这两类）；
        # sub-agent / 工具会话反映的只是 agent 内部调度，不该被当成长久记忆
        self.source = source

    # ------------------------------------------------------------ 事件循环
    @property
    def event_loop(self) -> Any:
        """MCP 会话必须**全程复用同一个事件循环**。

        MCP 的 stdio 连接是在装配时绑定到某个 loop 上的；如果装配用
        `asyncio.run()`（跑完就关 loop）、执行再用一次 `asyncio.run()`，
        工具调用就会报 `Event loop is closed`。
        所以这里为整个会话维护一个常驻 loop。
        """
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def close(self) -> None:
        """收尾：先优雅关掉 MCP 连接（stdio 子进程），再关事件循环。

        顺序反了就会在退出时看到
        `Exception ignored ... RuntimeError: Event loop is closed`。
        """
        loop = self._loop
        self._loop = None
        if loop is None or loop.is_closed():
            return

        try:
            hub = getattr(self.app, "mcp_hub", None) if self.app else None
            if hub is not None:
                loop.run_until_complete(hub.aclose())
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception as exc:  # noqa: BLE001
            log.debug("关闭会话时出错：%s: %s", type(exc).__name__, exc)
        finally:
            loop.close()

    # ------------------------------------------------------------ 加载
    def load(self) -> None:
        """含 MCP 工具时必须异步装配：MCP 工具只实现了 ainvoke。"""
        if self.cfg.needs_mcp:
            self.app = self.event_loop.run_until_complete(build_app_async(self.cfg))
        else:
            self.app = build_app(self.cfg)

    def reload(self, **changes: Any) -> None:
        data = {**self.cfg.__dict__, **changes}
        self.cfg = AppConfig(**data)
        self.load()

    # ------------------------------------------------------------ 命令
    def cmd_tools(self) -> None:
        graph = self.app.graph
        names = _tool_names(graph)
        print(f"\n当前模式 {self.cfg.mode} 可用工具（{len(names)}）：")
        for n in names:
            print(f"  · {n}")
        print()

    def cmd_info(self) -> None:
        s = self.app.settings
        print(f"\n  provider : {s.provider}")
        print(f"  model    : {s.model_name}")
        print(f"  mode     : {self.cfg.mode}  ({MODE_HELP.get(self.cfg.mode, '')})")
        print(f"  MCP      : {'已接入（' + '、'.join(self.app.mcp_tool_names) + '）' if self.app.is_mcp else '未接入'}")
        print(f"  thread   : {self.cfg.thread_id}")
        print(f"  流式输出 : {'开' if self.streaming else '关'}\n")

    def cmd_mem(self) -> None:
        from agent_kit import memory as mem

        print("\n  记忆层：")
        for line in mem.report_lines():
            print(line)
        print(f"    {mem.pad_display('当前 thread')}{self.cfg.thread_id}\n")

    def cmd_reset(self) -> None:
        # 换一个 thread_id 即得到全新会话；旧会话仍可通过 /thread 切回
        new_id = f"{self.cfg.thread_id}-{id(self) % 997}"
        self.cfg.thread_id = new_id
        print(f"已开启新会话：{new_id}\n")

    # ------------------------------------------------------------ 主循环
    def loop(self) -> None:
        print_banner(self.app.settings, self.cfg)
        print(HELP.format(modes="、".join(MODE_HELP)))

        while True:
            try:
                raw = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return

            if not raw:
                continue
            if raw.startswith("/"):
                if self._dispatch(raw):
                    return
                continue

            self._talk(raw)

    def _dispatch(self, raw: str) -> bool:
        """处理斜杠命令，返回 True 表示要退出。"""
        parts = raw.split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

        if cmd in ("/exit", "/quit"):
            print("已退出。")
            return True
        if cmd == "/help":
            print(HELP.format(modes="、".join(MODE_HELP)))
        elif cmd == "/tools":
            self.cmd_tools()
        elif cmd == "/info":
            self.cmd_info()
        elif cmd == "/mem":
            self.cmd_mem()
        elif cmd == "/stream":
            self.streaming = not self.streaming
            print(f"流式输出已{'开启' if self.streaming else '关闭'}。\n")
        elif cmd == "/reset":
            self.cmd_reset()
        elif cmd == "/thread":
            if not arg:
                print("用法：/thread <会话id>\n")
            else:
                self.cfg.thread_id = arg
                print(f"已切换到会话：{arg}\n")
        elif cmd == "/mode":
            if arg not in MODE_HELP:
                print(f"未知模式：{arg}。可用：{'、'.join(MODE_HELP)}\n")
            else:
                self.reload(mode=arg)
                print(f"已切换到模式：{arg}（{MODE_HELP[arg]}）\n")
        elif cmd == "/mcp":
            want = None if not arg else arg.lower() in ("on", "1", "true", "开")
            if want is None:
                want = not self.cfg.enable_mcp
            try:
                self.reload(enable_mcp=want)
            except Exception as exc:  # noqa: BLE001
                print(f"切换失败：{type(exc).__name__}: {exc}\n")
            else:
                state = "已接入" if want else "已关闭"
                extra = f"（{'、'.join(self.app.mcp_tool_names)}）" if want and self.app.mcp_tool_names else ""
                print(f"MCP {state}{extra}\n")
        else:
            print(f"未知命令：{cmd}，输入 /help 查看。\n")
        return False

    # ------------------------------------------------------------ 对话
    def runtime(self) -> AgentRuntime:
        """本次执行用的运行层。每次现取——app 会在 /mode、/mcp 之后被换掉。"""
        return AgentRuntime(self.app, source=self.source)

    def _talk(self, text: str) -> None:
        """跑一轮：执行交给运行层，这里只负责把事件渲染成终端文本。"""
        thread_id = self.cfg.thread_id
        runtime = self.runtime()
        self._pending = []

        try:
            if self.app.is_mcp:
                # MCP 工具没有同步实现，整条链路都得走异步（且复用同一 loop）
                self.event_loop.run_until_complete(self._render(runtime.arun(text, thread_id)))
            elif self.streaming:
                self._render(runtime.run(text, thread_id))
            else:
                self._render_invoke(runtime, text, thread_id)

            self._settle(runtime, thread_id)
        except KeyboardInterrupt:
            print("\n（已中断本次回答）")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[出错] {type(exc).__name__}: {exc}\n")

    # ------------------------------------------------------------ 渲染
    def _render(self, events: Any) -> None:
        """把运行层事件打印成终端文本；中断先攒着，跑完再统一问用户。"""
        print("\nAtlas > ", end="", flush=True)
        printed = False
        for event in events:
            kind = event["type"]
            if kind == "token":
                print(event["data"], end="", flush=True)
                printed = True
            elif kind == "custom":
                print(f"\n  · {event['data']}", end="", flush=True)
                printed = True
            elif kind == "interrupt":
                print()
                self._pending.extend(event["data"])
                printed = True
            elif kind == "error":
                print(f"\n[出错] {event['data']}", end="", flush=True)
                printed = True
        print("\n" if printed else "(无输出)\n")

    def _render_invoke(self, runtime: AgentRuntime, text: str, thread_id: str) -> None:
        """非流式的整段输出（/stream 关掉时走这条）。"""
        out = runtime.invoke(text, thread_id)
        if out.get("error"):
            print(f"\n[出错] {out['error']}\n")
            return

        result = out["result"] or {}
        if getattr(self.app, "is_workflow", False):
            print(f"\nAtlas > {result.get('final_answer', '')}\n")
            return

        self._pending = list(out["interrupts"])
        if self._pending:
            print()
            return

        if result.get("messages"):
            print(f"\nAtlas > {text_of_message(result['messages'][-1])}\n")
        self._print_structured(result)

    @staticmethod
    def _print_structured(result: dict) -> None:
        structured = result.get("structured_response")
        if structured is None:
            return
        print("  —— 结构化输出 ——")
        print(f"  标题   ：{structured.title}")
        print(f"  置信度 ：{structured.confidence:.0%}")
        for item in structured.key_findings:
            print(f"   · {item.fact}（来源：{item.source}）")
        print()

    def _settle(self, runtime: AgentRuntime, thread_id: str) -> None:
        """把攒下的中断逐轮问完（一次回复可能连着触发多个待审批项）。"""
        for _ in range(_MAX_APPROVAL_ROUNDS):
            if not self._pending:
                return
            decisions = self._ask_decisions(self._pending)
            self._pending = []
            if not decisions:
                return
            if self.app.is_mcp:
                self.event_loop.run_until_complete(
                    self._render(runtime.aresume(decisions, thread_id))
                )
            else:
                self._render(runtime.resume(decisions, thread_id))

    # ------------------------------------------------------------ 人工确认
    def _ask_decisions(self, values: list[Any]) -> list[dict]:
        """把待审批项逐条问用户，返回决策列表（纯 I/O，不涉及图）。"""
        decisions = []
        for value in values:
            if not isinstance(value, dict):
                continue
            for action in value.get("action_requests", []):
                print(f"\n⏸ 待审批：{action['name']}  参数={action['args']}")
                allowed = value.get("review_configs", [{}])[0].get("allowed_decisions", ["approve"])
                choice = input(f"  决策（{'/'.join(allowed)}）[a=同意/e=改写/r=拒绝/s=代答] > ").strip().lower() or "a"

                if choice.startswith("a"):
                    decisions.append({"type": "approve"})
                elif choice.startswith("e"):
                    new_args = input("  请输入新的 JSON 参数 > ").strip()
                    import json
                    try:
                        parsed = json.loads(new_args)
                    except json.JSONDecodeError:
                        print("  JSON 解析失败，按同意处理。")
                        parsed = action["args"]
                    decisions.append(
                        {"type": "edit", "edited_action": {"name": action["name"], "args": parsed}}
                    )
                elif choice.startswith("s"):
                    answer = input("  你的回答 > ").strip()
                    decisions.append({"type": "respond", "message": answer})
                else:
                    reason = input("  拒绝理由（可留空）> ").strip()
                    decisions.append({"type": "reject", "message": reason or "用户在终端拒绝"})

        return decisions

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _tool_names(graph: Any) -> list[str]:
    try:
        node = graph.get_graph().nodes.get("tools")
        return sorted(getattr(node.data, "tools_by_name", {}).keys()) if node else []
    except Exception:  # noqa: BLE001
        return []


def print_banner(settings: Any, cfg: AppConfig) -> None:
    print("=" * 72)
    print("  Atlas · LangChain 1.4 Agent")
    print(f"  provider={settings.provider}  model={settings.model_name}  mode={cfg.mode}")
    print(f"  thread={cfg.thread_id}")
    print("=" * 72)
