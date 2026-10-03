import { useEffect, useRef } from 'react'

/** 三态而不是布尔：Esc / 点遮罩属于「什么都不做」，不能和「主动取消」混为一谈 */
export type ConfirmAction = 'confirm' | 'cancel' | 'dismiss'

export interface ConfirmOptions {
  title: string
  message?: string
  /** 补充说明：讲清后果与影响范围 */
  detail?: string
  confirmText?: string
  cancelText?: string
  /** 危险操作：主按钮用红色 */
  danger?: boolean
  /** 图标语义：question / warning / danger */
  tone?: 'question' | 'warning' | 'danger'
}

interface Props {
  options: ConfirmOptions | null
  onResolve: (action: ConfirmAction) => void
}

/**
 * 全局确认弹窗。
 *
 * 为什么不直接用 window.confirm：它长什么样完全由浏览器/系统决定，
 * 和应用里其他控件对不上，而且没法做「分��说明」这类需要解释后果的场景。
 * 涉及密钥揭示、删除会话、关闭应用这类操作时，一个统一且说清后果的弹窗很重要。
 */
export default function ConfirmDialog({ options, onResolve }: Props) {
  const cardRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!options) return
    // 打开后把焦点放到主按钮上：键盘用户直接回车即可确认，Esc 取消
    const btn = cardRef.current?.querySelector<HTMLButtonElement>('.confirm-actions .btn.primary, .confirm-actions .btn.danger')
    btn?.focus()
  }, [options])

  if (!options) return null

  const { title, message, detail, confirmText = '确定', cancelText = '取消', danger = false, tone } = options
  const toneClass = tone ?? (danger ? 'danger' : 'question')

  return (
    <div
      className="confirm-mask"
      onClick={(e) => {
        if (e.target === e.currentTarget) onResolve('dismiss')
      }}
    >
      <div
        className={`confirm-card ${toneClass}`}
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        // div 默认不可聚焦，没有它就收不到 Esc —— 键盘用户无法取消
        tabIndex={-1}
        ref={cardRef}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            onResolve('dismiss')
          }
        }}
      >
        <div className="confirm-head">
          <span className="confirm-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {toneClass === 'question' ? (
                <>
                  <circle cx="12" cy="12" r="9" />
                  <path d="M9.6 9.2a2.5 2.5 0 1 1 3.4 2.3c-.6.3-1 .9-1 1.6v.4" />
                  <path d="M12 17h.01" />
                </>
              ) : (
                <>
                  <path d="M10.3 3.9 2.4 17.5A1.9 1.9 0 0 0 4 20.4h15.9a1.9 1.9 0 0 0 1.6-2.9L13.7 3.9a1.9 1.9 0 0 0-3.4 0Z" />
                  <path d="M12 9v4.5" />
                  <path d="M12 17h.01" />
                </>
              )}
            </svg>
          </span>
          <h3>{title}</h3>
        </div>

        {message ? <p className="confirm-message">{message}</p> : null}
        {detail ? <p className="confirm-detail">{detail}</p> : null}

        <div className="confirm-actions">
          <button className="btn ghost" onClick={() => onResolve('cancel')}>
            {cancelText}
          </button>
          <button className={`btn ${danger ? 'danger' : 'primary'}`} onClick={() => onResolve('confirm')}>
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  )
}
