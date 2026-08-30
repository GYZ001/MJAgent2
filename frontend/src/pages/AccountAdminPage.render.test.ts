import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// AccountAdminPage 是移动端优先的卡片式重做（2026-08-30）：原表格版操作按钮
// 在窄屏下被挤出视口（横向溢出），这里用 react-test-renderer 真挂载验证卡片
// 渲染、系统管理员与普通账号的档位展示分叉、创建/重置密码/自删三个弹窗的
// 开合，以及删除（管理员软删 vs 本人自删）两条路径不落进「纯函数全绿但版式
// 散架」的老问题。

// vi.mock 工厂里立即用到 mock* 变量（不是像别处那样延迟到嵌套闭包里才访问），
// 必须用 vi.hoisted 让声明真正跟着 vi.mock 一起提到文件顶部，否则会撞
// "Cannot access before initialization"（TDZ）。
const { mockApi, mockMe, mockDeleteMyAccount, MockApiError, mockUsers, mockDeletedUsers } = vi.hoisted(() => {
  const mockUsers = [
    {
      id: 'user_admin', username: 'lnuyasha', display_name: '系统管理员',
      status: 'active', is_system_admin: true, must_change_password: false,
      tier: 'free', quota_period_started_at: 1000, created_at: 1000, last_login_at: 2000, deleted_at: null,
    },
    {
      id: 'user_free', username: 'demo', display_name: 'demo',
      status: 'active', is_system_admin: false, must_change_password: false,
      tier: 'free', quota_period_started_at: 1000, created_at: 1200, last_login_at: null, deleted_at: null,
    },
    {
      id: 'user_pro_disabled', username: 'old-hand', display_name: '',
      status: 'disabled', is_system_admin: false, must_change_password: true,
      tier: 'pro', quota_period_started_at: 1000, created_at: 1500, last_login_at: null, deleted_at: null,
    },
  ]
  const mockDeletedUsers = [
    {
      id: 'user_gone', username: 'left-corp', display_name: '', status: 'disabled',
      is_system_admin: false, must_change_password: false, tier: 'free',
      quota_period_started_at: 1000, created_at: 900, last_login_at: null,
      deleted_at: 5000, purge_at: 5000 + 30 * 86400, retention_seconds_remaining: 29 * 86400,
    },
  ]
  const mockApi = {
    listUsers: vi.fn(async () => ({ items: mockUsers })),
    listDeletedUsers: vi.fn(async () => ({ items: mockDeletedUsers })),
    createUser: vi.fn(async () => ({})),
    updateUser: vi.fn(async () => ({})),
    deleteUser: vi.fn(async () => ({
      ok: true, deleted_user_id: 'user_free', deleted_at: 0, purge_at: 0,
      projects: { soft_deleted: [], soft_deleted_count: 0, failed: [] },
    })),
    restoreUser: vi.fn(async () => ({
      ok: true, restored_user_id: 'user_gone', projects: { restored: [], restored_count: 0, failed: [] },
    })),
    grantVideoAddon: vi.fn(async () => ({
      user_id: 'user_free', packages: 1, package_seconds: 600, price_cny: 199,
      attempt_key: 'k', seconds_granted: 600, idempotent_replay: false, addon_balance_s: 600,
    })),
  }
  const mockMe = vi.fn(async () => ({
    user: { id: 'user_admin', username: 'lnuyasha', display_name: '系统管理员' },
    is_system_admin: true, must_change_password: false,
  }))
  const mockDeleteMyAccount = vi.fn(async (confirm: boolean) => {
    if (!confirm) {
      throw new MockApiError(422, '将删除 2 个项目', undefined, undefined, undefined, {
        code: 'confirmation_required', message: '此操作将立即彻底删除你的账号与其下 2 个项目的全部数据，不可恢复。', project_count: 2,
      })
    }
    return { ok: true, deleted_user_id: 'user_admin', projects: { purged: [], purged_count: 0 } }
  })
  class MockApiError extends Error {
    constructor(
      public status: number, message: string, public code?: string, public category?: string,
      public errorId?: string, public detail?: unknown,
    ) { super(message) }
  }
  return { mockApi, mockMe, mockDeleteMyAccount, MockApiError, mockUsers, mockDeletedUsers }
})

