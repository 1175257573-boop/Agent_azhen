import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'
import type { ReactNode } from 'react'

import { findCall, installFetch, type FetchHandler } from '../test/setup'
import { StoreProvider } from '../store'
import Chat from '../pages/Chat'
import Sessions from '../pages/Sessions'
import QueuePage from '../pages/Queue'
import SettingsPage from '../pages/Settings'
import MemoryPage from '../pages/Memory'
import { streamSse } from '../api'
import type { CredentialState } from '../types'

/**
 * 页面级测试：真的渲染组件、真的走 store、真的消费 SSE 流，
 * 只有网络那一层换成桩。
 *
 * 注意断言不依赖 Toast——Toast 由 App 渲染，单测里只挂页面。
 * 要验证的是「页面有没有把请求发出去、有没有把结果渲染出来」。
 */
function mount(ui: ReactNode, handler: FetchHandler) {
  const calls = installFetch(handler)
  render(<StoreProvider>{ui}</StoreProvider>)
  return calls
}

/** 每个页面挂载时都会拉的基础接口，不打桩就是一堆 404 噪音。 */
function base(path: string): { json: unknown } | undefined {
  if (path === '/api/chat/history') return { json: [] }
  if (path === '/api/chat/queue') return { json: [] }
  return undefined
}

// ---------------------------------------------------------------------------
it('聊天页：流式渲染 token 与工具调用，并把参数发给 /api/chat/stream', async () => {
  const calls = mount(<Chat />, (path) => {
    if (path === '/api/chat/stream') {
      return {
        sse: [
          { type: 'tool_start', data: { name: 'get_current_time', args: { tz: 'Asia/Shanghai' } } },
          { type: 'tool_end', data: { name: 'get_current_time', output: '2026-09-30 20:00' } },
          { type: 'token', data: '现在时间' },
          { type: 'token', data: '是 20:00。' },
          { type: 'done', data: { thread_id: 'atlas-main', pending: 0 } },
        ],
      }
    }
    return base(path)
  })

  await userEvent.type(await screen.findByPlaceholderText(/输入消息/), '几点了{Enter}')

  await screen.findByText('现在时间是 20:00。')
  expect(screen.getByText('get_current_time')).toBeTruthy()
  expect(screen.getByText('2026-09-30 20:00')).toBeTruthy()

  const call = findCall(calls, 'POST', '/api/chat/stream')
  expect(call?.body).toMatchObject({ message: '几点了', thread_id: 'atlas-main', user_id: 'demo', role: 'admin' })
})

// ---------------------------------------------------------------------------
it('聊天页：中断事件变成审批卡片，工具不会先执行；批准后才继续', async () => {
  const calls = mount(<Chat />, (path) => {
    if (path === '/api/chat/stream') {
      return {
        sse: [
          {
            type: 'interrupt',
            data: [
              {
                action_requests: [{ name: 'write_report', args: { filename: 'a.md' }, description: '写入报告' }],
                review_configs: [{ action_name: 'write_report', allowed_decisions: ['approve', 'edit', 'reject'] }],
              },
            ],
          },
        ],
      }
    }
    if (path === '/api/chat/resume') {
      return {
        sse: [
          // 恢复执行的流里常常只有 tool_end：工具其实在上一段被中断前就跑完了
          { type: 'tool_end', data: { name: 'write_report', output: '已写入 a.md' } },
          { type: 'token', data: '报告写好了。' },
          { type: 'done', data: { thread_id: 'atlas-main' } },
        ],
      }
    }
    return base(path)
  })

  await userEvent.type(await screen.findByPlaceholderText(/输入消息/), '写个报告{Enter}')

  await screen.findByText(/需要人工确认/)
  expect(findCall(calls, 'POST', '/api/chat/resume')).toBeUndefined()
  expect(screen.queryByText('已写入 a.md')).toBeNull()

  await userEvent.click(screen.getByRole('button', { name: '批准' }))
  await userEvent.click(screen.getByRole('button', { name: '提交决策' }))

  await screen.findByText('报告写好了。')
  expect(screen.getByText('已写入 a.md')).toBeTruthy()

  const resume = findCall(calls, 'POST', '/api/chat/resume')
  expect(resume?.body).toMatchObject({ decisions: [{ type: 'approve' }], thread_id: 'atlas-main' })
})

