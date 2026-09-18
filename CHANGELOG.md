# 更新日志

本项目的版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)，
格式沿用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 变更（架构调整：以 Codex CLI 为模板）

- **记忆管理默认改为 SQLite，并统一收进家目录 `ATLAS_HOME`**：
  · 新增 `agent_kit/home.py`：运行态目录约定（`~/.atlas`，可用 `ATLAS_HOME` 覆盖），
    内含 `atlas.db` / `sessions/` / `memories/` / `logs/`（对标 Codex 的 `CODEX_HOME`）；
  · `SHORT_TERM_BACKEND` / `LONG_TERM_BACKEND` 默认从「有 REDIS_URL / PG_DSN 才用」
    改为 **sqlite**。理由：跑一次 demo 就要先起两个服务，门槛过高；
    Redis / PostgreSQL 降级为可选后端，代码分支保留；
  · **发现并修正一个真 bug**：官方 langgraph 只提供 `memory / postgres / redis` 三种
    store，**没有 SQLite 版**，PyPI 上也不存在 `langgraph-store-sqlite` 这个包
    （旧代码里 `from langgraph.store.sqlite import SqliteStore` 是一条永远走不通的分支，
    会静默降级到内存）。现自带 `agent_kit/sqlite_store.py`，照 `BaseStore` 契约实现
    `batch` / `abatch`，支持 put / get / search / delete / list_namespaces 与 TTL。
- **新增 `atlas.toml` 配置层**（对标 Codex 的 `config.toml` + schema 校验）：
  环境变量 > 项目根 `atlas.toml` > `~/.atlas/atlas.toml` > 默认值。
  密钥仍只走环境变量——TOML 里出现 `api_key` / `token` / `secret` / `password`
  这类字段时整份作废并回落默认值；字段用 pydantic schema 约束，多写字段直接报错。
- **删除向量检索，改为可扩展接口**（`agent_kit/retrieval/` 包）：
  · 删除 `agent_kit/retrieval.py`（324 行）、`search_knowledge` /
    `rebuild_knowledge_index` 两个工具、`main.py rag` 子命令与 23 个相关测试；
    依赖去掉 numpy（除检索外无其他使用点）；
  · 新增 `base.py`（`Retriever` 协议 + `Hit`）、`registry.py`（注册 / 取用 / 列举，
    **未注册就抛错，不静默降级**）、`keyword.py`（内置字面检索 `keyword` / `phrase`）；
  · `search_notes` 改为走检索器注册表，换实现不用改工具层；
    新增 `main.py retrievers` 自检命令。
- 测试总数 107 → **134**（新增 `test_home.py`(5) / `test_config_toml.py`(5) /
  `test_retrieval_protocol.py`(8) / `test_sqlite_store.py`(9)）。

### 新增（Codex 模板第二批）

- **审批 / 沙箱策略 `agent_kit/policy.py`**（对标 Codex 的 `approval_policy` + `sandbox_mode`）：
  · 审批三档 `untrusted`（写前必问，默认）/ `on-failure`（失败才转人工）/ `never`；
    沙箱三档 `read-only` / `workspace-write`（默认）/ `danger-full-access`；
  · 优先级：CLI > 环境变量 `ATLAS_APPROVAL`/`ATLAS_SANDBOX` > atlas.toml > 默认值；
    旧的 `--role` / `--no-hitl` 仍作为最高优先级覆盖（`--no-hitl` = never）；
  · `on-failure` 档新增 `failure_escalation` 中间件：写工具失败后**不再让模型自动重试**，
    标记需人工复核；装配改为由策略派生 hitl / 写工具装载 / readonly。
  · ⚠️ 与 Codex 的差异如实标注：Codex 是 OS 级沙箱（seatbelt / landlock），
    本项目只是路径约束 + 中间件拦截。
- **会话流水 `agent_kit/rollout.py`**（对标 Codex 的 rollout）：
  · 每轮结束增量落 SQLite，**数据源是 checkpointer**（不另记一份，避免两处真相）；
  · `main.py sessions`：列会话 / 回放 / 导出 JSONL / 删除。
