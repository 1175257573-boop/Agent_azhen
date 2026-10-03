import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'

import ConfirmDialog from '../components/ConfirmDialog'

/**
 * 确认弹窗的测试。
 *
 * 重点是**三态**：确认 / 主动取消 / Esc·点遮罩（什么都不做）。
 * 关窗询问就靠第三态区分「按 Esc 取消」和「选了退出」——
 * 早先按布尔做，结果 Esc 会被当成退出，用户想取消反而把应用关了。
 */
const base = { title: '要最小化，还是退出？', detail: '最小化：留在后台。' }

it('渲染标题、说明与两个按钮', () => {
  render(<ConfirmDialog options={base} onResolve={() => {}} />)

  expect(screen.getByText('要最小化，还是退出？')).toBeTruthy()
  expect(screen.getByText('最小化：留在后台。')).toBeTruthy()
  expect(screen.getByRole('button', { name: '确定' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '取消' })).toBeTruthy()
})

it('自定义按钮文案，并按 danger 切换主按钮样式', () => {
  render(
    <ConfirmDialog
      options={{ ...base, confirmText: '删除', cancelText: '算了', danger: true }}
      onResolve={() => {}}
    />,
  )

  const confirmBtn = screen.getByRole('button', { name: '删除' })
  expect(confirmBtn.className).toContain('danger')
  expect(screen.getByRole('button', { name: '算了' })).toBeTruthy()
})

it('点确认 / 点取消分别回传 confirm / cancel', async () => {
  const onResolve = vi.fn()
  render(<ConfirmDialog options={base} onResolve={onResolve} />)

  await userEvent.click(screen.getByRole('button', { name: '确定' }))
  expect(onResolve).toHaveBeenCalledWith('confirm')

  await userEvent.click(screen.getByRole('button', { name: '取消' }))
  expect(onResolve).toHaveBeenCalledWith('cancel')
})

it('Esc 与点遮罩都是 dismiss（什么都不做），不能被当成取消', async () => {
  const onResolve = vi.fn()
  const { container } = render(<ConfirmDialog options={base} onResolve={onResolve} />)

  await userEvent.click(container.querySelector('.confirm-mask') as Element)
  expect(onResolve).toHaveBeenCalledWith('dismiss')

  onResolve.mockClear()
  const card = screen.getByRole('alertdialog')
  card.focus()
  await userEvent.keyboard('{Escape}')
  expect(onResolve).toHaveBeenCalledWith('dismiss')
})

it('options 为 null 时不渲染任何东西', () => {
  const { container } = render(<ConfirmDialog options={null} onResolve={() => {}} />)
  expect(container.firstChild).toBeNull()
})