vi.mock('../api', () => ({
  api: mockApi,
  ApiError: MockApiError,
  me: mockMe,
  deleteMyAccount: mockDeleteMyAccount,
}))
// 弹窗焦点圈定要摸 document.body，vitest 跑在 environment: 'node' 下没有 DOM。
vi.mock('../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import AccountAdminPage from './AccountAdminPage'

/** notify() 用 window.setTimeout 定时收起 toast；自删成功后也用它延迟
 *  reload。node 环境没有全局 window，只装最小 stub（真实定时器）+ location
 *  桩（reload 是 no-op），不需要完整 DOM。 */
function installWindowStub() {
  ;(globalThis as { window?: unknown }).window = {
    setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
    location: { reload: () => undefined },
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

function hasClass(node: TestRenderer.ReactTestInstance, cls: string): boolean {
  const raw = node.props.className
  return typeof raw === 'string' && raw.split(/\s+/).includes(cls)
}

describe('账号管理页', () => {
  beforeEach(() => {
    installWindowStub()
    Object.values(mockApi).forEach((fn) => fn.mockClear())
    mockMe.mockClear()
    mockDeleteMyAccount.mockClear()
  })
  afterEach(() => {
    uninstallWindowStub()
  })

  it('拉到列表后按用户渲染卡片；系统管理员不显示档位下拉框，普通账号显示', async () => {
    const renderer = await renderPage()
    expect(mockApi.listUsers).toHaveBeenCalledTimes(1)

    const cards = renderer.root.findAll((node) => node.type === 'article' && hasClass(node, 'account-card'))
    expect(cards).toHaveLength(mockUsers.length)

    const tags = renderer.root.findAll((node) => node.props.className === 'account-admin-tag')
    expect(tags).toHaveLength(1)
    expect(textOf(tags[0])).toBe('系统管理员')

    // 两个非管理员账号（free + pro）各自一个档位下拉框，管理员账号没有。
    const selects = renderer.root.findAllByType('select')
    expect(selects).toHaveLength(2)

    const unlimited = renderer.root.findAll((node) => node.props.className === 'account-admin-tier-unlimited')
    expect(unlimited).toHaveLength(1)
    expect(textOf(unlimited[0])).toContain('不限')
  })

  it('停用的账号卡片整体降权样式，禁用按钮文案是"启用"', async () => {
    const renderer = await renderPage()
    const disabledCards = renderer.root.findAll(
      (node) => node.type === 'article' && hasClass(node, 'account-card-disabled'),
    )
    expect(disabledCards).toHaveLength(1)
    const enableButtons = renderer.root.findAll((node) => node.type === 'button' && textOf(node) === '启用')
    expect(enableButtons).toHaveLength(1)
  })

  it('点击"创建账号"弹出表单弹窗，取消后关闭', async () => {
    const renderer = await renderPage()
    const openBtn = renderer.root.findAll((node) => node.type === 'button' && textOf(node) === '创建账号')[0]
    act(() => { openBtn.props.onClick() })
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(1)

    const cancelBtn = renderer.root.findAll((node) => node.type === 'button' && textOf(node) === '取消')[0]
    act(() => { cancelBtn.props.onClick() })
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('点击某张卡片的"重置密码"弹出对应用户的表单', async () => {
    const renderer = await renderPage()
    const resetButtons = renderer.root.findAll((node) => node.type === 'button' && textOf(node) === '重置密码')
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
    const promoteBtn = renderer.root.findAll((node) => node.type === 'button' && textOf(node) === '设为管理员')[0]
    await act(async () => {
      promoteBtn.props.onClick()
    })
    expect(mockApi.updateUser).toHaveBeenCalledWith('user_free', { is_system_admin: true })
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('点击他人卡片的"删除（移入回收站）"先弹确认框，确认后才调用 deleteUser', async () => {
    // 管理员删他人账号会级联该账号名下全部项目——爆炸半径比删单个项目大一个
    // 量级，且卡片布局下按钮换行排布、窄屏误触概率更高，所以必须经确认。它可逆
    // （30 天回收站），因此只要一次确认，不像自删那样要求打对用户名。
    const renderer = await renderPage()
    const deleteBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '删除（移入回收站）',
    )[0]
    await act(async () => {
      deleteBtn.props.onClick()
    })
    // 关键：这一刻不能已经删掉了。
    expect(mockApi.deleteUser).not.toHaveBeenCalled()
    const dialogs = renderer.root.findAllByProps({ role: 'dialog' })
    expect(dialogs).toHaveLength(1)

    const confirmBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '移入回收站',
    )[0]
    await act(async () => {
      confirmBtn.props.onClick()
    })
    expect(mockApi.deleteUser).toHaveBeenCalledWith('user_free')
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('确认框点"取消"不删除任何账号', async () => {
    const renderer = await renderPage()
    const deleteBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '删除（移入回收站）',
    )[0]
    await act(async () => {
      deleteBtn.props.onClick()
    })
    const cancelBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '取消',
    )[0]
    await act(async () => {
      cancelBtn.props.onClick()
    })
    expect(mockApi.deleteUser).not.toHaveBeenCalled()
    expect(renderer.root.findAllByProps({ role: 'dialog' })).toHaveLength(0)
  })

  it('本人卡片没有管理员软删按钮，只有危险区的自删入口；点击后预检并要求打对用户名才能确认', async () => {
    const renderer = await renderPage()
    // user_admin === mockMe 返回的当前登录用户，卡片里不应出现"删除（移入回收站）"。
    const dangerBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node) === '彻底删除我的账号',
    )
    expect(dangerBtn).toHaveLength(1)

    await act(async () => {
      dangerBtn[0].props.onClick()
    })
    expect(mockDeleteMyAccount).toHaveBeenCalledWith(false)
    const dialog = renderer.root.findAllByProps({ role: 'dialog' })
    expect(dialog).toHaveLength(1)
    expect(textOf(renderer.root.findByType('h3'))).toBe('彻底删除我的账号')

    const confirmBtn = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node).includes('彻底删除，不可恢复'),
    )[0]
    expect(confirmBtn.props.disabled).toBe(true) // 用户名还没打对，确认键锁着

    // 卡片里的显示名输入框也是 <input>，必须限定在弹窗子树内查找，避免歧义。
    const input = dialog[0].findByType('input')
    await act(async () => {
      input.props.onChange({ target: { value: 'lnuyasha' } })
    })
    const confirmAfter = renderer.root.findAll(
      (node) => node.type === 'button' && textOf(node).includes('彻底删除，不可恢复'),
    )[0]
    expect(confirmAfter.props.disabled).toBe(false)

    await act(async () => {
      confirmAfter.props.onClick()
    })
    expect(mockDeleteMyAccount).toHaveBeenCalledWith(true)
  })
})
