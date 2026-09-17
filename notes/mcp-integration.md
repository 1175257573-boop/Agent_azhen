# 接入社区 MCP Server 的实操清单

（结合本项目 `agent_kit/mcp_servers/` 自建四个 server 的真实经验整理。
与 `mcp-protocol.md` 配合使用。）

## 一、接入前先问四个问题

| 问题 | 为什么重要 |
|---|---|
| 属于哪个**时代**（Modern / Legacy）？ | Legacy server 需要 `initialize` 握手，客户端得支持回退，否则连不上 |
| 用什么**传输**（stdio / Streamable HTTP）？ | stdio 要处理子进程启动与环境变量；HTTP 要处理鉴权头 |
| 声明了哪些**能力**（tools / resources / prompts）？ | 只有声明的能力才能调，没声明就别调 |
| 有没有**写操作**？ | 有写操作的 server 必须挂 human-in-the-loop 确认 |

社区里**绝大多数 server 仍是 legacy 时代**（`2025-11-25` 及更早）。
所以客户端最好是 dual-era：先按 modern 探一次，拿到非 modern 错误就回退 `initialize`。

## 二、配置落位

本项目的 server 注册表在 `agent_kit/mcp_client.py` 的 `ALL_SERVERS`：

```python
ALL_SERVERS: dict[str, Path] = {
    "notes":   SERVER_PATH,                    # stdio 脚本
    "quality": SERVER_DIR / "quality.py",
    "git":     SERVER_DIR / "git_history.py",
    "docs":    SERVER_DIR / "doc_audit.py",
}
```

加社区 server 时按同样方式登记，命名用**短且唯一**的 key。
stdio 型要写明 `command` / `args` / `env`；别把凭据写死在代码里，走环境变量。

## 三、连通性验证（先单跑，再接 agent）

MCP 握手失败时客户端往往只报一句 `Connection closed`，
**看不到 server 内部的异常**。所以接进 agent 之前必须先单独跑通：

1. 直接 `python <server.py>` 启动，看进程是否立刻退出（退出就是 import 炸了）
2. 用最小客户端脚本列一次 `tools/list`，确认工具数量与预期一致
3. 逐个 `tools/call` 冒烟，确认返回值结构

本项目 `examples/mcp_servers_demo.py` 就是干这个的：一次连通四个 server、21 个工具。

## 四、安全检查（只读 server 的三条硬约束）

自建只读 server 时定下的三条，审视别人的 server 同样适用：

1. **路径不越界** —— 接受路径参数时必须校验落在允许根目录内，越界直接报错，
   不要静默截断
2. **输出不含秘密原文** —— 疑似密钥只回打码片段；本地默认凭据（localhost 连接串）
   应降级计数而不是当泄密，否则误报会淹没真问题
3. **只回相对路径** —— 绝对路径会把本机目录结构泄漏进模型上下文和日志

补充两条对**第三方 server** 的检查：

4. **工具描述与 annotations 一律视为不可信** —— 官方明确要求。
   恶意 server 可以在 description 里藏提示词注入
5. **有写操作的工具必须人工确认** —— 本项目用 `HumanInTheLoopMiddleware` 拦 `WRITE_TOOLS`

## 五、本项目踩过的坑（接入前先看一遍，能省几小时）

1. **以脚本方式启动导致 import 失败** —— 客户端 `python <path/server.py>` 启动时
   `sys.path[0]` 是脚本所在目录，项目根不在其中 → `import agent_kit` 直接失败。
   而这个失败发生在 stdio 握手**之前**，客户端只报 `Connection closed`，极难定位。
   **每个 server 顶部必须显式补 sys.path。**

2. **模块名遮蔽标准库** —— 曾经有 `agent_kit/queue.py` 遮蔽了标准库 `queue`，
   导致 anyio 的 `from queue import Queue` 导入了我们的模块 → asyncio 后端加载失败
   → 同样只表现为 `Connection closed`。
   **排查线索：报错信息里出现自己项目的路径。**
   **命名红线：模块名不要与标准库或常用第三方包重名。**（已改名 `message_queue.py`）

3. **`@mcp.tool` 会把函数包成 `FunctionTool`** —— 原函数就调不到了，逻辑无法单测。
   写法应该是「先定义普通函数，末尾 `for fn in (...): mcp.tool(fn)` 统一注册」。

4. **解析 `git status --porcelain` 别用 `line[3:]`** —— 会吃掉路径首字符；
   要用 `line[2:].strip()`，并处理 `-> ` 重命名形式。

5. **无上游分支时 `rev-list` 返回错误信息** —— 别直接 `int()`，先判断是否纯数字。

## 六、多 server 聚合的撞名问题

工具名只保证**单个 server 内唯一**。聚合多个 server 时，
两个 server 都暴露 `search` 是完全可能的。
客户端应实现消歧策略（例如加 server 前缀名），
且**不能依赖 `serverInfo.name` 做消歧**——它不保证唯一。

## 七、判断"该做成 MCP 还是 Skill"

一句话标准：**跨进程的是 MCP，跨轮次复用提示词的是 Skill。**

- MCP 回答「能做什么」——独立进程、走协议、可被任何客户端复用
- Skill 回答「这件事该按什么步骤做」——提示词、渐进式披露、跨轮次复用

同一件事常常两者配合：Skill 定方法论与输出格式，MCP 负责取证。
