# MCP 协议规范要点

（基于 modelcontextprotocol.io 官方规范 **2026-07-28** 版整理。
协议演进较快，接入第三方 server 前请回官网核对当前版本。）

## 一、是什么

Model Context Protocol：LLM 应用与外部数据源 / 工具之间的开放协议。
设计上借鉴 LSP（Language Server Protocol）。

消息编码：**JSON-RPC 2.0**，UTF-8。

三种角色：

| 角色 | 职责 |
|---|---|
| Host | LLM 应用本体，创建并管理多个 client，负责安全策略与用户授权 |
| Client | 由 host 创建，**与一个 server 一对一**通信 |
| Server | 提供 resources / tools / prompts 等能力 |

设计原则（官方原文要点）：server 要极易实现、可组合、
**不能读整个会话、也不能"看见"其它 server**；能力可渐进添加。

## 二、两个时代：Modern 与 Legacy

这是 2026-07-28 版最重要的变化，**接入社区 server 时首先得判断对方属于哪个时代**。

| 术语 | 含义 |
|---|---|
| **Modern** | `2026-07-28` 及以后。版本、身份、能力作为**每个请求**的元数据携带，**没有握手** |
| **Legacy** | `2025-11-25` 及更早。用 `initialize` 握手建立会话 |
| **Dual-era** | 两种都支持的实现 |

### Modern 的请求元数据

每个请求都必须带 `_meta`：

```
io.modelcontextprotocol/protocolVersion   协议版本
io.modelcontextprotocol/clientInfo        客户端身份
io.modelcontextprotocol/clientCapabilities 客户端能力
```

服务端能力通过 `server/discover` 宣告（server **MUST** 实现该方法）。
客户端可在发其它请求前先调 `server/discover` 探明支持哪些版本，但不是必须——
也可以直接发请求，靠错误回退。

### 版本不匹配

服务端不认识该版本时，返回 `UnsupportedProtocolVersionError`：

```json
{"jsonrpc":"2.0","id":1,"error":{
  "code": -32022,
  "message": "Unsupported protocol version",
  "data": {"supported":["2026-07-28","2025-11-25"], "requested":"1900-01-01"}}}
```

客户端应从 `supported` 里挑一个共同版本重试。

### 兼容矩阵（官方）

| 客户端 | 服务端 | 结果 |
|---|---|---|
| Modern | Modern | 正常 |
| Modern | Legacy | **失败**。stdio 下应先发 `server/discover` 确定性失败 |
| Dual-era | Modern | 正常 |
| Dual-era | Legacy | 正常（探测失败/超时后回退到 `initialize`） |
| Legacy | Modern | **失败**。legacy 客户端没有向前兼容机制 |
| Legacy | Dual-era | 正常 |
| Legacy | Legacy | 按旧版规范，本文档不覆盖 |

**识别方法**：能返回可识别的 modern 错误（如 -32022）→ modern server，重试即可；
返回其它任何东西 → legacy server，回退到 `initialize`。
这个判定是**服务端的属性，不是单次请求的属性**，应缓存（stdio 按进程、HTTP 按 origin）。

## 三、传输层

协议语义在所有传输上一致，传输只定义**成帧与投递**。

| 传输 | 说明 |
|---|---|
| **stdio** | 客户端启动子进程，**换行分隔的 JSON-RPC** 走标准流 |
| **Streamable HTTP** | 每条消息一次 HTTP POST 到单一端点，响应是 JSON 对象或 request-scoped SSE 流 |
| 自定义传输 | 允许。若跑在可靠双向字节流上（Unix socket / TCP），**应复用 stdio 的成帧** |

旧的 HTTP+SSE 传输已废弃（仅作为 legacy 回退路径存在）。
Streamable HTTP 会把版本/能力镜像到 HTTP 头（如 `MCP-Protocol-Version`），
但**消息体才是事实来源**。

取消：stdio 发 `notifications/cancelled`；Streamable HTTP 关闭响应流。

**重要约束**：server 不主动发起 JSON-RPC 请求，client 不发 JSON-RPC 响应。
server 需要用户输入时，改用下文 `InputRequiredResult` 机制。

## 四、能力（Capabilities）

服务端能力示例：

```json
{"capabilities": {"tools": {"listChanged": true}}}
```

