# MCP 踩坑清单与排查线索

## 一、`Connection closed` 的定位路径

这是 MCP 最常见也最难查的故障：**server 在握手前就崩了，客户端只能看到连接关闭**。

按顺序排查：

1. **直接跑** `python <server.py>` —— 如果进程立刻退出，说明 import 阶段就炸了。
   这一步能解决九成问题。
2. 看报错信息里**有没有自己项目的路径**。
   如果有 → 极可能是**模块名遮蔽了标准库或第三方包**
   （例：自己的 `queue.py` 让 anyio 的 `from queue import Queue` 导入错模块，
   后果是 asyncio 后端加载失败）。
3. 检查 `sys.path` —— 客户端以脚本方式启动时 `sys.path[0]` 是脚本所在目录，
   项目根不在其中，导入项目内模块会失败。每个 server 顶部要显式补路径。
4. 确认 server **没有向 stdout 打印任何东西** —— stdio 传输用换行分隔的 JSON-RPC，
   一行调试 print 就会污染协议流。日志必须走 stderr。

## 二、写 MCP Server 的坑

| 坑 | 症状 | 做法 |
|---|---|---|
| 模块名与标准库重名 | 第三方库 import 失败，报错里出现自己项目的路径 | 改名，避开 `queue`/`types`/`select`/`json`/`http`/`logging`/`token` 等 |
| 缺 `sys.path` 引导 | 只有以脚本方式启动时才失败 | 顶部显式 `sys.path.insert(0, str(Path(__file__).resolve().parents[N]))` |
| `@mcp.tool` 装饰器 | 原函数被包成 `FunctionTool`，逻辑无法单测 | 先写普通函数，末尾 `for fn in (...): mcp.tool(fn)` 统一注册 |
| stdout 被污染 | 协议解析失败 | 日志走 stderr 或文件 |
| 路径参数不校验 | 整个文件系统暴露给模型 | `resolve()` + `relative_to()`/`is_relative_to()`，越界直接报错 |
| 输出含绝对路径 | 泄漏本机目录结构到模型上下文与日志 | 只回相对路径 |
| 疑似密钥原样输出 | 把秘密写进上下文 | 只回打码片段；localhost 默认凭据降级计数，否则误报淹没真问题 |

## 三、解析外部命令输出的坑

- `git status --porcelain`：别用 `line[3:]`，会吃掉路径首字符；
  用 `line[2:].strip()`，并处理 `-> ` 重命名形式。
- `git rev-list --left-right --count`：无上游分支时返回的是错误信息，
  别直接 `int()`，先判断是否纯数字。

## 四、协议层面的坑（2026-07-28 版）

- **Modern 取消了 `initialize` 握手**：版本/身份/能力改为每个请求的 `_meta` 携带。
  按旧版写客户端会连不上新版 server。
- **`server/discover` 是服务端必须实现的**，客户端可先调它探明支持哪些版本。
- **服务端不再主动发起 JSON-RPC 请求**，需要用户输入时返回
  `resultType: "input_required"`，客户端带 `inputResponses` + `requestState` 重发，
  **重发的 `id` 必须不同**。
- **版本错误码 `-32022`**：回包 `data.supported` 列出服务端支持的版本，
  挑一个共同版本重试。
- **区分两类错误**：协议错误（JSON-RPC error，模型救不回来）vs
  工具执行错误（`isError: true`，**必须回喂模型让它自纠**）。混为一谈就废掉了重试能力。
- **工具名只保证单 server 内唯一**：多 server 聚合会撞名，需要消歧策略，
  且不能依赖 `serverInfo.name`（不保证唯一）。

## 五、安全红线

来自官方规范，不是建议：

1. 工具等同**任意代码执行**，调用前必须取得用户明确同意。
2. **工具描述与 annotations 一律视为不可信** —— 恶意 server 可在 description 里
   藏提示词注入。
3. Host 不得在未经同意时把用户数据传给 server 或转发到别处。
4. 有写操作的工具必须挂人工确认。
