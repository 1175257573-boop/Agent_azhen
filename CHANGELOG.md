# 更新日志

本项目的版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)，
格式沿用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

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
