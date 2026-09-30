"""运行层端到端验证 —— 用**真实离线图**（fake provider 的 ScriptedChatModel + LangGraph）跑通：

1. 会话隔离：同一 AgentService 并发服务多个 thread_id，历史互不串话
   （回归：缓存 BuiltApp 的 thread_id 曾被并发改写，A 的消息写进 B 的会话）
2. 审批闭环：写工具触发 HITL 中断 → approve 决策 → 工具真实执行并落盘
3. 排队：Agent 忙碌期间入队的消息在本轮结束后按序自动发出
4. 流水：Web 会话也要落 rollout（此前只有 CLI 落，记忆 Phase 1 永远看不到 Web 会话）
5. 记忆召回：长期偏好写入 → 读回（跨进程共享同一 SQLite store）

零 API Key、零外部服务，全部离线可复现。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent_kit import memory as mem
from agent_kit import rollout
from agent_kit.app import AppConfig, BuiltApp
from agent_kit.agent import build_agent
from agent_kit.config import AgentSettings
from agent_kit.policy import resolve
from agent_kit.scripted_model import ScriptedChatModel, has_tool_output, tool_call
from server.service.agent_service import AgentService
from server.service.memory_service import MemoryService
from server.schemas import ChatRequest, ResumeRequest

PROVIDER = "fake"


def _make_service() -> AgentService:
    """干净的 AgentService（测试进程内自己 new，不碰 server.app 的单例）。"""
    return AgentService()


def _req(**overrides) -> ChatRequest:
    fields = dict(message="你好", thread_id="iso-a", mode="chat", provider=PROVIDER,
                  user_id="e2e", role="admin", enable_mcp=False)
    fields.update(overrides)
    return ChatRequest(**fields)


# ---------------------------------------------------------------------------
# 1. 会话隔离（并发）
# ---------------------------------------------------------------------------
def test_concurrent_streams_stay_in_their_own_threads(tmp_path):
    svc = _make_service()
    thread_ids = [f"iso-{i}" for i in range(4)]

    def talk(tid: str) -> None:
        events = list(svc.stream(_req(thread_id=tid, message=f"来自 {tid} 的消息")))
        assert any(e["type"] == "done" for e in events), f"{tid} 缺少 done 事件"

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(talk, thread_ids))

    for tid in thread_ids:
        history = svc.history(thread_id=tid, mode="chat", provider=PROVIDER,
                              user_id="e2e", role="admin")
        texts = [m["content"] for m in history]
        assert any(f"来自 {tid} 的消息" in t for t in texts), f"{tid} 看不到自己的消息"
        other = [t for t in texts for x in thread_ids if x != tid and f"来自 {x} 的消息" in t]
        assert not other, f"会话隔离被打破：{tid} 里串进了别人的消息 {other}"


# ---------------------------------------------------------------------------
# 2. 审批闭环：中断 → approve → 写工具真实执行
# ---------------------------------------------------------------------------
def _hitl_app(tmp_path) -> BuiltApp:
    """装配一个「调用 write_report 就触发人工确认」的离线图。"""

    def step(messages, _tools):
        # 第一次：发起写工具调用；拿到工具结果后：收尾回复
        if has_tool_output(messages, "write_report"):
            return AIMessage(content="报告已写入。")
        return tool_call("write_report", {"filename": "e2e-approval.md", "content": "# OK"})

    settings = AgentSettings(provider=PROVIDER, workspace=tmp_path)
    model = ScriptedChatModel(script=[step], terminal_reply="done")
    built = build_agent(
        settings,
        model=model,
        include_write_tools=True,
        enable_hitl=True,
        checkpointer=mem.build_checkpointer(),
        store=mem.build_store(),
        message_window=20,
    )
    cfg = AppConfig(provider=PROVIDER, mode="chat", enable_hitl=True,
                    user_id="e2e", role="admin", thread_id="appr-1")
    return BuiltApp(
        built.graph, cfg, settings, built.checkpointer, built.store,
        policy=resolve(role="admin"),
    )


def test_approval_interrupt_then_approve_executes_the_tool(tmp_path):
    svc = _make_service()
    svc._apps[("chat", PROVIDER, "e2e", "admin", False)] = _hitl_app(tmp_path)

    events = list(svc.stream(_req(thread_id="appr-1", message="帮我写个报告")))
    interrupts = [e for e in events if e["type"] == "interrupt"]
    assert interrupts, f"未触发人工确认中断：{[e['type'] for e in events]}"
    # 中断期间工具不能先跑掉，否则「先斩后奏」审批就失去意义
    assert not (tmp_path / "runs" / "e2e-approval.md").exists(), "中断期间工具已经被执行了"

    resume_events = list(svc.resume(ResumeRequest(
        thread_id="appr-1", mode="chat", provider=PROVIDER,
        user_id="e2e", role="admin",
        decisions=[{"type": "approve"}],
    )))
    tool_ends = [e for e in resume_events if e["type"] == "tool_end" and e["data"]["name"] == "write_report"]
    assert tool_ends, f"approve 后工具没有真实执行：{[e['type'] for e in resume_events]}"
    assert "rejected" not in tool_ends[0]["data"]["output"].lower(), "approve 被当成 reject 处理"

    report = tmp_path / "runs" / "e2e-approval.md"
    assert report.exists(), "write_report 落盘文件不存在"
    assert "# OK" in report.read_text(encoding="utf-8")


def test_approval_reject_does_not_execute_the_tool(tmp_path):
    svc = _make_service()
    svc._apps[("chat", PROVIDER, "e2e", "admin", False)] = _hitl_app(tmp_path)

    first = list(svc.stream(_req(thread_id="appr-2", message="帮我写个报告")))
    assert any(e["type"] == "interrupt" for e in first), "reject 场景没有先中断"
    assert not (tmp_path / "runs" / "e2e-approval.md").exists(), "中断期间工具已经被执行了"

    resume_events = list(svc.resume(ResumeRequest(
        thread_id="appr-2", mode="chat", provider=PROVIDER,
        user_id="e2e", role="admin",
        decisions=[{"type": "reject", "message": "不要写"}],
    )))
    ends = [e for e in resume_events if e["type"] == "tool_end" and e["data"]["name"] == "write_report"]
    # LangGraph 会把 reject 作为工具的返回结果回灌（一条 ToolMessage），
    # 所以「有没有 tool_end 事件」不能作为判据，**文件有没有落盘**才是。
    if ends:
        assert "rejected" in ends[0]["data"]["output"].lower(), f"reject 结果异常：{ends[0]['data']}"
    assert not (tmp_path / "runs" / "e2e-approval.md").exists(), "reject 后文件仍然被写出来了"


# ---------------------------------------------------------------------------
# 3. 排队：本轮结束后按序 drain
# ---------------------------------------------------------------------------
def test_queued_messages_are_drained_in_order(tmp_path):
    svc = _make_service()
    svc.enqueue(thread_id="queue-1", text="第二条")
    svc.enqueue(thread_id="queue-1", text="第三条")

    events = list(svc.stream(_req(thread_id="queue-1", message="第一条")))
    queued = [e for e in events if e["type"] == "queued_start"]
    assert [e["data"]["text"] for e in queued] == ["第二条", "第三条"], "排队顺序错误"

    history = svc.history(thread_id="queue-1", mode="chat", provider=PROVIDER,
                          user_id="e2e", role="admin")
    user_texts = [m["content"] for m in history if m["role"] == "user"]
    for text in ("第一条", "第二条", "第三条"):
        assert any(text in t for t in user_texts), f"排队消息 {text} 没有被执行"


# ---------------------------------------------------------------------------
# 4. 流水：Web 会话落 rollout
# ---------------------------------------------------------------------------
def test_web_stream_is_recorded_into_rollout(tmp_path):
    svc = _make_service()
    list(svc.stream(_req(thread_id="rollout-1", message="流水验证")))

    sessions = {s["thread_id"]: s for s in rollout.list_sessions(sources=("chat",))}
    assert "rollout-1" in sessions, "Web 会话没有落流水"
    records = rollout.load("rollout-1")
    assert any("流水验证" in r["content"] for r in records), "流水里没有用户消息"
    assert all(r["source"] == "chat" for r in records), "流水来源应为 chat"


# ---------------------------------------------------------------------------
# 5. 记忆召回：长期偏好写入 → 读回
# ---------------------------------------------------------------------------
def test_long_term_preferences_roundtrip(tmp_path):
    service = MemoryService()
    service.put_preference("e2e-user", "style", "结构化表达")
    items = service.preferences("e2e-user")
    match = [i for i in items if i["key"] == "style"]
    assert match and match[0]["value"] == "结构化表达"

    assert service.delete_preference("e2e-user", "style")
    assert not [i for i in service.preferences("e2e-user") if i["key"] == "style"]


def test_agent_uses_the_same_store_for_memory_recall(tmp_path):
    """Agent 装配出的 store 与 MemoryService 看到的是同一份长期记忆。"""
    svc = _make_service()
    app = svc.get(mode="chat", provider=PROVIDER, user_id="recall", role="admin",
                  thread_id="recall-1")
    mem.seed_long_term_memory(app.store, user_id="recall")
    keys = {it.key for it in app.store.search(("preferences", "recall"))}
    assert {"language", "tone", "domain"} <= keys
