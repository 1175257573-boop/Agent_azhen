import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../api'
import { useStore } from '../store'
import type { ReviewJobDetail, ReviewJobSummary, ReviewState } from '../types'

const POLL_MS = 2000

const STATE_LABEL: Record<ReviewState, string> = {
  queued: '排队中',
  running: '评审中',
  done: '已完成',
  failed: '失败',
}

/**
 * 把 markdown 渲染成可读 HTML。
 *
 * 只支持标题/列表/粗体/行内码/引用——够用且不引三方库。
 * **必须先转义再拼标签**：`dangerouslySetInnerHTML` 之前不做转义，
 * 仓库 README 里的 `<script>` 就会变成真实执行的脚本（XSS）。
 */
function renderMarkdown(md: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const inline = (s: string) =>
    esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  const out: string[] = []
  let inList = false
  for (const raw of md.split('\n')) {
    const line = raw.trimEnd()
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    const li = /^\s*[-*]\s+(.*)$/.exec(line)
    if (li) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${inline(li[1])}</li>`)
      continue
    }
    if (inList) {
      out.push('</ul>')
      inList = false
    }
    if (!line.trim()) continue
    if (h) {
      const lv = Math.min(h[1].length + 1, 5)
      out.push(`<h${lv}>${inline(h[2])}</h${lv}>`)
      continue
    }
    if (/^>\s?/.test(line)) {
      out.push(`<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`)
      continue
    }
    out.push(`<p>${inline(line)}</p>`)
  }
  if (inList) out.push('</ul>')
  return out.join('\n')
}

export default function Review() {
  const { toast } = useStore()
  const [target, setTarget] = useState('')
  const [withModules, setWithModules] = useState(true)
  const [codeLines, setCodeLines] = useState(0)
  const [noJudge, setNoJudge] = useState(false)
  const [jobs, setJobs] = useState<ReviewJobSummary[]>([])
  const [current, setCurrent] = useState<ReviewJobDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const timer = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      setJobs((await api.reviewJobs(10)).jobs)
    } catch (err) {
      toast('error', `任务列表加载失败：${err instanceof Error ? err.message : err}`)
    }
  }, [toast])

  useEffect(() => {
    void refresh()
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [refresh])

  // 只有当前任务还在跑时才轮询：没有任务在跑就别空转
  const polling = current?.state === 'queued' || current?.state === 'running'
  useEffect(() => {
    if (!polling || !current) return
    timer.current = window.setInterval(async () => {
      try {
        const next = await api.reviewJob(current.id)
        setCurrent(next)
        void refresh()
        if (next.state === 'done' || next.state === 'failed') {
          if (timer.current) window.clearInterval(timer.current)
          if (next.state === 'done') toast('ok', `评审完成，用时 ${next.elapsed_s}s`)
        }
      } catch (err) {
        toast('error', `轮询失败：${err instanceof Error ? err.message : err}`)
        if (timer.current) window.clearInterval(timer.current)
      }
    }, POLL_MS)
    return () => {
      if (timer.current) window.clearInterval(timer.current)
    }
  }, [polling, current, refresh, toast])

  const submit = async () => {
    const t = target.trim()
    if (!t || busy) return
    setBusy(true)
    setCurrent(null)
    try {
      const job = await api.submitReview({
        target: t,
        with_modules: withModules,
        module_top_n: 9,
        module_code_lines: codeLines,
        no_judge: noJudge,
      })
      setCurrent({ ...job, markdown: '' })
      void refresh()
    } catch (err) {
      toast('error', `提交失败：${err instanceof Error ? err.message : err}`)
    } finally {
      setBusy(false)
    }
  }

  const openJob = async (id: string) => {
    try {
      setCurrent(await api.reviewJob(id))
    } catch (err) {
      toast('error', `打开失败：${err instanceof Error ? err.message : err}`)
    }
  }

  const copyReport = async () => {
    if (!current?.markdown) return
    try {
      await navigator.clipboard.writeText(current.markdown)
      toast('ok', '已复制 Markdown')
    } catch {
      toast('error', '复制失败：浏览器拒绝了剪贴板访问')
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h2>工程评审</h2>
        <span className="muted">
          填仓库链接或本机目录。评审在后台跑，大仓库要几分钟，完成后自动显示在下面。
        </span>
      </div>

      <section className="card">
        <div className="cred-form">
          <input
            className="text-input grow"
            placeholder="https://github.com/owner/repo  或  D:\repos\project"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void submit()
            }}
          />
          <button className="btn" disabled={busy || !target.trim()} onClick={() => void submit()}>
            {busy ? '提交中…' : '开始评审'}
          </button>
        </div>

        <div className="chips">
          <button
            className={`chip ${withModules ? 'on' : ''}`}
            onClick={() => setWithModules(!withModules)}
          >
            分析模块职责
          </button>
          <button className={`chip ${noJudge ? 'on' : ''}`} onClick={() => setNoJudge(!noJudge)}>
            强制降级（不调模型）
          </button>
          <label className="chip">
            代码行数
            <input
              className="text-input"
              type="number"
              min={0}
              max={200}
              style={{ width: 72 }}
              value={codeLines}
              onChange={(e) => setCodeLines(Number(e.target.value) || 0)}
            />
          </label>
        </div>
        {codeLines > 0 && (
          <p className="muted small">
            每个模块会附上入口文件与主要源文件的首屏。给到 40-60 能明显提升职责判断准确度，
            代价是每个模块多约 1.5k 字符。
          </p>
        )}
      </section>

      {current && (
        <section className="card">
          <div className="page-head">
            <h3>
              {STATE_LABEL[current.state]}
              {current.state === 'done' && current.module_count > 0 && (
                <span className="muted small"> · 已分析 {current.module_count} 个模块</span>
              )}
            </h3>
            <div className="btn-row">
              <span className="muted small">{current.elapsed_s}s</span>
              {current.markdown && (
                <button className="btn tiny ghost" onClick={() => void copyReport()}>
                  复制 Markdown
                </button>
              )}
            </div>
          </div>
          {current.error && <div className="error-box">{current.error}</div>}
          {current.markdown && (
            <div className="report" dangerouslySetInnerHTML={{ __html: renderMarkdown(current.markdown) }} />
          )}
        </section>
      )}

      <section className="card">
        <h3>最近的任务</h3>
        {jobs.length === 0 ? (
          <p className="muted">还没有评审记录。</p>
        ) : (
          <div className="pref-list">
            {jobs.map((j) => (
              <div className="pref-main" key={j.id}>
                <div className="thread-list">
                  <span className="mono" title={j.target}>
                    {j.target}
                  </span>
                  <span className="muted small">
                    {STATE_LABEL[j.state]} · {j.elapsed_s}s
                    {j.module_count ? ` · ${j.module_count} 模块` : ''}
                  </span>
                </div>
                <button className="btn tiny ghost" onClick={() => void openJob(j.id)}>
                  查看
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}