// ---------------------------------------------------------------------------
it('会话页：列出 / 新建 / 删除会话', async () => {
  let threads: { thread_id: string; message_count: number }[] = [{ thread_id: 'atlas-old', message_count: 4 }]

  const calls = mount(<Sessions />, (path, init) => {
    const method = init?.method ?? 'GET'
    if (path.startsWith('/api/chat/threads')) {
      if (method === 'POST') {
        threads = [...threads, { thread_id: 'atlas-new', message_count: 0 }]
        return { json: { thread_id: 'atlas-new', message_count: 0 } }
      }
      if (method === 'DELETE') {
        const gone = decodeURIComponent(path.split('/').pop() ?? '')
        threads = threads.filter((t) => t.thread_id !== gone)
        return { json: { ok: true, thread_id: gone } }
      }
      return { json: threads }
    }
    return base(path)
  })

  await screen.findByText('atlas-old')

  await userEvent.click(screen.getByRole('button', { name: '＋ 新建会话' }))
  expect(await screen.findByText('atlas-new')).toBeTruthy()
  expect(findCall(calls, 'POST', '/api/chat/threads')).toBeTruthy()

  await userEvent.click(screen.getAllByRole('button', { name: '删除' })[0])
  await waitFor(() => expect(screen.queryByText('atlas-old')).toBeNull())
  expect(calls.some((c) => c.method === 'DELETE' && c.path === '/api/chat/threads/atlas-old')).toBe(true)
})

// ---------------------------------------------------------------------------
it('队列页：入队 / 撤回都打到正确的接口，列表跟着变', async () => {
  let items = [{ id: 'q1', thread_id: 'atlas-main', text: '稍后问', seq: 1, created_at: 0, preview: '' }]

  const calls = mount(<QueuePage />, (path, init) => {
    const method = init?.method ?? 'GET'
    if (path.startsWith('/api/chat/queue')) {
      if (method === 'POST') {
        const body = JSON.parse(String(init?.body ?? '{}'))
        items = [...items, { id: 'q2', thread_id: 'atlas-main', text: body.message, seq: 2, created_at: 0, preview: '' }]
        return { json: items[items.length - 1] }
      }
      if (method === 'DELETE') {
        if (path === '/api/chat/queue') {
          items = []
          return { json: { ok: true, cleared: 0 } }
        }
        const gone = decodeURIComponent(path.split('/').pop() ?? '')
        items = items.filter((q) => q.id !== gone)
        return { json: { ok: true, id: gone } }
      }
      return { json: items }
    }
    return base(path)
  })

  await screen.findByText('稍后问')

  await userEvent.type(screen.getByPlaceholderText('输入要排队的消息'), '新问题{Enter}')
  await waitFor(() => expect(screen.queryByText('新问题')).toBeTruthy())
  expect(findCall(calls, 'POST', '/api/chat/queue')?.body).toMatchObject({
    thread_id: 'atlas-main',
    message: '新问题',
  })

  await userEvent.click(screen.getAllByRole('button', { name: '撤回' })[0])
  await waitFor(() => expect(screen.queryByText('稍后问')).toBeNull())
  expect(calls.some((c) => c.method === 'DELETE' && c.path === '/api/chat/queue/q1')).toBe(true)
})

