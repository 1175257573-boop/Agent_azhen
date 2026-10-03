import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { act } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import App from '../App'
import { installFetch, type FetchHandler } from '../test/setup'
import { StoreProvider } from '../store'
import type { AtlasDesktopBridge } from '../desktop'

/**
 * 关窗询问的回归测试。
 *
 * 这组测试盯的是一个真实事故：关窗弹窗点掉之后，关闭键从此再也没反应。
 * 根因是「按 Esc / 点遮罩」那条分支没有回传给主进程，主进程手里的锁
 * 一直没解开，于是之后每次点关闭键都被 if (closeDialogOpen) return 挡掉。
 *
 * 所以断言的重点不是「弹窗长什么样」，而是**三条路径都必须回传**。
 */

interface BridgeStub {
  trigger: () => void
  closeDecision: ReturnType<typeof vi.fn>
}

function stubBridge(): BridgeStub {
  let cb: (() => void) | null = null
  const closeDecision = vi.fn(async () => true)
  const bridge = {
    isDesktop: true,
    onLog: () => undefined,
    onReady: () => undefined,
    onFailed: () => undefined,
    onBackendExit: () => undefined,
    onConfirmClose: (fn: () => void) => {
      cb = fn
      return () => {
        cb = null
      }
    },
    restart: async () => undefined,
    revealLog: async () => undefined,
    openExternal: async () => undefined,
    closeDecision,
  } as unknown as AtlasDesktopBridge
  ;(window as unknown as { atlasDesktop?: AtlasDesktopBridge }).atlasDesktop = bridge
  return { trigger: () => act(() => cb?.()), closeDecision }
}

function mountApp(handler?: FetchHandler) {
  const base: FetchHandler = (path) => {
    if (path === '/api/health') {
      return { json: { ok: true, python: '3.12', langchain: '1.4', platform: 'win', memory: {} } }
    }
    return { json: {} }
  }
  installFetch(handler ?? base)
  render(
    <StoreProvider>
      <App />
    </StoreProvider>,
  )
}

afterEach(() => {
  delete (window as unknown as { atlasDesktop?: AtlasDesktopBridge }).atlasDesktop
})

async function openCloseDialog() {
  await screen.findByText('要最小化，还是退出？')
}

it('关窗询问：点「最小化」回传 minimize', async () => {
  const bridge = stubBridge()
  mountApp()
  bridge.trigger()
  await openCloseDialog()

  await userEvent.click(screen.getByRole('button', { name: '最小化到托盘' }))
  await waitFor(() => expect(bridge.closeDecision).toHaveBeenCalledWith('minimize'))
})

it('关窗询问：点「退出」回传 quit', async () => {
  const bridge = stubBridge()
  mountApp()
  bridge.trigger()
  await openCloseDialog()

  await userEvent.click(screen.getByRole('button', { name: '退出（同时停止后端）' }))
  await waitFor(() => expect(bridge.closeDecision).toHaveBeenCalledWith('quit'))
})

it('关窗询问：Esc / 点遮罩也必须回传 cancel（否则关闭键永久失灵）', async () => {
  const bridge = stubBridge()
  const { container } = mountAppRendered()
  bridge.trigger()
  await openCloseDialog()

  // 点遮罩空白处 = dismiss
  await userEvent.click(container.querySelector('.confirm-mask') as Element)
  await waitFor(() => expect(bridge.closeDecision).toHaveBeenCalledWith('cancel'))

  // 关键回归点：主进程靠这个回传解锁。少一次，关闭键就再也点不动了。
  expect(bridge.closeDecision).toHaveBeenCalledTimes(1)
})

// 单独抽出来只是为了拿到 container（遮罩元素在 container 里）
function mountAppRendered() {
  const base: FetchHandler = (path) => {
    if (path === '/api/health') {
      return { json: { ok: true, python: '3.12', langchain: '1.4', platform: 'win', memory: {} } }
    }
    return { json: {} }
  }
  installFetch(base)
  const { container } = render(
    <StoreProvider>
      <App />
    </StoreProvider>,
  )
  return { container }
}
