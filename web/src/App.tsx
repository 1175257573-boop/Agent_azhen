import { useEffect, useState } from 'react'

import { api } from './api'
import Approvals from './pages/Approvals'
import Chat from './pages/Chat'
import Memory from './pages/Memory'
import Queue from './pages/Queue'
import Sessions from './pages/Sessions'
import Settings from './pages/Settings'
import ConfirmDialog from './components/ConfirmDialog'
import { useStore } from './store'
import type { HealthPayload, SystemInfo } from './types'

type PageKey = 'chat' | 'approvals' | 'sessions' | 'queue' | 'settings' | 'memory'

const PAGES: { key: PageKey; label: string; hint: string }[] = [
  { key: 'chat', label: '聊天', hint: '对话与工具调用' },
  { key: 'approvals', label: '审批', hint: '待人工确认的动作' },
  { key: 'sessions', label: '会话', hint: '会话清单与历史' },
  { key: 'queue', label: '队列', hint: '排队中的消息' },
  { key: 'settings', label: '配置', hint: '模式 / MCP / 密钥' },
  { key: 'memory', label: '记忆', hint: '后端状态与长期偏好' },
]

function useHash(): [PageKey, (next: PageKey) => void] {
  const read = (): PageKey => {
    const raw = window.location.hash.replace(/^#\/?/, '')
    return (PAGES.find((p) => p.key === raw)?.key ?? 'chat') as PageKey
  }
  const [page, setPage] = useState<PageKey>(read)
  useEffect(() => {
    const onHash = () => setPage(read())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const go = (next: PageKey) => {
    window.location.hash = `#/${next}`
    setPage(next)
  }
  return [page, go]
}

export default function App() {
  const [page, go] = useHash()
  const { pending, queue, threadId, streaming } = useStore()
  const [health, setHealth] = useState<HealthPayload | null>(null)
  const [info, setInfo] = useState<SystemInfo | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [h, i] = await Promise.all([api.health(), api.info()])
        if (!alive) return
        setHealth(h)
        setInfo(i)
      } catch {
        if (alive) setHealth({ ok: false })
      }
    }
    void tick()
    const timer = window.setInterval(tick, 15000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const badge = (page: PageKey) => {
    if (page === 'approvals' && pending.length) return pending.length
    if (page === 'queue' && queue.length) return queue.length
    return null
  }

  return (
    <div className="app">
      <aside className="nav">
        <div className="brand">
          <div className="mark">◈</div>
          <div>
            <h1>Atlas 控制台</h1>
            <p>LangChain 1.4 · LangGraph</p>
          </div>
        </div>

        <nav>
          {PAGES.map((p) => (
            <button
              key={p.key}
              className={`nav-item${page === p.key ? ' active' : ''}`}
              onClick={() => go(p.key)}
              title={p.hint}
            >
              <span>{p.label}</span>
              {badge(p.key) ? <i className="badge-count">{badge(p.key)}</i> : null}
            </button>
          ))}
        </nav>

        <div className="nav-foot">
          <div className="kv">
            <span>会话</span>
            <code title={threadId}>{threadId}</code>
          </div>
          <div className="kv">
            <span>状态</span>
            <span className={streaming ? 'dot-live' : 'dot-idle'}>{streaming ? '生成中' : '空闲'}</span>
          </div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <Pill label="后端" ok={health?.ok ?? null} text={health?.ok ? '正常' : '不可达'} />
          <Pill label="模型" ok={null} text={info ? `${info.provider} · ${info.model}` : '检测中…'} />
          <Pill
            label="记忆"
            ok={null}
            text={health?.memory_backends ? Object.entries(health.memory_backends).map(([k, v]) => `${k}:${v}`).join(' ') : (info ? '—' : '检测中…')}
          />
          {health?.agent ? <Pill label="Agent 服务" ok={health.agent.ok} text={health.agent.status} /> : null}
          {health?.memory && 'status' in health.memory ? (
            <Pill label="记忆服务" ok={health.memory.ok} text={String(health.memory.status)} />
          ) : null}
        </header>

        <section className="content">
          {page === 'chat' && <Chat />}
          {page === 'approvals' && <Approvals />}
          {page === 'sessions' && <Sessions />}
          {page === 'queue' && <Queue />}
          {page === 'settings' && <Settings />}
          {page === 'memory' && <Memory />}
        </section>
      </main>

      <Toasts />
      <ConfirmHost />
    </div>
  )
}

function Pill({ label, text, ok }: { label: string; text: string; ok: boolean | null }) {
  const cls = ok === null ? '' : ok ? ' ok' : ' bad'
  return (
    <span className={`pill${cls}`} title={`${label}：${text}`}>
      <b>{label}</b>
      <span>{text}</span>
    </span>
  )
}

function Toasts() {
  const { toasts } = useStore()
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <span className="toast-icon" aria-hidden="true">
            {t.kind === 'ok' ? (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                <path d="m5 12.5 4.5 4.5L19 7.5" />
              </svg>
            ) : t.kind === 'error' ? (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                <path d="M12 6.5v7" />
                <path d="M12 17.2h.01" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="9" />
                <path d="M12 11v5.5" />
                <path d="M12 7.8h.01" />
              </svg>
            )}
          </span>
          <span className="toast-text">{t.text}</span>
        </div>
      ))}
    </div>
  )
}

function ConfirmHost() {
  const { confirmState, confirmAction, resolveConfirm } = useStore()

  // 桌面端点关闭键时，主进程会来问一句。用同一个弹窗组件承接，
  // 样式和文案才能和应用里其他地方统一（原生 dialog 是系统样式，格格不入）。
  useEffect(() => {
    const bridge = window.atlasDesktop
    if (!bridge?.onConfirmClose) return
    return bridge.onConfirmClose(() => {
      void (async () => {
        const action = await confirmAction({
          title: '要最小化，还是退出？',
          detail:
            '最小化：窗口收进系统托盘，后端继续运行；点托盘图标即可恢复。\n' +
            '退出：会一并停掉本地后端服务，下次启动需要重新拉起（约 1 秒）。',
          confirmText: '最小化到托盘',
          cancelText: '退出（同时停止后端）',
        })
        // 三种结果都必须回传。dismiss（Esc / 点遮罩）虽然什么都不做，
        // 但主进程正在等这个回话来解除「关窗询问中」的锁 —— 漏掉它，
        // 之后每次点关闭键都会被当成"上一轮还没结束"而毫无反应。
        await bridge.closeDecision(
          action === 'confirm' ? 'minimize' : action === 'cancel' ? 'quit' : 'cancel',
        )
      })()
    })
  }, [confirmAction])

  return <ConfirmDialog options={confirmState} onResolve={resolveConfirm} />
}
