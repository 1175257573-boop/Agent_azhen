# 更新日志

本项目的版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)，
格式沿用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增

- **Multi-Agent 防护模块 `agent_kit/guards.py`**：针对两类失控各给一套机制——
  · 方向跑偏（目标遗忘）→ 目标锚定 `make_goal_anchor()`（每步把原始目标重注入
    system prompt）+ 预算闸门 `make_budget_guard()`（步数 / 时间硬上限，超限带着
    已完成的进展停下，而不是只抛异常）；
  · 互斥循环（转移无收敛性）→ 跳数上限 + 环检测 `detect_pingpong()` + 转移白名单
    `guard_transition()` + 单调推进判定 `is_progress()`，所有防线统一收敛到
    `escalate` 节点，输出「进展 + 交接路径 + 下一步建议」。
- `handoffs.py` 新增 `ALLOWED_TRANSITIONS` / `safe_next_step()`，把「模型随意跳转」
  约束成状态机，非法转移留在当前步并累计违规，连续违规转收敛。
- 中间件栈新增 `enable_goal_anchor`（默认开，state 无目标时自动跳过，对现有模式无副作用）
  与 `budget`（默认关，多智能体长任务显式传入）两个参数。
- 离线演示 `examples/guards_demo.py` + CLI 子命令 `python main.py guards`：真实建一张
  LangGraph，两个代理互相甩锅，第 3 跳被拦下；CI 冒烟已加入该步骤。
- `tests/test_guards.py`：20 个用例钉住两类防护的判据，重点防「两种模式下
  `A→B→A` 语义相反」这类回归（用例总数 27 → 47）。

- **新增三个只读 MCP Server**（`agent_kit/mcp_servers/`，共 15 个工具）：
  · `quality.py` —— 工程质量体检：代码规模、TODO/FIXME 技术债、疑似密钥（只回打码片段）、
    测试现状、依赖是否钉版本；
  · `git_history.py` —— 版本控制**只读**（刻意不提供 commit/push）：状态、日志、
    变更统计、贡献者、提交搜索；
  · `doc_audit.py` —— 文档一致性：README 结构、目录锚点校验、CHANGELOG 是否有
    Unreleased、开源必备文件清单、目录树。
  三条硬约束：路径不越界（越界直接报错）、输出不含秘密原文、只用相对路径。
- **新增内置 Skill `project_engineering`**（`agent_kit/builtin_skills.py`）：
  八项工程化检查清单（分层/配置/错误/日志/测试/CI/文档/安全），每项指明用哪个
  MCP 工具取证，并固定「结论 → 阻断项 → 建议项 → 做得好的地方 → 未覆盖」输出格式；
  已接入 `skills` 模式。
- `MCPHub` 新增 `ALL_SERVERS` 注册表与 `connect_all()` / `connect_named()`，
  新增 server 只需改一行。
- 离线演示 `examples/mcp_servers_demo.py`：真实拉起 4 个 stdio 子进程，
  列出 21 个工具并跨进程调用；已加入 CI 冒烟。
- `tests/test_mcp_servers.py`：28 个用例（用例总数 47 → 75），重点钉住
  路径越界防护、密钥输出打码、不泄漏本机绝对路径。

- **排队消息（queued messages）**：Agent 忙碌时用户的输入先入队，本轮结束后由服务端
  自动按先进先出执行，行为对齐 WorkBuddy。核心在 `agent_kit/queue.py`：
  · 用 `deque + asyncio.Event` 而不是 `asyncio.Queue`——后者拿不到「待发列表」，
    而前端要展示「待发送 N 条」并支持撤回/编辑；
  · 按 `thread_id` 隔离，上限 20 条（超出 429，不让 Agent 一轮后连续自言自语）；
  · drain **复用同一条 SSE 连接**（`_run_one` → `_drain`），前端只需照常消费事件流；
  · **遇到人工确认（HITL）立即停止 drain**——中断未决时灌新消息会与中断状态冲突，
    剩余消息留到 `resume` 之后再发；
  · 同步 `stream` / `astream` / `resume` / `aresume` 四条链路全部支持。
