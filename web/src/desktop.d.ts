/**
 * 桌面壳（Electron preload）暴露的桥。
 *
 * 只有在桌面端运行时 window.atlasDesktop 才存在；浏览器里直接访问是 undefined，
 * 所以每个方法调用前都要判空（web/ 与 desktop/ 共用同一套前端代码）。
 */
export interface AtlasDesktopBridge {
  isDesktop: true
  /** 订阅主进程日志，返回取消订阅的函数 */
  onLog: (cb: (line: string) => void) => void
  onReady: (cb: (payload: { baseUrl: string }) => void) => void
  onFailed: (cb: (payload: { code: string; backendRoot?: string; packaged?: boolean }) => void) => void
  onBackendExit: (cb: (payload: { code: number | null; signal: string | null }) => void) => void
  /** 点关闭键时主进程来问：要最小化还是要退出 */
  onConfirmClose: (cb: () => void) => void
  restart: () => Promise<unknown>
  revealLog: () => Promise<unknown>
  openExternal: (url: string) => Promise<unknown>
  /** 回传关窗选择 */
  closeDecision: (decision: 'minimize' | 'quit' | 'cancel') => Promise<unknown>
}

declare global {
  interface Window {
    atlasDesktop?: AtlasDesktopBridge
  }
}

export {}
