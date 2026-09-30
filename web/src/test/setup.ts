import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import type { StreamEvent } from '../types'

/**
 * 页面级测试用的 fetch 桩。
 *
 * 要点：SSE 走的是 fetch + ReadableStream，所以桩必须能吐**分帧的文本流**，
 * 只返回一个完整 JSON 是测不出流式渲染的。
 */
export interface Reply {
  status?: number
  json?: unknown
  text?: string
  /** 逐帧发送的 SSE 事件 */
  sse?: StreamEvent[]
  /** 原始 SSE 文本分片：用来测「一帧被拆进两个网络包」这种粘包/断帧场景 */
  chunks?: string[]
}

export type FetchHandler = (path: string, init?: RequestInit) => Reply | undefined

interface Recorded {
  path: string
  search: string
  method: string
  body: unknown
}

export function installFetch(handler: FetchHandler) {
  const calls: Recorded[] = []

  const fake = (input: unknown, init?: RequestInit) => {
    const raw = typeof input === 'string' ? input : String(input)
    const parsed = new URL(raw, 'http://unit.test')
    let body: unknown = undefined
    if (typeof init?.body === 'string') {
      try {
        body = JSON.parse(init.body)
      } catch {
        body = init.body
      }
    }
    calls.push({
      path: parsed.pathname,
      search: parsed.search,
      method: init?.method ?? 'GET',
      body,
    })

    const reply = handler(parsed.pathname, init) ?? { status: 404, json: { detail: `未打桩：${parsed.pathname}` } }
    const status = reply.status ?? 200
    const payload = reply.sse
      ? reply.sse.map((evt) => `data: ${JSON.stringify(evt)}\n\n`).join('')
      : (reply.text ?? JSON.stringify(reply.json ?? {}))

    let stream: ReadableStream<Uint8Array> | null = null
    if (reply.sse || reply.chunks) {
      const pieces = reply.chunks ?? [payload]
      const encoder = new TextEncoder()
      stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const piece of pieces) controller.enqueue(encoder.encode(piece))
          controller.close()
        },
      })
    }

    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => (reply.sse ? 'text/event-stream' : 'application/json') },
      text: () => Promise.resolve(payload),
      body: stream,
    } as unknown as Response)
  }

  globalThis.fetch = fake as unknown as typeof fetch
  return calls
}

export function findCall(calls: Recorded[], method: string, path: string) {
  return calls.find((c) => c.method === method.toUpperCase() && c.path === path)
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
