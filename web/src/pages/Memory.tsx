import { useCallback, useEffect, useState } from 'react'

import { api } from '../api'
import { useStore } from '../store'
import type { MemoryStatus, Preference } from '../types'

export default function Memory() {
  const { userId, toast } = useStore()
  const [status, setStatus] = useState<MemoryStatus | null>(null)
  const [prefs, setPrefs] = useState<Preference[]>([])
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')

  const load = useCallback(async () => {
    try {
      setStatus(await api.memoryStatus())
    } catch (err) {
      toast('error', `记忆状态加载失败：${err instanceof Error ? err.message : err}`)
    }
    try {
      setPrefs(await api.preferences(userId))
    } catch {
      setPrefs([])
    }
  }, [toast, userId])

  useEffect(() => {
    void load()
  }, [load])

  const put = async () => {
    const k = key.trim()
    const v = value.trim()
    if (!k || !v) return
    try {
      await api.putPreference(userId, k, v)
      setKey('')
      setValue('')
      await load()
      toast('ok', `已记住 ${k}`)
    } catch (err) {
      toast('error', `写入失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const drop = async (k: string) => {
    try {
      await api.deletePreference(userId, k)
      await load()
    } catch (err) {
      toast('error', `删除失败：${err instanceof Error ? err.message : err}`)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h2>记忆</h2>
        <span className="muted">
          短期记忆存对话线程（按会话隔离），长期记忆存跨会话的用户偏好（按 user_id 隔离）。
        </span>
      </div>

      <section className="card">
        <h3>后端状态</h3>
        {status ? (
          <div className="kv-grid">
            <div className="kv">
              <span>短期记忆</span>
              <code>{status.short_term}</code>
            </div>
            <div className="kv">
              <span>长期记忆</span>
              <code>{status.long_term}</code>
            </div>
            <div className="kv">
              <span>上下文窗口</span>
              <code>{status.window}</code>
            </div>
            {Object.entries(status.resolved ?? {}).map(([k, v]) => (
              <div className="kv" key={k}>
                <span>{k}</span>
                <code>{v}</code>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">加载中…</p>
        )}
        <button className="btn tiny ghost" onClick={() => void load()}>
          刷新
        </button>
      </section>

      <section className="card">
        <h3>
          长期偏好 <span className="tag">user_id = {userId}</span>
        </h3>

        <div className="toolbar">
          <input
            className="text-input"
            placeholder="键（如 语言偏好）"
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          <input
            className="text-input grow"
            placeholder="值（如 中文回答）"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void put()
            }}
          />
          <button className="btn primary" onClick={() => void put()} disabled={!key.trim() || !value.trim()}>
            写入
          </button>
        </div>

        {prefs.length === 0 ? (
          <p className="empty">还没有长期偏好。</p>
        ) : (
          <ul className="pref-list">
            {prefs.map((p) => (
              <li key={p.key}>
                <div className="pref-main">
                  <strong>{p.key}</strong>
                  <span>{p.value ?? ''}</span>
                  {p.updated_at ? <span className="muted small">{p.updated_at}</span> : null}
                </div>
                <button className="btn tiny ghost" onClick={() => void drop(p.key)}>
                  删除
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