- **记忆产出 `agent_kit/memories.py`**（对标 Codex 的 memories，两阶段）：
  · Phase 1 per-thread 抽取（结构化输出 raw_memory / rollout_summary / slug）；
  · Phase 2 全局合并：按 `usage_count` → `last_usage` 取前 N 条（超 30 天未用淘汰），
    同步 `raw_memories.md` + `rollout_summaries/`，再合并出 `MEMORY.md`；
  · **红线：没有可用模型时明确跳过并说明原因，绝不编造记忆**（有测试兜底）；
  · 实测坑：模型会把整篇正文用 Markdown 代码围栏包起来，prompt 约束不住，落盘前再剥一层。
- 测试 144 → **159**（`test_policy.py` 10 / `test_rollout.py` 7 / `test_memories.py` 8）。

### 新增（Codex 模板第三批：并发保护 + git 基线）

- **Phase 1 加 lease/claim 并发保护**（`agent_kit/memories.py`）：新增 `memory_lease` 台账表
  （state / owner / lease_until / attempts / next_attempt_at / source_digest）。
  · 抢锁是 `BEGIN IMMEDIATE` 事务里的一条**条件 UPDATE**；lease 默认 120 秒、过期可被接手；
    失败按 10s → 60s → 停止退避，超过 3 次停在 `failed` 等人介入；
  · **内容指纹做 CAS**：`source_digest` 作为 UPDATE 条件之一，流水没变就不重复抽取。
    放在事务外「先查后抢」会漏出窗口（两个 worker 都查到内容变了，后者覆盖前者成果）；
  · 修正一个自己的错误认知：互斥来自单条 UPDATE 的原子性，**不是** `BEGIN IMMEDIATE`。
    对照实验 8 线程 × 20 轮，两种写法都是每轮恰好 1 个赢家、0 次 busy 错误。
    `BEGIN IMMEDIATE` 的实际作用是让「补台账行 + 改状态」成一个单元且先拿写锁再干活；
  · 修正一个真 bug：旧实现的 claim 条件里有 `state != 'done'`，导致**会话接着聊、流水变了也抽不了**。
- **Phase 2 加 git 基线 diff**（新增 `agent_kit/memory_git.py`，对标 Codex 的 workspace diff）：
  `~/.atlas/memories` 下维护一个本地 git 仓库，流程改为「基线快照 → 同步产物 → diff →
  有变更才调模型 → 再落快照」。产物相对基线无变化时**直接跳过 LLM 调用**；
  变更清单会拼进合并 prompt，让模型只消化增量。git 不可用时报在运行报告的 `说明` 行，不静默降级。
  · 提交身份用 `-c user.name/email` 注入而非依赖全局 config（CI / 容器里通常没配，
    否则 commit 直接失败）；`-c core.autocrlf=false` 避免 Windows 上行尾抖动。
- `memories` 命令新增 `--leases`（看台账）/ `--thread`（单会话）/ `--force`（强抽）/ `--no-git`。
- `tests/conftest.py` 补 `ATLAS_HOME` 隔离：Phase 2 会在家目录里 git init，
  漏传路径的用例会真的在开发机 `~/.atlas` 建仓库。
- 测试 159 → **183**（`test_memory_lease.py` 14 / `test_memory_git.py` 10）。
  含 8 线程抢同一把锁必须恰好一个赢家、多进程端到端（4 进程 5 会话零重复）两轮验证。

### 新增（Codex 模板第四批：Phase 1 后台编排 + startup claim）

取证依据：`codex-rs/memories/README.md`（官方仓库原文）。原先只做到了
「单条怎么抽 + lease 互斥」，缺的是 Codex 那一整套**受理规则与后台执行**。

- 新增 `agent_kit/memory_jobs.py`：
  · `Eligibility` —— 每次受理上限 5 条 / 7 天时间窗 / 会话需空闲 30 分钟 /
    只收交互会话 / 并发上限 3，全部可用环境变量覆盖（值非法则忽略该字段回落默认）；
  · `select_eligible()` 做粗筛，`run_pipeline()` 用线程池受限并行，`spawn()` 起守护线程
    （对标 Codex「会话启动后异步跑 Phase 1 再接 Phase 2」）；
  · `redact_secrets()` —— 记忆长期留存且会进 git 基线仓库，落盘前抹掉形状像密钥的片段。
    原则写在注释里：**宁可漏，不可把正常文本改坏**（有测试兜着）。
- `rollout` 表加 `source` 列 **并做幂等迁移**（老库 `CREATE TABLE IF NOT EXISTS` 是空操作，
  漏了 ALTER 会在 select 时抛 no such column）。`ChatSession(source=...)` 区分 REPL 与单次问答。
  排除 sub-agent / 工具会话：它们的流水是 agent 内部调度，不该被当成用户长期记忆。
