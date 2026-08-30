import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// AccountAdminPage 找回了被上一轮「团队/工作空间」退场误删的账号管理入口
// （原 TeamAdminPage 一页两用，团队部分不再重建）。这里用 react-test-renderer
// 真挂载：验证列表渲染、系统管理员与普通账号的档位展示分叉、以及创建/重置
// 密码两个弹窗的开合，不落进「纯函数全绿但版式散架」的老问题。

// vi.mock 工厂里立即用到 mockApi（不是像别处那样延迟到嵌套闭包里才访问），
// 必须用 vi.hoisted 让声明真正跟着 vi.mock 一起提到文件顶部，否则会撞
// "Cannot access before initialization"（TDZ）。
const { mockApi, MockApiError, mockUsers } = vi.hoisted(() => {
  const mockUsers = [
    {
      id: 'user_admin', username: 'lnuyasha', display_name: '系统管理员',
      status: 'active', is_system_admin: true, must_change_password: false,
      tier: 'free', quota_period_started_at: 1000, created_at: 1000, last_login_at: 2000,
    },
    {
      id: 'user_free', username: 'demo', display_name: 'demo',
      status: 'active', is_system_admin: false, must_change_password: false,
      tier: 'free', quota_period_started_at: 1000, created_at: 1200, last_login_at: null,
    },
    {
      id: 'user_pro_disabled', username: 'old-hand', display_name: '',
      status: 'disabled', is_system_admin: false, must_change_password: true,
      tier: 'pro', quota_period_started_at: 1000, created_at: 1500, last_login_at: null,
    },
  ]
  const mockApi = {
    listUsers: vi.fn(async () => ({ items: mockUsers })),
    createUser: vi.fn(async () => ({})),
    updateUser: vi.fn(async () => ({})),
  }
  class MockApiError extends Error {
    status = 0
  }
  return { mockApi, MockApiError, mockUsers }
})

vi.mock('../api', () => ({
  api: mockApi,
  ApiError: MockApiError,
}))
// 弹窗焦点圈定要摸 document.body，vitest 跑在 environment: 'node' 下没有 DOM。
vi.mock('../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import AccountAdminPage from './AccountAdminPage'

/** notify() 用 window.setTimeout 定时收起 toast；node 环境没有全局 window，
 *  只装最小 stub（真实定时器），不需要完整 DOM。 */
function installWindowStub() {
  ;(globalThis as { window?: unknown }).window = {
    setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
  }
}
function uninstallWindowStub() {
  delete (globalThis as { window?: unknown }).window
}

async function renderPage() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(AccountAdminPage))
  })
  return renderer
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.filter((c): c is string => typeof c === 'string').join('')
}

describe('账号管理页', () => {
  beforeEach(() => {
    installWindowStub()
    mockApi.listUsers.mockClear()
    mockApi.createUser.mockClear()
    mockApi.updateUser.mockClear()
  })
  afterEach(() => {
    uninstallWindowStub()
  })

  it('拉到列表后按用户渲染行；系统管理员不显示档位下拉框，普通账号显示', async () => {
    const renderer = await renderPage()
    expect(mockApi.listUsers).toHaveBeenCalledTimes(1)

    const tbody = renderer.root.findByType('tbody')
    const rows = tbody.findAllByType('tr')
    expect(rows).toHaveLength(3)

    const tags = renderer.root.findAll((node) => node.props.className === 'account-admin-tag')
    expect(tags).toHaveLength(1)
    expect(textOf(tags[0])).toBe('系统管理员')

    // 两个非管理员账号（free + pro）各自一个档位下拉框，管理员账号没有。
    const selects = renderer.root.findAllByType('select')
    expect(selects).toHaveLength(2)

    const unlimited = renderer.root.findAll(
      (node) => node.props.className === 'account-admin-tier-unlimited',
    )
    expect(unlimited).toHaveLength(1)
    expect(textOf(unlimited[0])).toContain('不限')
  })

  it('停用的账号行整体降权样式，禁用按钮文案是"启用"', async () => {
    const renderer = await renderPage()
    const disabledRow = renderer.root.findAll(
      (node) => node.type === 'tr' && node.props.className === 'account-admin-row-disabled',
    )
    expect(disabledRow).toHaveLength(1)
    const enableButtons = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '启用',
    )
    expect(enableButtons).toHaveLength(1)
  })

  it('点击"创建账号"弹出表单弹窗，取消后关闭', async () => {
    const renderer = await renderPage()
    const openBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '创建账号',
    )[0]
    act(() => { openBtn.props.onClick() })
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(1)

    const cancelBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '取消',
    )[0]
    act(() => { cancelBtn.props.onClick() })
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('点击某行的"重置密码"弹出对应用户的表单', async () => {
    const renderer = await renderPage()
    const resetButtons = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '重置密码',
    )
    expect(resetButtons).toHaveLength(3)
    act(() => { resetButtons[1].props.onClick() })
    const heading = renderer.root.findByType('h3')
    expect(textOf(heading)).toContain('demo')
  })

  it('切换某个非管理员账号的档位会带着新 tier 调用 updateUser', async () => {
    const renderer = await renderPage()
    const select = renderer.root.findAllByType('select')[0]
    await act(async () => {
      select.props.onChange({ target: { value: 'pro' } })
    })
    expect(mockApi.updateUser).toHaveBeenCalledWith('user_free', { tier: 'pro' })
  })

  it('点击"设为管理员"直接调用 updateUser，不弹确认框', async () => {
    const renderer = await renderPage()
    const promoteBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '设为管理员',
    )[0]
    await act(async () => {
      promoteBtn.props.onClick()
    })
    expect(mockApi.updateUser).toHaveBeenCalledWith('user_free', { is_system_admin: true })
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })
})