- 新增 REST：`POST|GET|DELETE /api/chat/queue`（入队 / 列出 / 撤回 / 清空）。
- 前端：输入框上方显示「待发送 N 条」列表，支持撤回、取回修改、全部撤回；
  忙碌时发送按钮变为「排队」而非禁用；`queued_start` 事件把气泡从「待发送」转为正式消息。
- `tests/test_queue.py` 16 个用例（总数 75 → 91），钉住先进先出、会话隔离、
  队列上限，以及 HITL 时停止 drain 这条最容易回归的边界。

### 修正

- **MCP 技术选型说明改为引用官方依据**：原先写作「课程资料用的是 `MultiServerMCPClient`」，
  现改为引用官方迁移指南与发布博客，并保留本地实测到的依赖冲突作为佐证。
- 补充 `langchain.mcp` **官方标注为 beta** 的风险提示（导入会抛 `LangChainBetaWarning`，
  API 可能变化），此前完全没提。
- 修正 Elicitation 的表述：官方 1.4 已提供 `langchain.mcp.elicitation`
  （LangGraph `interrupt()` 驱动），原文档写的「适配器未暴露客户端回调」已过时。
- 拦截器对照表的来源改为 `langchain-mcp-adapters` 的官方 `ToolCallInterceptor` 协议。
- README 新增第 11 节「参考资料」：官方文档清单 + 本项目与官方做法不同的三处取舍。

### 文档

- 清理代码 docstring 中 9 处对外部资料的引用，改为中性的技术表述。
- 移除 README 第 5.1 节的本机环境细节（Windows 安装路径、服务启停脚本、组件版本号），
  统一改为跨平台的 `docker compose` 说明；`docker-compose.yml` 与 `.gitignore`
  中的本机遗留注释一并清理。
- 保留并中性化了「多数 Windows 构建不带 RedisJSON」这一条 —— 它是自写
  `PlainRedisSaver` 的技术依据，属于通用技术背景而非本机细节。

## [1.0.0] - 2026-09-16

首个对外公开版本。

### 新增

**Agent 能力**
- 8 种运行模式：`chat` / `structured` / `tools`（动态模型工具）/ `mcp` / `skills` / `handoffs` / `subagents` / `router`
- 任意模式可叠加 MCP 工具，不再局限于单独的 mcp 模式
- 完整中间件栈：日志、限流、工具重试、人工确认（HITL）、动态模型切换、上下文裁剪
- 多智能体：Skills 渐进式披露、Handoffs 委派、Subagents 子图隔离

**记忆分层**
- 短期记忆走 Redis：消息窗口 + TTL，进程重启不丢
- 长期记忆走 PostgreSQL：跨会话事实
- 两者不可用时自动降级内存，可配 `MEMORY_STRICT=1` 改成硬失败
- 自研 `PlainRedisSaver`：不依赖 RedisJSON 模块，纯 SET/HASH/LIST 实现

**服务与界面**
- FastAPI 后端：10 个 REST 接口，SSE 流式输出
- 原生 JS 前端：工具调用可视化、人工确认卡片、MCP 工具开关
- 统一异常处理：5xx 隐藏堆栈、请求带 `X-Request-ID`

**工程化**
- Java 风格分层：`main.py` → `routers` → `service` → `agent_kit`
- 27 个单元测试，覆盖双模钩子、参数注入、7 种模式装配
- GitHub Actions CI：Python 3.10 / 3.12 矩阵，ruff + pytest + 冒烟
- CLI 自查工具 `check_env.py`

### 修复

- MCP 工具同步调用报 `NotImplementedError` → 全链路改异步
- 自定义钩子只有 sync 版导致 `awrap_tool_call is not available` → 新增 `tool_hooks.py` 双模封装
- 零参数工具被注入参数触发 `unexpected_keyword_argument` → 按 JSON Schema 判断是否注入
- CLI 中两次 `asyncio.run()` 导致 `Event loop is closed` → 全程共用单个事件循环
- MCP stdio 跨 task 退出报 `Attempted to exit cancel scope in a different task` → 改用 `client.close()`

### 安全

- 移除本地开发用的硬编码凭据说明，README 与 docker-compose 均标注「仅限本地」
- `.env` 及密钥文件全部纳入 `.gitignore`