- `memories.run_phase1()` 增加第三种结局（对标 Codex 的 `succeeded_no_output`）：
  模型说「本次无可记忆内容」时如实跳过，**不拿模板补一条**，也不算失败进退避。
- `memories` 命令新增 `--background` / `--wait` / `--no-bg-phase2`；CI 冒烟加了这条路径
  （在 Linux 上验证「没 Key 时应明确跳过而不是报错或编造」）。
- 测试 183 → **200**（`test_memory_jobs.py` 17）。

### 新增

- **Agent 效果评估 `agent_kit/evalset.py` + CLI `main.py eval`**：补上「单元测试证明不了
  Agent 干得好」的缺口。评估工具选择准确率、关键词命中率与用例通过率，
  支持 `--save` / `--compare` 做回归对比（通过率下降返回非零）。
  · **离线自检**（默认，进 CI）：用 `ScriptedChatModel` 编排确定性用例，
    其中**故意放两条必然失败的**，用来证明评估器会判失败；
  · **真机评估**（`--real`）：真实模型 + 人工标注用例，产出真实指标。
  · 坑：没配 Key 时 provider 会退化成 fake，`--real` 会静默产出假报告
    → 显式检查 provider 并 fail fast，提示该配哪个环境变量。
- `tests/test_evalset.py`：16 个用例（总数 114 → 130），含一条
  「自检集必须同时含通过与失败用例」的反向验证。

- **MCP 规范笔记 + 接入流程 Skill**：
  · `notes/mcp-protocol.md` —— 官方规范 `2026-07-28` 版要点。**重点记录协议变化**：
    新版取消 `initialize` 握手，改为每请求 `_meta` 携带版本/身份/能力，
    新增必实现方法 `server/discover`；服务端不再主动发 JSON-RPC 请求，
    改用 `resultType: "input_required"` + `requestState` 让客户端补齐后重发（id 必须不同）；
    版本错误码 `-32022`；附官方 7 种 Modern/Legacy 兼容组合矩阵。
  · `notes/mcp-integration.md` —— 接入社区 MCP 的实操清单与本项目踩坑。
  · `skills/mcp-integration/` —— 固化成 Skill 包，含只读审查脚本 `audit_server.py`，
    检查标准库遮蔽 / sys.path 引导 / 工具可测性 / 路径越界 / 写操作 / stdout 污染 /
    硬编码凭据七项。自建四个 server 审查全 PASS，故意写坏的样例能正确报 FAIL。
- 新增 `notes/forgetting-curve.md`，补齐知识库在通识类问题上的空白。


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
  自动按先进先出执行，行为对齐 WorkBuddy。核心在 `agent_kit/message_queue.py`：
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

- **CI 静态检查钉住 ruff 版本**：`ruff>=0.6` 改成 `ruff>=0.16,<0.17`（CI 与 `pyproject.toml`
  的 dev extras 同步）。不同 ruff 版本的默认规则集不同——实测 `0.6.9` 会在
  `server/app.py` 等 5 个既有文件上多报 12 条 `E402`，松散约束会让 CI 结果随上游发布而变。
- **审查脚本补可执行位**：`skills/mcp-integration/scripts/audit_server.py` 带 shebang
  但 git mode 是 `100644`，Linux 上触发 `EXE001`。**Windows 没有 POSIX 权限位，本地 ruff
  判不出来，只有 CI 会暴露** → `git update-index --chmod=+x`。
- CI 的 ruff 步骤改用 `--output-format=github`：违规直接落成注解。
  公开仓库的 job logs 需要 admin 权限（403），注解则是公开可读的，排查时能省很多事。
- MCP 工具同步调用报 `NotImplementedError` → 全链路改异步
- 自定义钩子只有 sync 版导致 `awrap_tool_call is not available` → 新增 `tool_hooks.py` 双模封装
- 零参数工具被注入参数触发 `unexpected_keyword_argument` → 按 JSON Schema 判断是否注入
- CLI 中两次 `asyncio.run()` 导致 `Event loop is closed` → 全程共用单个事件循环
- MCP stdio 跨 task 退出报 `Attempted to exit cancel scope in a different task` → 改用 `client.close()`

### 安全

- 移除本地开发用的硬编码凭据说明，README 与 docker-compose 均标注「仅限本地」
- `.env` 及密钥文件全部纳入 `.gitignore`