客户端能力示例（可含 `roots`、扩展等）：

```json
{"capabilities": {"roots": {}, "extensions": {"io.modelcontextprotocol/ui": {...}}}}
```

- 声明了 `tools` 能力的 server **MUST** 响应 `tools/list`
- 工具集可按**请求携带的授权**变化（凭据是每请求输入，不是连接状态）
- 但**不得**因连接不同或副作用而变化
- 工具列表应**稳定排序**（利于客户端缓存 + 提高 prompt cache 命中率）

扩展（Extensions）是可选的、双方显式协商的：目前有
Tasks（长任务异步执行）、Skills over MCP、MCP Apps（内联交互 UI）等。

## 五、工具（Tools）

### 方法

| 方法 | 用途 |
|---|---|
| `tools/list` | 列出工具，支持 `cursor` 分页 |
| `tools/call` | 调用工具，参数 `{name, arguments}` |
| `notifications/tools/list_changed` | 工具列表变化通知（需先开 `subscriptions/listen` 流） |

`tools/list` 响应含 `resultType: "complete"`、`tools[]`、`nextCursor`、
以及 `ttlMs` / `cacheScope`（可缓存性）。

### 工具定义字段

`name` / `title` / `description` / `icons` / `inputSchema` / `outputSchema` / `annotations`

- `inputSchema` 与 `outputSchema` 默认 JSON Schema **2020-12**
- 无参数工具推荐写 `{"type":"object","additionalProperties":false}`
- `annotations` 描述工具行为，**客户端必须视为不可信**（除非来自可信 server）
- 属性可加 `x-mcp-header` 注解，把参数值作为 HTTP 头暴露

工具名：1–128 字符、**大小写敏感**、建议只用 `A-Za-z0-9_-.`、
不含空格逗号、单个 server 内唯一。
**多 server 聚合时可能撞名**（两个 server 都有 `search`），
客户端应做消歧（如加 server 前缀）；`serverInfo.name` 不保证唯一，不能拿来消歧。

### 调用结果

```json
{"jsonrpc":"2.0","id":2,"result":{
  "resultType": "complete",
  "content": [{"type":"text","text":"..."}],
  "structuredContent": {...},
  "isError": false}}
```

`structuredContent` 是**结构化结果数据**，与 LLM 的"结构化输出"无关。
为兼容旧客户端，返回结构化内容时应同时把序列化 JSON 放一个 TextContent 块。

### 需要补充输入（Modern 的新机制）

server 不再反向发请求，而是返回一个 `InputRequiredResult`：

```json
{"result":{
  "resultType": "input_required",
  "inputRequests": {"github_login": {"method":"elicitation/create", "params":{...}}},
  "requestState": "eyJ..."}}
```

客户端补齐后重发 `tools/call`，带上 `inputResponses` 与 `requestState`。
**注意：重发时 JSON-RPC `id` 必须不同。**

### 错误分两类（关键区别）

| 类型 | 表现 | 该不该回喂模型 |
|---|---|---|
| 协议错误 | 标准 JSON-RPC error，如 `-32602 Unknown tool` | 模型基本无法自纠，可不回喂 |
| 工具执行错误 | `result.isError: true`，content 里有可操作信息 | **应当回喂**，让模型改参数重试 |

## 六、其它原语

- **Resources**：`resources/list`、`resources/read` 等；更新通知需开 `subscriptions/listen` 流
- **Prompts**：`prompts/list`、`prompts/get`
- **Elicitation**：`elicitation/create`，server 反向请求用户补充信息
- **Roots**：客户端可声明 `roots` 能力

## 七、安全原则（官方原文要点）

1. 用户必须**明确同意**所有数据访问与操作，且始终保有控制权
2. Host **不得**在未经同意时把用户数据传给 server 或转发到别处
3. 工具等同**任意代码执行**：调用前必须取得用户明确同意
4. **工具描述、annotations 等来自 server 的内容一律视为不可信**

## 八、来源

- 规范（2026-07-28）：https://modelcontextprotocol.io/specification/2026-07-28
- TypeScript schema：https://github.com/modelcontextprotocol/specification
- 本笔记只覆盖核心部分，Tasks / Apps / Skills over MCP 等扩展见官网
