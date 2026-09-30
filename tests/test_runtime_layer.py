"""统一运行层验证 —— 阶段二。

阶段一把 CLI（ui.py）和 Web（server/service/agent_service.py）各自那套
「跑图 → 翻译事件 → 落流水」的循环合并成了 `agent_kit/runtime.py` 一处。
这个文件验证合并**真的生效**，而不是各跑各的：

1. 同一条执行链路：CLI 与 Web 的事件序列一致
2. 会话隔离：运行层不把 thread_id 写回共享的 BuiltApp
3. 中断识别：顶层 / 嵌套两种形状的 __interrupt__ 都认（合并前两边都漏了顶层）
4. 流水：CLI 落 cli 来源、Web 落 chat 来源，两者都在记忆 Phase 1 的可见范围内
5. 排队：Web 排队消息在同一条 SSE 里被按序消费

零 API Key、零外部服务，全部离线可复现。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from agent_kit import rollout
from agent_kit.agent import build_agent
from agent_kit.app import AppConfig, BuiltApp
from agent_kit.config import AgentSettings
from agent_kit import memory as mem
from agent_kit.policy import resolve
from agent_kit.runtime import AgentRuntime, INTERRUPT, DONE, TOKEN, extract_interrupts
from agent_kit.scripted_model import ScriptedChatModel, has_tool_output, tool_call
from server.schemas import ChatRequest
from server.service.agent_service import AgentService

PROVIDER = "fake"


def _build_app(tmp_path, *, hitl: bool, thread_id: str) -> BuiltApp:
    """装一个离线图；hitl=True 时调用 write_report 会先中断。"""

    def step(messages, _tools):
        if has_tool_output(messages, "write_report"):
            return AIMessage(content="报告已写入。")
        return tool_call("write_report", {"filename": "rt.md", "content": "# OK"})

    settings = AgentSettings(provider=PROVIDER, workspace=tmp_path)
    model = ScriptedChatModel(script=[step], terminal_reply="done")
    built = build_agent(
        settings, model=model, include_write_tools=True, enable_hitl=hitl,
        checkpointer=mem.build_checkpointer(), store=mem.build_store(), message_window=20,
    )
    cfg = AppConfig(provider=PROVIDER, mode="chat", user_id="rt", role="admin", thread_id=thread_id)
    return BuiltApp(built.graph, cfg, settings, built.checkpointer, built.store, policy=resolve(role="admin"))


# ---------------------------------------------------------------------------
# 1. 同一条执行链路
# ---------------------------------------------------------------------------
def test_cli_and_web_emit_the_same_event_sequence(tmp_path):
    """CLI 与 Web 用同一个运行层，事件序列必须一致（否则说明又分叉了）。"""
    cli_app = _build_app(tmp_path, hitl=False, thread_id="uni-cli")
    web_app = _build_app(tmp_path, hitl=False, thread_id="uni-web")

    cli = [e["type"] for e in AgentRuntime(cli_app, source=rollout.SOURCE_CLI).run("你好", "uni-cli")]
    web = [e["type"] for e in AgentRuntime(web_app, source=rollout.SOURCE_CHAT).run("你好", "uni-web")]

    assert TOKEN in cli and TOKEN in web, f"没有产出文本：cli={cli} web={web}"
    assert cli[-1] == DONE and web[-1] == DONE, "两条链路都没有以 done 收尾"
    # 事件类型序列一致（内容随会话而异，但形状必须相同）
    assert cli == web, f"CLI 与 Web 的执行链路已经分叉：cli={cli} web={web}"


def test_web_service_delegates_to_the_same_runtime(tmp_path):
    """Web 服务层只是「运行层事件 → SSE」的编码器，不自己跑图。"""
    svc = AgentService()
    svc._apps[("chat", PROVIDER, "rt", "admin", False)] = _build_app(tmp_path, hitl=False, thread_id="svc-1")

    events = list(svc.stream(ChatRequest(
        message="你好", thread_id="svc-1", mode="chat", provider=PROVIDER,
        user_id="rt", role="admin", enable_mcp=False,
    )))
    types = [e["type"] for e in events]
    assert TOKEN in types and types[-1] == DONE, f"Web 事件流异常：{types}"


# ---------------------------------------------------------------------------
# 2. 会话隔离
# ---------------------------------------------------------------------------
def test_runtime_never_writes_thread_id_back_to_the_shared_app(tmp_path):
    """运行层只按传入的 thread_id 执行，绝不改写共享的 BuiltApp。"""
    app = _build_app(tmp_path, hitl=False, thread_id="owner")
    runtime = AgentRuntime(app, source=rollout.SOURCE_CLI)

    list(runtime.run("一句话", "session-a"))
    assert app.config.thread_id == "owner", "运行层把 thread_id 写回了共享的 app.config"

    list(runtime.run("另一句", "session-b"))
    assert app.config.thread_id == "owner"


# ---------------------------------------------------------------------------
# 3. 中断识别
# ---------------------------------------------------------------------------
def test_extract_interrupts_handles_both_shapes():
    """LangGraph 的中断有顶层与嵌套两种形状，都得认出来。"""
    class _Interrupt:
        def __init__(self, value):
            self.value = value

    payload = {"action_requests": [{"name": "write_report", "args": {}}]}

    # 顶层（真实形状）：值是 tuple，早期实现被 isinstance(value, dict) 跳过
    assert extract_interrupts({"__interrupt__": (_Interrupt(payload),)}) == [payload]
    # 嵌套在节点下
    assert extract_interrupts({"tools": {"__interrupt__": (_Interrupt(payload),)}}) == [payload]
    # 没有中断
    assert extract_interrupts({"tools": {"messages": []}}) == []


def test_runtime_reports_interrupt_for_hitl(tmp_path):
    """HITL 打开时，运行层必须把中断作为事件抛给调用方（CLI/Web 都一样）。"""
    app = _build_app(tmp_path, hitl=True, thread_id="hitl-rt")
    events = list(AgentRuntime(app, source=rollout.SOURCE_CLI).run("写个报告", "hitl-rt"))
    assert any(e["type"] == INTERRUPT for e in events), f"运行层丢了中断：{[e['type'] for e in events]}"
    # 中断期间工具不能先跑掉
    assert not (tmp_path / "runs" / "rt.md").exists(), "中断期间工具已经被执行了"


# ---------------------------------------------------------------------------
# 4. 流水：CLI 与 Web 都落，且都在记忆 Phase 1 的可见范围
# ---------------------------------------------------------------------------
def test_cli_and_web_both_land_in_interactive_rollout(tmp_path):
    """记忆 Phase 1 只抽交互会话（cli / chat）；两边都得落得上。"""
    cli_app = _build_app(tmp_path, hitl=False, thread_id="src-cli")
    web_app = _build_app(tmp_path, hitl=False, thread_id="src-web")

    list(AgentRuntime(cli_app, source=rollout.SOURCE_CLI).run("终端问的", "src-cli"))
    list(AgentRuntime(web_app, source=rollout.SOURCE_CHAT).run("网页问的", "src-web"))

    sessions = {s["thread_id"]: s for s in rollout.list_sessions(sources=rollout.INTERACTIVE_SOURCES)}
    assert "src-cli" in sessions, "CLI 会话没落流水"
    assert "src-web" in sessions, "Web 会话没落流水"
    assert sessions["src-cli"]["source"] == rollout.SOURCE_CLI
    assert sessions["src-web"]["source"] == rollout.SOURCE_CHAT


def test_non_interactive_source_is_excluded_from_memory_phase1(tmp_path):
    """sub-agent / 工具会话不该进长期记忆——来源过滤必须生效。"""
    app = _build_app(tmp_path, hitl=False, thread_id="src-tool")
    list(AgentRuntime(app, source=rollout.SOURCE_TOOL).run("内部调度", "src-tool"))

    sessions = {s["thread_id"] for s in rollout.list_sessions(sources=rollout.INTERACTIVE_SOURCES)}
    assert "src-tool" not in sessions, "工具内部会话混进了交互流水"


# ---------------------------------------------------------------------------
# 6. CLI 终端链路（改写过的接线本身也要跑通）
# ---------------------------------------------------------------------------
def test_cli_session_asks_and_resumes_on_approval(tmp_path, monkeypatch, capsys):
    """终端里：中断 → 问决策 → approve → 工具真实执行。

    这条路径在合并前是坏的：旧 CLI 只认嵌套形状的中断，压根不弹审批。
    """
    from ui import ChatSession

    session = ChatSession(AppConfig(provider=PROVIDER, mode="chat", user_id="rt",
                                    role="admin", thread_id="cli-hitl"), source=rollout.SOURCE_CLI)
    session.app = _build_app(tmp_path, hitl=True, thread_id="cli-hitl")

    monkeypatch.setattr("builtins.input", lambda *_: "a")   # a = 同意
    session._talk("写个报告")

    out = capsys.readouterr().out
    assert "待审批" in out, f"终端没有弹审批：{out}"
    assert (tmp_path / "runs" / "rt.md").exists(), "approve 后文件没有落盘"


def test_cli_session_respects_rejection(tmp_path, monkeypatch, capsys):
    """终端里选拒绝，文件就不能被写出来。"""
    from ui import ChatSession

    session = ChatSession(AppConfig(provider=PROVIDER, mode="chat", user_id="rt",
                                    role="admin", thread_id="cli-rej"), source=rollout.SOURCE_CLI)
    session.app = _build_app(tmp_path, hitl=True, thread_id="cli-rej")

    monkeypatch.setattr("builtins.input", lambda *_: "r")   # r = 拒绝（随后要理由）
    session._talk("写个报告")

    assert not (tmp_path / "runs" / "rt.md").exists(), "拒绝后文件仍然被写出来了"


# ---------------------------------------------------------------------------
# 5. 排队
# ---------------------------------------------------------------------------
def test_queued_messages_are_drained_after_the_round(tmp_path):
    """Agent 忙碌期间入队的消息，本轮结束后按序自动发出。"""
    svc = AgentService()
    svc._apps[("chat", PROVIDER, "rt", "admin", False)] = _build_app(tmp_path, hitl=False, thread_id="q-1")
    svc.enqueue(thread_id="q-1", text="第二条")
    svc.enqueue(thread_id="q-1", text="第三条")

    events = list(svc.stream(ChatRequest(
        message="第一条", thread_id="q-1", mode="chat", provider=PROVIDER,
        user_id="rt", role="admin", enable_mcp=False,
    )))
    drained = [e["data"]["text"] for e in events if e["type"] == "queued_start"]
    assert drained == ["第二条", "第三条"], f"排队顺序错误：{drained}"

    history = svc.history(thread_id="q-1", mode="chat", provider=PROVIDER, user_id="rt", role="admin")
    texts = [m["content"] for m in history if m["role"] == "user"]
    for text in ("第一条", "第二条", "第三条"):
        assert any(text in t for t in texts), f"排队消息 {text} 没被执行"
