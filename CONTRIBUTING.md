# 参与贡献

感谢你愿意花时间在 Atlas Agent 上。这份文档讲清楚「改代码前要做什么」「提交前要过哪几关」，
照着走一遍，PR 基本一次过。

## 1. 环境准备

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate      # Linux / macOS

pip install -r requirements.txt
pip install ruff pytest          # 开发依赖
```

配置模型 Key（任一个即可），推荐写进 `.env`：

```bash
cp .env.example .env
```

> 没有 Key 也能跑测试 —— `tests/` 全部使用 fake 模型，不联网、不花钱。

## 2. 目录约定（Java 同学可以直接对号入座）

| 目录 | Java 类比 | 职责 |
| --- | --- | --- |
| `main.py` | `Application` | CLI 入口，只做参数解析与调度 |
| `server/routers/` | `Controller` | HTTP 路由，只做参数校验与转发 |
| `server/service/` | `Service` | 业务编排，Agent 生命周期在这里 |
| `agent_kit/` | `Domain` | Agent、中间件、记忆、工具 |
| `server/schemas.py` | `DTO` | 请求/响应模型 |
| `server/errors.py` | `@ControllerAdvice` | 全局异常处理 |

**铁律**：`routers/` 不写业务逻辑，`agent_kit/` 不碰 HTTP。

## 3. 开发流程

```bash
git checkout -b feat/你的改动
# ... 写代码 ...
ruff check .          # 必须 0 报错
pytest -q             # 必须全绿
```

## 4. 三条容易踩的坑（都是本项目真实踩过的）

### 4.1 工具调用钩子必须成对提供同步 / 异步版本

LangGraph 在 `stream()` 走同步路径、`astream()` 走异步路径，
**只写 sync 版本，在 FastAPI 里会报 `awrap_tool_call is not available`**。

用 `agent_kit/tool_hooks.py` 里的工具函数，别手写：

```python
from agent_kit.tool_hooks import dual, dual_model

hook = dual(sync_fn, async_fn)          # 工具钩子
hook = dual_model(sync_fn, async_fn)    # 模型钩子
```

`tests/test_tool_hooks.py` 专门钉住这条，改坏了会红。

### 4.2 MCP 工具只有异步实现

MCP 工具**只实现了 `ainvoke`**，同步调用会抛
`NotImplementedError: StructuredTool does not support sync invocation`。
凡是可能挂 MCP 工具的路径，一律走 `astream()` / `ainvoke()`。

### 4.3 参数注入前要查工具的 JSON Schema

给工具自动注入 `user_id` / `current_utc` 这类参数时，
**零参数工具被注入会触发 Pydantic `unexpected_keyword_argument`**。
用 `agent_kit/mcp_client.py` 的 `_schema_accepts()` 判断，别无脑注入。

## 5. 测试

```bash
pytest -q                          # 全量（约 27 个用例，秒级）
pytest tests/test_tool_hooks.py -v # 单文件
pytest -k "mcp" -v                 # 按关键字
```

新增行为请先加测试。三种情况必须补测试：

- 改了钩子 / 中间件执行顺序
- 改了记忆后端或降级逻辑
- 改了 MCP 参数注入

`tests/conftest.py` 会自动清空所有环境变量并切到临时目录，
所以测试**不会**污染你本机的 Redis / PostgreSQL。

## 6. 提交信息

用 Conventional Commits，CI 与后续 Release Notes 都读它：

```
feat: 新增 xxx
fix: 修复 xxx
docs: 补充 xxx
refactor: 重构 xxx
test: 补充 xxx 的测试
chore: 依赖 / CI 调整
```

## 7. PR 检查清单

- [ ] `ruff check .` 零报错
- [ ] `pytest -q` 全绿
- [ ] 新增/修改的行为有对应测试
- [ ] 没有把 `.env`、真实 Key、个人信息提交进来
- [ ] 改了用户可见行为的话，同步更新 `README.md` 与 `CHANGELOG.md`

## 8. 行为准则

就一句话：对事不对人。技术争论欢迎，人身攻击不欢迎。
