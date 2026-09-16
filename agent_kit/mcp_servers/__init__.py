"""MCP Server 集合：工程质量体检 / Git 只读查询 / 文档一致性审计。

三个 server 的共同约定：
  · **全部只读** —— 不写文件、不改版本历史，Agent 可以放心在无人值守场景调用
  · **路径收敛** —— 任何入参路径都必须落在项目根目录内（见 _common.resolve_dir）
  · **输出脱敏** —— 只回相对路径，密钥只回打码片段，不泄漏本机目录结构与秘密原文

注册到 MCPHub：`agent_kit.mcp_client.ALL_SERVERS`，客户端侧无需为新增 server 改代码。
"""
