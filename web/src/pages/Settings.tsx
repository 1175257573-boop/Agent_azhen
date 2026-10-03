import { useCallback, useEffect, useState } from 'react'

import { api } from '../api'
import { useStore } from '../store'
import type { CredentialState, SystemInfo, VerifyOut } from '../types'

export default function Settings() {
  const { mode, provider, userId, role, enableMcp, setConfig } = useStore()
  const [modes, setModes] = useState<Record<string, string>>({})
  const [info, setInfo] = useState<SystemInfo | null>(null)
  const [tools, setTools] = useState<string[]>([])
  const [mcpError, setMcpError] = useState('')

  useEffect(() => {
    void api.modes().then(setModes).catch(() => setModes({}))
    void api.info().then(setInfo).catch(() => setInfo(null))
  }, [])

  const loadTools = useCallback(async () => {
    setMcpError('')
    try {
      const res = await api.mcpTools({ mode, provider, user_id: userId, role, connect: false })
      setTools(res.tools ?? [])
    } catch (err) {
      setTools([])
      setMcpError(err instanceof Error ? err.message : String(err))
    }
  }, [mode, provider, role, userId])

  return (
    <div className="page">
      <div className="page-head">
        <h2>运行配置</h2>
        <span className="muted">这些参数会随每次对话请求一起发给后端，并保存在本机浏览器里（密钥除外）。</span>
      </div>

      <div className="grid-2">
        <section className="card">
          <h3>能力模式</h3>
          <select className="select" value={mode} onChange={(e) => setConfig({ mode: e.target.value })}>
            {Object.keys(modes).length ? (
              Object.entries(modes).map(([key, desc]) => (
                <option key={key} value={key} title={desc}>
                  {key}
                </option>
              ))
            ) : (
              <option value={mode}>{mode}</option>
            )}
          </select>
          <p className="desc">{modes[mode] ?? ''}</p>

          <h3>模型 Provider</h3>
          <select
            className="select"
            value={provider ?? ''}
            onChange={(e) => setConfig({ provider: e.target.value || null })}
          >
            <option value="">自动探测</option>
            {(info?.providers ?? []).map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <p className="desc">当前生效：{info ? `${info.provider} · ${info.model}` : '检测中…'}</p>
        </section>

        <section className="card">
          <h3>身份与权限</h3>
          <label className="field">
            <span>user_id（长期记忆按此隔离）</span>
            <input className="text-input" value={userId} onChange={(e) => setConfig({ userId: e.target.value })} />
          </label>
          <label className="field">
            <span>role</span>
            <select className="select" value={role} onChange={(e) => setConfig({ role: e.target.value })}>
              <option value="admin">admin · 可写文件</option>
              <option value="user">user · 只读</option>
            </select>
          </label>

          <h3>MCP 工具</h3>
          <label className="switch">
            <input
              type="checkbox"
              checked={enableMcp}
              onChange={(e) => {
                setConfig({ enableMcp: e.target.checked })
                if (e.target.checked) void loadTools()
              }}
            />
            <span>接入 MCP Server（会拉起子进程）</span>
          </label>
          <div className="chips">
            {mcpError ? <span className="bad">{mcpError}</span> : null}
            {!mcpError && tools.length === 0 ? <span className="muted">未连接</span> : null}
            {tools.map((t) => (
              <span className="chip static" key={t}>
                {t}
              </span>
            ))}
          </div>
          <button className="btn tiny ghost" onClick={() => void loadTools()}>
            刷新工具列表
          </button>
        </section>
      </div>

      <Credentials />
    </div>
  )
}

// ---------------------------------------------------------------------------
function Credentials() {
  const { toast, confirm } = useStore()
  const [state, setState] = useState<CredentialState | null>(null)
  const [provider, setProvider] = useState('openai')
  const [key, setKey] = useState('')
  const [remember, setRemember] = useState(false)
  const [revealed, setRevealed] = useState<{ provider: string; value: string; left: number } | null>(null)
  const [verify, setVerify] = useState<VerifyOut | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setState(await api.credentials())
    } catch (err) {
      toast('error', `凭据状态加载失败：${err instanceof Error ? err.message : err}`)
    }
  }, [toast])

  useEffect(() => {
    void load()
  }, [load])

  // 明文只活在内存里，到点自动清掉
  useEffect(() => {
    if (!revealed) return
    const timer = window.setInterval(() => {
      setRevealed((prev) => (prev ? { ...prev, left: prev.left - 1 } : prev))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [revealed])

  useEffect(() => {
    if (revealed && revealed.left <= 0) setRevealed(null)
  }, [revealed])

  const save = async () => {
    if (key.trim().length < 8) {
      toast('error', '密钥太短，至少 8 位')
      return
    }
    setBusy(true)
    try {
      setState(await api.saveCredential(provider, key.trim(), remember))
      setKey('')
      toast('ok', `${provider} 已保存并生效`)
    } catch (err) {
      toast('error', `保存失败：${err instanceof Error ? err.message : err}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name: string) => {
    try {
      setState(await api.deleteCredential(name))
      toast('ok', `${name} 已清除`)
    } catch (err) {
      toast('error', `清除失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const reveal = async (name: string) => {
    const ok = await confirm({
      title: '显示完整密钥？',
      message: `即将显示 ${name} 的完整密钥。`,
      detail: '密钥会在界面上限时展示，到点自动隐藏。截图或录屏可能把它带出去。',
      confirmText: '显示',
      cancelText: '取消',
    })
    if (!ok) return
    try {
      const res = await api.revealCredential(name)
      setRevealed({ provider: res.provider, value: res.api_key, left: res.expires_in || 15 })
    } catch (err) {
      toast('error', `查看失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const probe = async () => {
    setVerify(null)
    try {
      setVerify(await api.verifyCredential(provider))
    } catch (err) {
      toast('error', `连通性测试失败：${err instanceof Error ? err.message : err}`)
    }
  }

  return (
    <section className="card">
      <h3>
        密钥管理 <span className="tag">仅本机可访问</span>
      </h3>

      {state ? (
        <>
          <div className="kv">
            <span>当前生效</span>
            <code>{state.active_provider}</code>
          </div>
          <div className="kv">
            <span>凭据文件</span>
            <span className="muted">{state.file_exists ? state.storage_path : '尚未写入磁盘'}</span>
          </div>
          {state.storage_note ? <p className="desc">{state.storage_note}</p> : null}

          <ul className="cred-list">
            {state.items.map((item) => (
              <li key={item.provider}>
                <div className="cred-main">
                  <strong>{item.label}</strong>
                  <code>{item.masked || '—'}</code>
                  <span className={`tag ${item.configured ? 'ok' : 'bad'}`}>
                    {item.origin === 'env' ? '环境变量' : item.origin === 'runtime' ? '本次注入' : '未配置'}
                  </span>
                  {item.persistent ? <span className="tag">已记住</span> : null}
                  {item.shadowed ? <span className="tag warn">已覆盖环境变量</span> : null}
                </div>
                <div className="btn-row">
                  <button className="btn tiny" onClick={() => void reveal(item.provider)} disabled={!item.configured}>
                    查看
                  </button>
                  <button className="btn tiny ghost" onClick={() => void remove(item.provider)} disabled={!item.configured}>
                    清除
                  </button>
                </div>
              </li>
            ))}
          </ul>

          {revealed ? (
            <div className="reveal">
              <div className="kv">
                <span>{revealed.provider} 完整密钥</span>
                <span className="bad">{revealed.left}s 后自动隐藏</span>
              </div>
              <code className="secret">{revealed.value}</code>
            </div>
          ) : null}
        </>
      ) : (
        <p className="muted">加载中…</p>
      )}

      <div className="cred-form">
        <label className="field">
          <span>Provider</span>
          <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)}>
            {(state?.items ?? []).map((i) => (
              <option key={i.provider} value={i.provider}>
                {i.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field grow">
          <span>API Key</span>
          <input
            className="text-input"
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="粘贴密钥，形如 sk-…"
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
        </label>
        <label className="switch">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <span>记住到本机（重启后仍生效）</span>
        </label>
        <div className="btn-row">
          <button className="btn primary" onClick={() => void save()} disabled={busy || !key.trim()}>
            保存并启用
          </button>
          <button className="btn" onClick={() => void probe()} disabled={busy}>
            测试连接
          </button>
        </div>
      </div>

      {verify ? (
        <div className={`verify ${verify.ok ? 'ok' : 'bad'}`}>
          <div className="kv">
            <span>
              {verify.provider} · {verify.model}
            </span>
            <span>{verify.latency_ms} ms</span>
          </div>
          <p>{verify.message}</p>
          {verify.sample ? <pre className="code">{verify.sample}</pre> : null}
        </div>
      ) : null}

      {state?.notes?.length ? (
        <details className="notes">
          <summary>保密说明</summary>
          <ul>
            {state.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  )
}
