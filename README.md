# Atlas · LangChain 1.4 Agent

![CI](https://github.com/1175257573-boop/Agent_azhen/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![LangChain](https://img.shields.io/badge/langchain-1.4.0-green)
![License](https://img.shields.io/badge/license-MIT-orange)

一个**真实可跑**的 Agent 软件（不是「零 API Key 演示」）：由真实大模型驱动，
覆盖 LangChain 1.x 的 Agent、中间件、MCP、多智能体四大块能力。

**没有配置任何模型的 API Key 时，程序会明确报错退出，不会静默降级成假模型。**

## ✨ 30 秒了解这个项目

- **记忆分层**：短期走 Redis（消息窗口 + TTL），长期走 PostgreSQL（跨会话事实），
  两者都不可用时优雅降级，不会崩。
- **8 种运行模式**：对话 / 结构化输出 / 动态模型工具 / MCP / Skills / Handoffs / Subagents / Router，
  一套代码覆盖 LangChain 1.x 的主要范式。
- **任意模式可叠加 MCP**：不只是单独的 mcp 模式，chat 等模式也能并入 MCP 工具。
- **Java 风格分层**：`main.py`(Application) → `server/routers`(Controller) →
  `server/service`(Service) → `agent_kit`(Domain)，后端同学零门槛。
- **自带 Web 界面**：FastAPI + 原生 JS，SSE 流式、工具调用可视化、人工确认卡片。

## 🚀 快速开始

```bash
# 1. 装依赖
python -m venv .venv && .venv/Scripts/activate      # Windows
# source .venv/bin/activate                          # Linux / macOS
pip install -r requirements.txt

# 2. 配一个模型的 Key（以阿里云百炼为例）
setx DASHSCOPE_API_KEY "sk-xxxxx"                    # Windows
# export DASHSCOPE_API_KEY="sk-xxxxx"                # Linux / macOS

# 3. 挑一种方式跑起来
python main.py check --ping     # 先自查：Key 与记忆后端是否连通
python main.py demo             # 离线演示 8 个场景（不需要 Key）
python main.py chat             # 终端交互
python main.py web              # Web 界面 → http://127.0.0.1:8000
```

只要第 3 步的 `web` 能打开页面并发一句话，就说明整套链路通了。
想接 Redis / PostgreSQL 见 [第 5.1 节](#sec5-1)。

## 🏗 架构一览

```mermaid
flowchart TB
    subgraph 入口
      CLI["main.py<br/>chat / demo / check / web"]
      WEB["FastAPI<br/>server/"]
      UI["ui.py<br/>终端 REPL"]
    end

    subgraph Service
      SVC["AgentService<br/>按(mode,provider,user)缓存"]
      MEM["MemoryService"]
    end

    subgraph Domain["agent_kit"]
      AGENT["create_agent<br/>LangGraph 图"]
      MW["中间件栈<br/>限流/重试/脱敏/HITL/窗口"]
      TOOLS["本地工具 + MCP 工具"]
    end

    subgraph 记忆
      REDIS[("Redis<br/>短期消息窗口")]
      PG[("PostgreSQL<br/>长期事实")]
    end

    CLI --> SVC
    UI --> SVC
    WEB --> SVC
    WEB --> MEM
    SVC --> AGENT
    SVC --> MEM
    AGENT --- MW
    AGENT --- TOOLS
    AGENT -->|checkpointer| REDIS
    AGENT -->|store| PG
    MEM --> PG
    TOOLS -.->|MCP stdio| MCPSRV["本地 MCP Server<br/>mcp_server.py"]
```

> 想深入理解每一层，看下方目录。

## 📑 目录

| 章节 | 内容 |
| --- | --- |
| [1. 能力对照](#sec1) | 一张表看清覆盖 LangChain 1.x 哪些能力 |
| [2. 目录结构](#sec2) | 对标 Java 分层的目录说明 |
| [3. 配置](#sec3) | 密钥怎么配、从哪读 |
| [4. 启动](#sec4) | CLI / Web / 会话内命令 |
| [5. 八种能力模式](#sec5) | chat → router 逐个说明 |
| [5.1 记忆层](#sec5-1) | Redis 短期 + PostgreSQL 长期 |
| [5.2 Multi-Agent 防护](#sec5-2) | 跑偏拦截 + 循环拦截 |
| [5.3 Skill 与 MCP](#sec5-3) | 方法论 vs 能力，三层渐进式披露 |
| [5.4 排队消息](#sec5-4) | Agent 忙碌时的输入缓存与自动发出 |
| [6. MCP](#sec6) | 技术选型、叠加用法、为什么必须异步 |
| [7. 关键认知](#sec7) | 实测踩过的坑 |
| [7.1 测试与 CI](#sec7-1) | 27 个用例 + GitHub Actions |
| [8. 安全提醒](#sec8) | **公开部署前必读** |
| [9. 参与贡献](#sec9) | 开发流程与三条铁律 |
| [10. 许可证](#sec10) | MIT |
| [11. 参考资料](#sec11) | 官方文档与本项目的技术取舍 |

**配套文档**：[`.env.example`](.env.example)（配置项全清单） ·
[`CONTRIBUTING.md`](CONTRIBUTING.md)（开发规范） ·
[`SECURITY.md`](SECURITY.md)（安全策略与漏洞上报） ·
[`CHANGELOG.md`](CHANGELOG.md)（版本历史）

---

<a id="sec1"></a>
## 1. 能力对照

| 能力块 | 本项目实现 | 位置 |
|---|---|---|
| `create_agent` 标准入口 | 基于 LangGraph 的 Agent 循环 | `agent_kit/agent.py` |
| 静态 / 动态模型 | 按对话长度与 state 标记切换轻/重模型 | `agent_kit/models.py` |
| 静态 / 动态工具 | 按权限过滤可见工具；运行时注册新工具；工具注册表 | `agent_kit/dynamic_tools.py` |
| 工具错误处理 | 异常转 `ToolMessage` 让模型自恢复 + 重试中间件 | `agent_kit/tools.py`、`middleware.py` |
| 静态 / 动态提示词 | 静态基座 + `@dynamic_prompt` 注入运行时状态 | `agent_kit/prompts.py` |
| 自定义 state | TypedDict 继承 `AgentState` | `agent_kit/state.py` |
| 结构化输出 | `ToolStrategy` + Pydantic | `agent_kit/schemas.py` |
| 人机协同 HITL | `approve` / `edit` / `reject` / `respond` 四决策 | `agent_kit/middleware.py`、`ui.py` |
| 流式四模式 | updates / messages / custom / 组合，统一 `version="v2"` | `agent_kit/streaming.py` |
| 中间件栈 | 限流、重试、异常兜底、压缩、脱敏、审计 | `agent_kit/middleware.py` |
| MCP | 自建 Server（工具/资源/进度/日志/Elicitation）+ Client + 拦截器 | `agent_kit/mcp_server.py`、`mcp_client.py` |
| Subagents | 子代理包装成工具；tool-per-agent 与 single-dispatch 两种模式 | `agent_kit/multi_agent/subagents.py` |
| Handoffs | 状态机式交接（Command 改 current_step） | `agent_kit/multi_agent/handoffs.py` |
| Skills | 渐进式披露：先给目录，按需 `load_skill` | `agent_kit/multi_agent/skills.py` |
| Router | LangGraph 图分发，`Send` 并行多源召回后汇总 | `agent_kit/multi_agent/router.py` |
| Custom workflow | 把 Agent 嵌进手写图 | `agent_kit/multi_agent/router.py` |

<a id="sec2"></a>
## 2. 目录结构（对标 Java 分层）

```
langchain-v1.4-demo/
├── main.py                   ≈ Application 启动类（唯一入口）
├── ui.py                     ≈ 控制台 Controller（REPL）
├── run.ps1                   ≈ Windows 启动脚本
├── pyproject.toml            ≈ pom.xml
├── requirements.txt          ≈ pom.xml 轻量替代
├── check_env.py              ≈ 健康检查脚本
├── examples/scenarios.py     ≈ 集成示例（不进生产包）
├── notes/                    ≈ src/main/resources（含 MCP 规范 / 接入笔记）
├── skills/                   ≈ 可复用的 Skill 包（mcp-integration）
├── runs/                     ≈ 运行时输出
└── agent_kit/                ≈ com.xxx 业务包
    ├── config.py               ≈ @Configuration（密钥从环境变量读）
    ├── logging_conf.py         ≈ logback.xml
    ├── schemas.py              ≈ DTO / Entity
    ├── state.py                ≈ 扩展会话上下文
    ├── prompts.py              ≈ 文案资源
    ├── tools.py                ≈ Service
    ├── middleware.py           ≈ Interceptor / Filter / AOP
    ├── memory.py               ≈ Mapper / Repository
    ├── models.py               ≈ 模型路由策略
    ├── dynamic_tools.py        ≈ 运行时工具可见性
    ├── streaming.py            ≈ 流式输出封装
    ├── mcp_client.py           ≈ MCP 客户端 + 拦截器
    ├── mcp_server.py           ≈ MCP 服务端（独立进程）
    ├── app.py                  ≈ 按模式装配 Bean（工厂）
    └── multi_agent/            ≈ 多智能体
        ├── subagents.py  ├── handoffs.py
        ├── skills.py     └── router.py
```

| Java | Python |
|---|---|
| Application | `main.py` + `if __name__ == "__main__"` |
| Controller | `ui.py`（REPL）+ `main.py` 子命令 |
| Service | `agent_kit/tools.py` |
| Mapper / Repository | `agent_kit/memory.py` |
| Controller（Web） | `server/routers/*.py` |
| Service（Web） | `server/service/*.py` |
| Web 启动类 | `server/app.py` |
| 前端页面 | `static/index.html` + `app.js` + `style.css` |
| Interceptor / Filter | `agent_kit/middleware.py` |
| DTO / Entity | `agent_kit/schemas.py`、`state.py` |
| `@Configuration` | `agent_kit/config.py` |
| Bean 工厂 | `agent_kit/app.py` |
| pom.xml | `pyproject.toml` / `requirements.txt` |

<a id="sec3"></a>
## 3. 配置：密钥只从系统环境变量读取

| provider | 必填环境变量 | 默认模型 |
|---|---|---|
| `dashscope` | `DASHSCOPE_API_KEY` | `qwen-plus` |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5` |
| `fake` | 无需（仅离线演示用） | scripted |

可选：`LLM_PROVIDER`、`LLM_MODEL`、`*_BASE_URL`（覆盖 endpoint）。

```powershell
# 持久化（新开终端生效）
setx DASHSCOPE_API_KEY "你的密钥"
setx LLM_PROVIDER dashscope

# 仅当前终端
$env:DASHSCOPE_API_KEY = "你的密钥"
```

自查：`python main.py check --ping`（会真实调用一次，验证 Key 与网络）。

**不想写系统变量**也可以在项目根目录放 `.env`（已在 `.gitignore`，不会入库）：

```bash
cp .env.example .env      # Linux / macOS
copy .env.example .env    # Windows
```

所有可用配置项（含 Redis / PostgreSQL / 记忆窗口 / 追踪）都列在
[`.env.example`](.env.example) 里，带中文注释。优先级：**系统环境变量 > `.env`**。

<a id="sec4"></a>
## 4. 启动

```powershell
.venv\Scripts\activate          # Windows；Linux / macOS 用 source .venv/bin/activate
pip install -r requirements.txt

python main.py chat                      # 交互式对话（默认模式）
python main.py chat --mode skills        # 指定能力模式
python main.py chat --ask "现在几点"      # 单次问答，答完即退
python main.py check                     # 环境变量自查
python main.py info                      # 环境概况
python main.py demo                      # 离线能力演示（8 个场景）
python main.py demo --real-memory        # 演示时改用真实的 Redis / PostgreSQL
```

> **演示默认不碰生产存储**：`main.py demo` 会把记忆层切到进程内内存，
> 否则每跑一次就往生产 Redis 里塞 `s1`~`s8` 这类一次性会话，污染 Web 端的会话列表。
> 想验证真实记忆层请加 `--real-memory`，或直接跑 `examples/memory_e2e.py`。

### Web 模式（FastAPI + 前端页面）

```powershell
python main.py web                 # 默认 http://127.0.0.1:8000
python main.py web --port 9000 --reload
uvicorn server.app:app --reload    # 等价写法
```

打开 <http://127.0.0.1:8000> 即可用，接口文档在 `/docs`。

| 能力 | 位置 |
|---|---|
| 流式打字机（SSE） | 中栏，`/api/chat/stream` |
| 工具调用过程可视化 | 消息气泡下的 chip |
| 人工确认（HITL） | 触发写文件时弹出审批卡，`/api/chat/resume` |
| 会话列表 / 切换 / 删除 | 左栏，读 Redis |
| 8 种能力模式切换 | 左栏下拉 |
| 记忆层状态 | 右栏，实时显示 Redis / PG 后端 |
| 长期偏好增删改 | 右栏，直接写 PostgreSQL |

等价写法：`python -m agent_kit chat`、`.\\run.ps1 chat`、`agent-demo chat`（`pip install -e .` 后）。

**PyCharm**：Script=`main.py`，Parameters=`chat`，Working dir=项目根，Interpreter=`.venv`。

### 会话内命令

```
/help              帮助
/mode <名称>       切换模式：chat|structured|dynamic|mcp|skills|handoffs|subagents|router
/tools             列出当前工具
/thread <id>       切换/续接会话
/stream            切换流式输出
/reset             开新会话
/info              打印当前配置
/mem               打印记忆层状态（短期/长期后端、消息窗口）
/exit              退出
```

写文件等敏感操作会暂停等你决策：`a`=同意 / `e`=改写参数 / `r`=拒绝 / `s`=你来回答。

<a id="sec5"></a>
## 5. 八种能力模式

| 模式 | 说明 |
|---|---|
| `chat` | 基础对话 + 工具 + 中间件 + 短期/长期记忆 |
| `structured` | 强制输出 Pydantic 结构的调研报告 |
| `dynamic` | 动态模型 + 动态工具 + 自定义 state |
| `mcp` | 接入 MCP 服务端（含进度通知、日志、引导式输入） |
| `skills` | 技能渐进式披露，按需加载 |
| `handoffs` | 客服向导式流程交接 |
| `subagents` | 主代理调度多个专项子代理 |
| `router` | 多源知识库路由，可并行召回后汇总 |

<a id="sec5-1"></a>
## 5.1 记忆层：Redis（短期）+ PostgreSQL（长期）

两层记忆解决的问题不同，用的是两套存储：

| | 短期记忆 | 长期记忆 |
|---|---|---|
| 回答什么 | 这轮聊到哪了 | 这个用户是谁 |
| 存储 | **Redis**（`RedisSaver`） | **PostgreSQL**（`PostgresStore`） |
| 主键 | `thread_id` | `(namespace, user_id)` → `key` |
| 内容 | 完整 messages 历史 | 跨会话事实 / 偏好 |
| 过期 | TTL，默认 7 天（`REDIS_TTL_MINUTES`） | 不过期 |
| 降级 | `InMemorySaver` | `InMemoryStore` |

**消息窗口**：短期记忆在 Redis 里存全量（可回溯），但只把最近 N 条喂给模型，
由 `memory.make_message_window()` 这个 `@before_model` 中间件实现。
它用 `trim_messages` 而不是 `list[-N:]`，原因是后者会切出「孤儿 ToolMessage」
（AI 消息带 tool_calls 但对应的 ToolMessage 被截掉），调用 API 会直接报错。

> 消息窗口与 `SummarizationMiddleware` 是解决同一个问题的两种策略，**二者互斥，窗口优先**。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHORT_TERM_BACKEND` | 配了 `REDIS_URL` 就 `redis` | `memory` / `sqlite` / `redis` |
| `REDIS_URL` | — | `redis://localhost:6379/0` |
| `REDIS_TTL_MINUTES` | `10080`（7 天） | checkpoint 过期时间 |
| `MEMORY_WINDOW` | `20` | 进入上下文的最近消息条数 |
| `LONG_TERM_BACKEND` | 配了 `PG_DSN` 就 `postgres` | `memory` / `sqlite` / `postgres` |
| `PG_DSN` | — | 也认 `POSTGRES_URL` / `DATABASE_URL` |
| `PG_CONNECT_TIMEOUT` | `5` | 秒；不加 PG 连不通时会卡死而不是报错 |
| `MEMORY_STRICT` | `0` | `1` = 后端连不上直接失败；`0` = 告警并降级内存 |

### 起服务

推荐直接用容器，`docker-compose.yml` 里已经配好了两个服务：

```bash
docker compose up -d      # 首次会拉镜像，约 1-2 分钟
docker compose ps         # 两个都 healthy 再启动 Agent
docker compose down -v    # 连数据一起清掉
```

起好之后配两个环境变量即可，也可以写进 `.env`（见第 3 节）：

```bash
export REDIS_URL="redis://localhost:6379/0"
export PG_DSN="postgresql://atlas:atlas@localhost:5432/atlas"
```

> ⚠️ 上面这組 `atlas/atlas` 是本地开发默认凭据，仅限 127.0.0.1 调试，
> 换环境请务必改掉（见第 8 节）。

**不想起服务也能跑**：没配 DSN 时自动降级为内存模式，只是进程重启后记忆丢失。

> **RedisJSON 说明**：官方 `langgraph-checkpoint-redis` 依赖 RedisJSON 模块（`JSON.SET`），
> 而多数 Windows 构建与部分云 Redis 基础版不带它。本项目会自动探测：
> 有 RedisJSON 就用官方实现，没有则切到自带的 `agent_kit/redis_checkpointer.py`
> （只用原生 STRING/HASH/LIST 命令，免模块）。

连不上时的排查顺序：`python main.py check --ping` → 看 `[记忆后端连通性]` 一节。

<a id="sec5-2"></a>
## 5.2 Multi-Agent 防护：跑偏与循环

多智能体一旦上线，最常见的两类事故是**方向跑偏**和**互斥循环**。它们的根因不同，
防护手段也不同，**不能用同一套机制硬套**：

| | 方向跑偏（off-rail） | 互斥循环（ping-pong） |
|---|---|---|
| 本质 | 目标遗忘：多轮后原始任务被挤到上下文很远处 | 转移无收敛性：状态没有单调推进 |
| 表现 | 模型把「手段」当成「目标」，越跑越远 | A 认为该 B 做、B 认为该 A 做，来回踢皮球 |
| 对策 | 目标锚定（每步重注入原始目标）+ 预算闸门 | 跳数上限 + 环检测 + 转移白名单 + 单调推进判定 |
| 落地 | `make_goal_anchor()` / `make_budget_guard()` | `detect_pingpong()` / `guard_transition()` / `route_after_handoff()` |

全部实现在 `agent_kit/guards.py`，可直接 `python main.py guards` 离线看效果：
一个真实的 LangGraph 里让两个代理互相甩锅，第 3 跳被拦下，收敛节点交出已完成部分与交接路径。

**最容易踩的坑：`A→B→A` 在两种模式下语义完全相反**

- Handoffs：控制权交接，`A→B→A` 是踢皮球，**必须拦**
- Subagents：主代理调子代理后收回结果，`A→B→A` **完全正常**

所以 `detect_pingpong(path, mode=...)` 必须按模式分别判定——用同一套判据，
要么误杀正常的回调，要么放过死循环。另外环检测早期写法 `path[-1] == path[-3]`
会漏判 `A→B→C→A` 四步环，已改为按出现次数判定。

**停止不等于报错**。所有防线的终点都是同一个收敛节点 `escalate`，它输出三件事：
已完成的部分进展、完整交接路径、下一步建议。只抛异常的话，前面消耗的算力全浪费了，
读者也无从排查。

<a id="sec5-3"></a>
## 5.3 Skill 与 MCP：方法论 vs 能力

这两件事经常被混为一谈，其实职责完全不同：

| | MCP | Skill |
|---|---|---|
| 回答什么 | **能做什么** | **这件事该按什么步骤做** |
| 形态 | 独立进程，通过协议调用 | 一段提示词，活在系统提示里 |
| 跨什么复用 | 跨项目、跨语言 | 跨轮次 |
| 成本 | 每次调用有进程/网络开销 | L1 常驻仅几十 token，L2 按需加载 |
| 判断标准 | 要跨进程（需独立部署/隔离/多语言）→ MCP | 只是做事的方法论 → Skill |

**Skill 是三层渐进式披露**（`agent_kit/multi_agent/skills.py`）：

```
L1  名称 + 一句话        → 常驻系统提示，几十个技能也才几百 token
L2  完整 SOP            → 模型觉得需要时调 load_skill(name) 才加载
L3  按 SOP 去调真正的工具 → 落到 MCP 工具或本地工具
```

内置技能 `project_engineering`（`agent_kit/builtin_skills.py`）：按工程化规范审查项目，
八项检查清单（分层 / 配置 / 错误 / 日志 / 测试 / CI / 文档 / 安全）**每一项都指明用哪个
MCP 工具取证**——没有取证就下结论，是工程评审里最典型的失真。

```
python main.py chat --mode skills      # 交互使用
```

写新 Skill 的三条经验：

1. **description 写给模型看**，要能触发正确的调用时机；描述含糊，模型永远不会加载它。
2. **content 里必须写「取证工具」和「输出格式」**，否则模型拿到 SOP 仍然会自由发挥。
3. **SOP 要写「未覆盖」一栏**——不说清自己没查什么，读者会误以为是全量检查。

<a id="sec5-4"></a>
## 5.4 排队消息：Agent 忙碌时的输入不丢也不打断

一轮 Agent 要跑几十秒，这期间用户输入的第二条、第三条指令怎么办？两个错误答案：
直接丢弃（用户得重打一遍）、立刻并发发给 Agent（打断当前这轮的上下文）。
本项目采用与 WorkBuddy 一致的**排队**方案：`agent_kit/message_queue.py`。

```
用户在 Agent 忙碌时输入  →  入队（服务端为真相源，按 thread_id 隔离）
                        ↓
                   前端显示「待发送 N 条」，可撤回 / 取回修改
                        ↓
              当前轮 done → 服务端自动按先进先出执行（复用同一条 SSE 连接）
                        ↓
              事件流给出 queued_start → 前端把该气泡从「待发送」转为正式消息
```

| 接口 | 作用 |
|---|---|
| `POST /api/chat/queue` | 入队（带 `item_id` 则是编辑已有排队项）；队列满返回 429 |
| `GET /api/chat/queue?thread_id=` | 列出待发消息 |
| `DELETE /api/chat/queue/{id}` | 撤回一条 |
| `DELETE /api/chat/queue?thread_id=` | 清空 |

**四个设计决定**

1. **服务端是真相源**，前端只做展示。刷新页面后排队内容还在，不会因为前端状态丢失而"吞掉"用户输入。
2. **drain 复用同一条 SSE 连接**（`_run_one` 跑完 → `_drain` 继续取下一条），
   而不是让前端轮询或重开请求：少一次连接，且前端只需像往常一样消费事件流。
3. **遇到人工确认（HITL）立刻停止 drain**。中断悬而未决时灌新消息会和中断状态打架，
   剩下的排队消息留到用户确认完（`resume`）再发——测试 `test_drain_stops_at_human_interrupt` 钉住这条。
4. **队列上限 20 条**，超出返回 429 让前端明确提示。没有上限时用户狂敲回车会让
   Agent 一轮结束后连续自言自语几十轮。

> **CLI 为什么不做**：终端里 agent 输出期间敲的内容由 TTY 行缓冲，
> 下一个 `input()` 会立刻读到，效果上已经接近排队；真做成可见队列需要后台线程读
> stdin，会和主线程的 `input()` 争抢、且有跨平台差异，收益不抵风险。

<a id="sec6"></a>
## 6. 技术决策：MCP 走哪套 API

LangChain 的 MCP 集成在 **1.4.0 换过一代**，选型依据来自官方文档，并用本地实测校准：

| 方案 | 出处 | 结论 |
|---|---|---|
| `langchain-mcp-adapters` 的 `MultiServerMCPClient` | 1.4 之前的官方方案 | ❌ 不用 |
| `langchain.mcp.MCPAdapter` | LangChain 1.4 官方内置，底层 FastMCP | ✅ 采用 |

**官方方向**：从 1.4.0 起 MCP 支持移入主包 `langchain.mcp`，`MultiServerMCPClient`
折叠为单个 `MCPAdapter`，传输/鉴权/协议协商交给 FastMCP
（见官方 [迁移指南](https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters) 与
[发布博客](https://www.langchain.com/blog/mcp-in-langchain-stateless-protocol-elicitation-and-more)）。

**本地实测**还撞到一个硬性冲突：独立包会把 `mcp` 依赖降到 1.x，而 fastmcp 4.x 的
server 端需要 `mcp.server.request_state`（2.x 才有），装上之后本地 MCP 服务起不来。
**官方推荐与本地实测指向同一个结论**，所以没有纠结的余地。

> ⚠️ 但要注意：`langchain.mcp` 官方标注为 **beta**，导入时会抛
> `LangChainBetaWarning: langchain.mcp is in beta... the API may change`。
> 本项目已实测连通 6 个工具，但升级 LangChain 时请优先回归 MCP 相关用例。

### 6.1 拦截器能力怎么平移

旧包的 `tool_interceptors`（`ToolCallInterceptor` 协议：handler 回调 + 洋葱式组合）
在 `langchain.mcp` 里**没有直接对应物**。本项目用 LangChain 原生中间件等价实现：

| `langchain-mcp-adapters` 的能力 | 本项目的等价实现 |
|---|---|
| 访问运行时上下文（`request.runtime`） | `@wrap_tool_call` + `request.runtime.{state,context,store}` |
| `request.override(args=...)` 改写参数 | `request.override(tool_call={...})` |
| 短路不执行（缓存 / mock） | 直接 `return ToolMessage(...)` |
| 返回 `Command` 改状态 | 同样返回 `Command`（跳转 / 改状态） |
| 多拦截器洋葱式组合 | 中间件栈按注册顺序组合 |

**一个值得改进的点**：官方 `MCPAdapter` 会给工具打上 `mcp` namespace 元数据，
其中 `annotations.destructive_hint` / `read_only_hint` 是服务端声明的危险性提示。
目前本项目用 `make_guard(denied=[...])` 硬编码黑名单，
后续可以改成读 `tool.metadata["mcp"]["tool"]["annotations"]` 自动识别危险工具。

### 6.2 MCP 协议层能力（进度 / 日志 / Elicitation）

进度通知、日志、Elicitation 属于 **MCP 协议层**能力，本项目在服务端用 fastmcp
`Context` 实现（`mcp_server.py` 的 `ctx.report_progress` / `ctx.info` / `ctx.elicit`）。

客户端侧：LangChain 1.4 官方已提供 `langchain.mcp.elicitation`
（以 LangGraph `interrupt()` 驱动人工回答），本项目**尚未接线该 interrupt 循环**，
因此这几项目前以「服务端能力 + 文档说明」呈现。

### 6.3 在 chat 模式上叠加 MCP（而不是切到 mcp 模式）

MCP 不再只能单独用 `--mode mcp`：**任意模式都可以叠加 MCP 工具**，
本地工具与 MCP 工具一起交给模型挑。

```bash
# CLI：chat 模式 + MCP
python main.py chat --mcp --ask "用 current_utc 查一下现在的 UTC 时间"

# REPL 里随时开关
/mcp            # 切换
/mcp on         # 显式打开
/mcp off        # 关闭
/tools          # 查看当前可用工具（含 MCP 的 6 个）
```

Web 端：左侧「MCP 工具」勾选框，勾上即连接，下方会列出 MCP Server 提供的工具名。
对应接口 `GET /api/chat/mcp-tools?connect=true|false`，
对话请求体多一个 `enable_mcp: true`。

叠加模式下工具集合 = `本地全量工具 + MCP 工具`（并开启写工具与人工确认）；
纯 `mcp` 模式则只给 `SAFE_TOOLS + MCP 工具`。

### 6.4 MCP 为什么必须走异步

MCP 工具**只实现了 `ainvoke`**，同步调用会抛
`NotImplementedError: StructuredTool does not support sync invocation`。
所以一旦启用 MCP，整条链路都得是异步的（`astream` / `ainvoke`），
本项目为此做了三处配套改造：

| 位置 | 改造 |
|---|---|
| `agent_kit/redis_checkpointer.py` | 补 `aget_tuple / aput / aput_writes / alist / adelete_thread`，否则 `astream` 一上来就 `NotImplementedError` |
| `agent_kit/tool_hooks.py` | 自定义工具/模型钩子必须**成对**提供同步与异步实现（`dual` / `dual_model`） |
| `ui.py` | MCP 会话全程复用**同一个事件循环**，装配与执行不能各起一个 `asyncio.run()` |

### 6.5 自定义钩子必须「同步 + 异步成对提供」

`@wrap_tool_call` 装饰 `def` → 只有 `wrap_tool_call`；装饰 `async def` → 只有 `awrap_tool_call`。
而 `agent.stream()` 走前者、`agent.astream()` 走后者，缺一个就直接报错：

```
NotImplementedError: Asynchronous implementation of awrap_tool_call is not available.
```

本项目的做法是 `agent_kit/tool_hooks.py` 里的 `dual(sync_fn, async_fn)` /
`dual_model(sync_fn, async_fn)`，一对函数打包成一个中间件实例，两种链路都能跑。

<a id="sec6-6"></a>
### 6.6 本项目自带的四个 MCP Server（共 21 个工具）

| Server | 工具 | 干什么 |
|---|---|---|
| `notes`（`mcp_server.py`） | 6 | 知识库：词频、笔记列表、进度通知、日志、Elicitation |
| `quality`（`mcp_servers/quality.py`） | 5 | 工程质量体检：代码规模、技术债标记、疑似密钥、测试现状、依赖审计 |
| `git`（`mcp_servers/git_history.py`） | 5 | **只读**版本控制：状态、日志、变更统计、贡献者、提交搜索 |
| `docs`（`mcp_servers/doc_audit.py`） | 5 | 文档一致性：README 结构、锚点校验、CHANGELOG、必备文件、目录树 |

```bash
python examples/mcp_servers_demo.py    # 真实拉起 4 个 stdio 子进程并调用，零 API Key
```

新增 server 只要往 `agent_kit/mcp_client.py` 的 `ALL_SERVERS` 里加一行，Agent 侧不用改代码。

**三条硬约束**（新增 server 必须遵守）：

1. **路径不越界** —— 入参路径必须落在项目根内，见 `_common.resolve_dir()`。
   只读工具一旦能读任意路径，就变成了任意文件读取口子。
2. **输出不含秘密原文** —— 密钥只回打码片段；`localhost` 默认凭据单独降级计数，
   不算泄密但仍提示「换环境必须改」。
3. **输出用相对路径** —— 绝对路径会把本机目录结构泄漏给模型与日志。

> **踩坑**：MCP 客户端是**当脚本启动** server 的（`python .../quality.py`），
> 此时 `sys.path[0]` 是脚本所在目录、项目根不在其中，`import agent_kit...` 直接失败。
> 而失败发生在 stdio 握手之前，客户端只会看到 `Connection closed`，极难定位。
> 所以每个 server 顶部都有一段显式补 `sys.path` 的引导。

<a id="sec6-7"></a>
### 6.7 MCP 规范笔记与接入流程

`notes/` 下新增两篇笔记（同时作为 Agent 知识库，可被 `search_notes` 检索）：

| 笔记 | 内容 |
|---|---|
| `notes/mcp-protocol.md` | 官方规范 `2026-07-28` 版要点：Modern / Legacy 双时代、传输绑定、能力协商、工具报文、两类错误、兼容矩阵 |
| `notes/mcp-integration.md` | 接入社区 MCP 的实操清单 + 本项目真实踩过的坑 |

**一个必须知道的协议变化**：新版规范（`2026-07-28`）**取消了 `initialize` 握手**，
改为每个请求在 `_meta` 里自带 `protocolVersion` / `clientInfo` / `clientCapabilities`，
并新增必实现方法 `server/discover`。旧版（`2025-11-25` 及更早）才走 `initialize`。
社区 server 目前绝大多数仍是旧版，所以客户端需要具备回退能力。
版本不匹配的错误码是 `-32022 UnsupportedProtocolVersionError`。

`skills/mcp-integration/` 是把这套流程固化成的 Skill 包，含一个只读审查脚本：

```bash
python skills/mcp-integration/scripts/audit_server.py agent_kit/mcp_servers
```

它检查七项：标准库遮蔽、sys.path 引导、工具可测性、路径越界防护、写操作工具、
stdout 污染、硬编码凭据。自建的四个 server 用它审查是全 PASS；
故意写坏的样例（模块名 `queue.py`、无越界校验、硬编码 `sk-`、模块级 `print`）
会被正确判为 FAIL。

> 审查脚本只排静态问题，**不能替代真实连通性验证**
> （`tools/list` + 逐个 `tools/call` 冒烟）。

<a id="sec7"></a>
## 7. 关键认知（实测踩坑）

1. `create_agent` 在 `langchain.agents`，**不在顶层 `langchain`**。
2. `@before_model` / `@after_model` 回调签名是 `(state, runtime)`；`@dynamic_prompt` 收 `ModelRequest`；`@wrap_tool_call` 是 `(request, handler)`。三者别记混。
3. HITL 恢复载荷必须是 `Command(resume={"decisions": [...]})`，传 list 会报 `list indices must be integers`。
4. `MCPAdapter(target)` 传字符串会被当 URL，本地脚本必须传 `Path`。
5. `override()` 里 `system_prompt` 已 deprecated，改用 `system_message=SystemMessage(...)`。
6. 自定义 state 必须是 TypedDict 且继承 `AgentState`，1.x 不再接受 dataclass / Pydantic。
7. 并行路由的 `results` 要声明成 `Annotated[list, operator.add]`，否则后返回的覆盖先返回的。
8. 脚本化假模型的 `bind_tools` 要返回 `model_copy(deep=True)` 副本，游标要用 `__deepcopy__` 返回自身的共享对象，否则工具会被无限调用。
9. `PostgresStore.from_conn_string()` 返回的是**上下文管理器**，长生命周期进程要手动 `__enter__`，本项目用 `atexit` 关连接池。
10. libpq 默认没有连接超时，PG 不通会一直卡住；连接串必须显式带 `connect_timeout`。
11. `langgraph-checkpoint-redis` 的模块路径是 `langgraph.checkpoint.redis`（不是 `langgraph_checkpoint_redis`）；`ttl` 单位是**分钟**，形如 `{"default_ttl": 10080, "refresh_on_read": True}`。
12. `@before_model` 装饰后返回的是 `AgentMiddleware` 实例，不是函数，不能直接调用；单测要调 `.before_model(state, runtime)`。
13. MCP 工具只有 `ainvoke`，启用 MCP 后 checkpointer 的**异步**接口（`aget_tuple` 等）也必须实现，否则 `astream` 直接失败。
14. 自定义 `wrap_tool_call` / `wrap_model_call` 钩子要同时给同步与异步实现，见 `agent_kit/tool_hooks.py`。
15. MCP 的 stdio 连接绑定在**装配时那个事件循环**上：装配用一次 `asyncio.run()`、执行再用一次，工具调用就报 `Event loop is closed`。CLI 侧改为整个会话共用一个 loop。
16. 关闭 MCP 要用 `adapter.client.close()`，**不要**用 `__aexit__` —— 它退出的是 anyio 任务组，跨 task 调用会报 `Attempted to exit cancel scope in a different task`。
17. 参数注入类拦截器不能无脑注入：工具 schema 声明「不接受任何参数」时（如 `current_utc`）注入会让 Pydantic 报 `unexpected_keyword_argument`。本项目按 schema 自动判断，见 `make_arg_injector`。
18. Multi-Agent 的 `A→B→A` 在 Handoffs（踢皮球）与 Subagents（正常回调）里**语义相反**，环检测必须按模式分开判；判据写成 `path[-1] == path[-3]` 还会漏掉 `A→B→C→A` 四步环。
19. MCP Server 被客户端**当脚本启动**（`python .../server.py`），项目根不在 `sys.path` 里，`import agent_kit...` 直接失败；失败发生在握手之前，客户端只报 `Connection closed`。每个 server 顶部都要显式补 `sys.path`。
20. `@mcp.tool` 会把函数包成 `FunctionTool`，原函数就调不到了 —— 想写单元测试，必须「先写普通函数、最后 `mcp.tool(fn)` 统一注册」。
21. **模块名不能和标准库重名**：本项目曾把排队模块命名为 `agent_kit/queue.py`，结果 MCP Server 以脚本方式启动（`python agent_kit/mcp_server.py`）时 `sys.path[0]` 是 `agent_kit/`，`import queue` 导入的是**我们的模块**而不是标准库 → anyio 报 `cannot import name 'Queue' from 'queue'` → 后端 `asyncio` 加载失败 → 客户端只看到 `Connection closed`。已改名为 `agent_kit/message_queue.py`。**排查线索**：报错信息里出现你自己项目的路径，就是被遮蔽了。

<a id="sec7-1"></a>
## 7.1 测试与 CI

```bash
pip install pytest ruff       # 或 pip install -e ".[dev]"
pytest -q                     # 91 个用例，不依赖 Redis / PG / 真实 Key
ruff check .                  # 静态检查
python main.py guards         # 防护演示：跑偏 / 循环拦截（离线）
```

测试刻意设计成**零外部依赖**：`tests/conftest.py` 会清掉所有环境变量并切到临时目录，
因此 GitHub Actions 里不需要起任何服务（见 `.github/workflows/ci.yml`，
在 Python 3.10 / 3.12 上跑 ruff + pytest，外加 fake 模型的 8 场景冒烟、
7 模式装配、防护演示与 MCP 协议连通）。

六个测试文件各自钉住一类易回归点：

| 文件 | 钉住什么 |
|---|---|
| `tests/test_tool_hooks.py` | `dual` / `dual_model` 必须同时提供同步与异步钩子（缺一个 MCP 链路就炸） |
| `tests/test_arg_injector.py` | 零参数工具（如 `current_utc`）不能被注入额外字段 |
| `tests/test_app_modes.py` | 7 个模式能装配；无 Key 必须明确报错；显式 `fake` 放行 |
| `tests/test_guards.py` | 跑偏 / 循环两类防护的判据（含两种模式下 `A→B→A` 的相反语义） |
| `tests/test_mcp_servers.py` | 路径越界防护、密钥输出打码、不泄漏本机绝对路径 |
| `tests/test_queue.py` | 排队消息的先进先出、会话隔离、上限，以及 HITL 时停止 drain |

### 统一异常处理（对标 Spring 的 @ControllerAdvice）

`server/errors.py` 注册了全局异常处理器 + 请求日志中间件：

- **不改** `HTTPException` 的响应形状（仍是 `{"detail": ...}`），前端依赖该字段；
- 未捕获异常统一成 JSON，**5xx 不回传堆栈**（防信息泄露），完整堆栈只进日志；
- 业务异常映射语义化状态码：`ValueError→400`、`KeyError→404`、
  `PermissionError→403`、`TimeoutError→504`；
- 每个请求带 `X-Request-ID`（沿用上游传来的，否则生成），响应头回传，便于串日志。

```jsonc
// GET 一个会抛 RuntimeError 的接口
{ "detail": "服务内部错误，请查看服务端日志",
  "error": { "type": "RuntimeError", "status": 500, "request_id": "550837452515", "path": "/api/x" } }
```

<a id="sec8"></a>
## 8. 安全提醒

- 密钥**优先**走系统环境变量（`setx DASHSCOPE_API_KEY "..."`），不落盘。
- 本地调试可在项目根目录放 `.env`（已在 `.gitignore`，绝不入库）；只填充环境变量里**缺失**的项，系统变量优先级更高。不要提交、不要外发。
- 密钥一旦出现在聊天记录或截图里，应视为已泄露并去控制台轮换。
- 工具抛出的异常会回传给模型，**不要在异常信息里拼接密钥或完整请求体**。
- 所有密钥展示均经 `mask()` 打码（保留前 4 位与后 4 位）。

### 本地默认凭据（重要）

`docker-compose.yml` 与安装脚本里的 `atlas/atlas`、`Redis 无密码`，
**仅为本地开发方便而设，绝不可用于生产**。
对外部署前请务必：给 Redis 设 `requirepass`、给 PostgreSQL 换强密码，
并把 `REDIS_URL` / `PG_DSN` 改到内网地址。

另外三条，公开部署前请一并确认（详见 [`SECURITY.md`](SECURITY.md)）：

1. **Web 服务默认只监听 `127.0.0.1`**，本项目**没有内置任何认证机制**，
   改绑 `0.0.0.0` 前请自行前置鉴权与反向代理。
2. **MCP Server 等于让模型以你的用户权限执行本地代码**，只接信任的 Server。
3. **不要关掉人工确认（HITL）**，否则等于把写操作的控制权完全交给模型输出。

发现安全漏洞请走 [Private vulnerability reporting](../../security/advisories/new)，
不要开公开 Issue。

<a id="sec9"></a>
## 9. 参与贡献

完整的开发规范、目录约定、三条踩坑铁律都在 [`CONTRIBUTING.md`](CONTRIBUTING.md) ——
**第一次改代码请先读它**，能省掉一轮 review。

快速自检（CI 也会跑这三步）：

```bash
pip install pytest ruff
ruff check .          # 静态检查，必须 0 error
pytest -q             # 27 个用例，必须全过
python main.py demo -p fake   # 离线冒烟
```

三条硬约定：

- 新增/修改中间件钩子时，**同步与异步实现要成对提供**（见 `agent_kit/tool_hooks.py`），
  否则 MCP 链路会在 `astream` 下抛 `awrap_tool_call is not available`。
- 新增依赖请同时写进 `requirements.txt` 与 `pyproject.toml`。
- 测试不要依赖 Redis / PostgreSQL / 真实 API Key——CI 环境里没有这些。

提交信息用 Conventional Commits（`feat:` / `fix:` / `docs:` …），
版本历史记在 [`CHANGELOG.md`](CHANGELOG.md)。
- 涉及密钥的改动，请确保不会把明文写进日志、异常信息或测试断言。

发现安全漏洞请先通过 Issue 私聊作者，不要在公开场合披露细节。

<a id="sec10"></a>
## 10. 许可证

[MIT](LICENSE) © 2026 He Kezhen

可自由使用、修改与分发，包括商业用途；唯一的义务是保留版权声明。
依赖的第三方库（LangChain、FastAPI、FastMCP 等）各自遵循其原许可证。

<a id="sec11"></a>
## 11. 参考资料

本项目的设计依据来自官方文档与社区实践，关键结论均在本地实测校准过。

**LangChain / LangGraph 官方**

| 主题 | 链接 |
|---|---|
| MCP 集成（1.4 新方案） | https://docs.langchain.com/oss/python/langchain/mcp/index |
| 从 `langchain-mcp-adapters` 迁移 | https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters |
| MCP 工具：加载 / 控制 / 输出处理 | https://docs.langchain.com/oss/python/langchain/mcp/tools |
| `MCPAdapter` API 参考 | https://reference.langchain.com/python/langchain/mcp/adapter/MCPAdapter |
| MCP 1.4 改版说明（官方博客） | https://www.langchain.com/blog/mcp-in-langchain-stateless-protocol-elicitation-and-more |
| 中间件（`wrap_tool_call` 等） | https://docs.langchain.com/oss/python/langchain/middleware |
| `langchain-mcp-adapters` 拦截器设计 | https://github.com/langchain-ai/langchain-mcp-adapters |

**MCP 协议与服务端**

| 主题 | 链接 |
|---|---|
| MCP 官方规范 | https://modelcontextprotocol.io |
| FastMCP 文档（本项目 MCP Server 用） | https://gofastmcp.com |
| MCP Python SDK | https://github.com/modelcontextprotocol/python-sdk |

**几处与官方做法不同的取舍**（都是实测后的主动选择，不是照抄）：

1. **不完全依赖官方 `langgraph-checkpoint-redis`** —— 它需要 RedisJSON 模块，
   而多数 Windows 构建与云 Redis 基础版不带该模块。本项目自写 `PlainRedisSaver`
   （纯 SET/HASH/LIST），并在启动时自动探测：有 RedisJSON 就用官方实现，没有则降级。
   代价是降级路径不支持官方的某些查询特性。
2. **MCP 只接本地 stdio 子进程** —— 官方 `MCPAdapter` 支持 URL、
   `MCPConfig` 多服务器、进程内 server 等，本项目为保持示例最小集只用了 `Path`。
   要接远程 server 直接改 `MCPHub.connect([...])` 即可。
3. **危险工具用硬编码黑名单** —— 官方提供了 `destructive_hint` 注解，
   本项目暂未接入，见第 6.1 节的改进说明。
