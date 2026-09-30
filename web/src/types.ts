/** 与后端 server/schemas.py + agent_kit/runtime.py 对齐的数据形状。 */

// ---------------------------------------------------------------- 事件流
export type ToolStart = { name: string; args: unknown }
export type ToolEnd = { name: string; output: string }

export type StreamEvent =
  | { type: 'token'; data: string }
  | { type: 'custom'; data: string }
  | { type: 'tool_start'; data: ToolStart }
  | { type: 'tool_end'; data: ToolEnd }
  | { type: 'interrupt'; data: InterruptPayload[] }
  | { type: 'done'; data: { thread_id: string; queued_id?: string | null; pending?: number; mcp?: boolean } }
  | { type: 'error'; data: string }

// ---------------------------------------------------------------- 人工审批
export interface ActionRequest {
  name: string
  args?: Record<string, unknown>
  description?: string
}

export interface ReviewConfig {
  action_name: string
  allowed_decisions?: string[]
}

/** LangGraph 中断载荷（HumanInTheLoopMiddleware 的形状）。 */
export interface InterruptPayload {
  action_requests?: ActionRequest[]
  review_configs?: ReviewConfig[]
  [key: string]: unknown
}

export type DecisionType = 'approve' | 'edit' | 'reject' | 'respond'

export interface Decision {
  type: DecisionType
  edited_action?: { name: string; args: Record<string, unknown> }
  message?: string
}

export interface PendingApproval {
  id: string
  thread_id: string
  created_at: number
  payload: InterruptPayload
}

// ---------------------------------------------------------------- 聊天
export interface ToolRun {
  id: string
  name: string
  args: string
  output?: string
  running: boolean
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  tools: ToolRun[]
  error?: string
  streaming?: boolean
}

// ---------------------------------------------------------------- 后端 DTO
export interface MessageOut {
  role: string
  content: string
  tool_calls?: { name?: string; args?: unknown }[]
  tool_call_id?: string | null
  name?: string | null
}

export interface ThreadBrief {
  thread_id: string
  message_count: number
}

export interface QueuedOut {
  id: string
  thread_id: string
  text: string
  seq: number
  created_at: number
  preview: string
}

export interface MemoryStatus {
  short_term: string
  long_term: string
  window: number
  resolved: Record<string, string>
  report?: Record<string, unknown>
}

export interface Preference {
  key: string
  value: string | null
  updated_at?: string | null
}

export interface CredentialOut {
  provider: string
  label: string
  env_name: string
  configured: boolean
  masked: string
  length: number
  origin: 'env' | 'runtime' | 'none'
  persistent: boolean
  shadowed: boolean
}

export interface CredentialState {
  items: CredentialOut[]
  active_provider: string
  storage_path: string
  file_exists: boolean
  storage_note: string
  notes: string[]
}

export interface VerifyOut {
  ok: boolean
  provider: string
  model: string
  message: string
  latency_ms: number
  sample: string
}

export interface SystemInfo {
  provider: string
  model: string
  providers: string[]
  modes: Record<string, string>
  has_key: boolean
  report?: Record<string, unknown>
}

export interface HealthPayload {
  ok: boolean
  gateway?: string
  agent?: { ok: boolean; status: string }
  memory?: { ok: boolean; status: string }
  python?: string
  memory_backends?: Record<string, string>
  [key: string]: unknown
}