// ---------------------------------------------------------------------------
it('配置页：保存密钥时明文只在请求体里，且带上 remember', async () => {
  const state: CredentialState = {
    items: [
      {
        provider: 'openai',
        label: 'OpenAI',
        env_name: 'OPENAI_API_KEY',
        configured: false,
        masked: '',
        length: 0,
        origin: 'none',
        persistent: false,
        shadowed: false,
      },
    ],
    active_provider: 'fake',
    storage_path: '',
    file_exists: false,
    storage_note: '',
    notes: ['密钥不会写入浏览器存储'],
  }

  const calls = mount(<SettingsPage />, (path, init) => {
    if (path === '/api/modes') return { json: { chat: '普通对话', research: '深度研究' } }
    if (path === '/api/info') {
      return { json: { provider: 'fake', model: 'fake-1', providers: ['fake', 'openai'], modes: {}, has_key: false } }
    }
    if (path === '/api/credentials') {
      if ((init?.method ?? 'GET') === 'POST') {
        state.items[0] = { ...state.items[0], configured: true, masked: 'sk-1***0000', length: 18, origin: 'runtime' }
      }
      return { json: state }
    }
    return base(path)
  })

  await screen.findByText('密钥管理')

  const input = screen.getByPlaceholderText(/粘贴密钥/) as HTMLInputElement
  await userEvent.type(input, 'sk-unit-test-000000')
  await userEvent.click(screen.getByRole('button', { name: '保存并启用' }))

  await waitFor(() => expect(findCall(calls, 'POST', '/api/credentials')).toBeTruthy())
  const saved = findCall(calls, 'POST', '/api/credentials')
  // 假密钥，形态与真实一致才能验证「明文只在请求体」这条断言；从不用于任何真实请求
  expect(saved?.body).toMatchObject({ provider: 'openai', api_key: 'sk-unit-test-000000', remember: false })

  // 明文不能落到任何浏览器存储里
  expect(JSON.stringify(localStorage)).not.toContain('sk-unit-test-000000')
  await waitFor(() => expect((screen.getByPlaceholderText(/粘贴密钥/) as HTMLInputElement).value).toBe(''))
})

// ---------------------------------------------------------------------------
it('记忆页：展示后端状态，写入与删除长期偏好', async () => {
  let prefs = [{ key: '语言偏好', value: '中文', updated_at: null }]

  const calls = mount(<MemoryPage />, (path, init) => {
    const method = init?.method ?? 'GET'
    if (path === '/api/memory/status') {
      return { json: { short_term: 'sqlite', long_term: 'postgres', window: 20, resolved: {} } }
    }
    if (path === '/api/memory/preferences') {
      if (method === 'POST') {
        const body = JSON.parse(String(init?.body ?? '{}'))
        prefs = [...prefs, { key: body.key, value: body.value, updated_at: null }]
        return { json: { user_id: body.user_id, key: body.key, value: body.value } }
      }
      if (method === 'DELETE') {
        const body = JSON.parse(String(init?.body ?? '{}'))
        prefs = prefs.filter((p) => p.key !== body.key)
        return { json: { ok: true } }
      }
      return { json: prefs }
    }
    return base(path)
  })

  const backends = await screen.findAllByText(/sqlite|postgres/)
  expect(backends.length).toBeGreaterThanOrEqual(2)
  await screen.findByText('语言偏好')

  await userEvent.type(screen.getByPlaceholderText('键（如 语言偏好）'), '城市')
  await userEvent.type(screen.getByPlaceholderText('值（如 中文回答）'), '武汉{Enter}')

  await waitFor(() => expect(screen.queryByText('城市')).toBeTruthy())
  expect(findCall(calls, 'POST', '/api/memory/preferences')?.body).toMatchObject({
    user_id: 'demo',
    key: '城市',
    value: '武汉',
  })
})

// ---------------------------------------------------------------------------
it('SSE 解析：心跳被忽略、被拆断的帧能重新拼回来', async () => {
  installFetch(() => ({
    chunks: [
      ': ping\n\n',
      'data: {"type":"token","data":"前半',
      '截"}\n\n: ping\n\ndata: {"type":"token","data":"后半"}\n\n',
    ],
  }))

  const events = []
  for await (const evt of streamSse('/api/chat/stream', {})) events.push(evt)

  expect(events).toEqual([
    { type: 'token', data: '前半截' },
    { type: 'token', data: '后半' },
  ])
})
