import { readFileSync } from 'node:fs'
import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'

// 登录页、首次改密页、成员与团队页在 2026-08-25 改过版式（左右分栏 / 两栏+弹窗）。
// 纯函数测试覆盖不到 JSX 结构，这里用 react-test-renderer 真挂载一遍：
// hook 顺序、必填 props、事件绑定有问题会直接炸在这。

const mockAuth = {
  user: { id: 'u1', username: 'demo2', display_name: '演示账号' },
  refresh: vi.fn(async () => {}),
  logout: vi.fn(async () => {}),
}

vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth }))
// 焦点圈定要摸 document.body，而 vitest 跑在 environment: 'node' 下没有 DOM。
// 这里只验版式结构，焦点行为不在本文件的射程内。
vi.mock('../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))
vi.mock('../api', () => ({
  ApiError: class ApiError extends Error { status = 0 },
  login: vi.fn(async () => {}),
  changePassword: vi.fn(async () => {}),
  api: {
    listWorkspaces: vi.fn(async () => (
      { items: [{ id: 'w1', name: '制作一组', status: 'active', member_count: 2, project_count: 1 }] }
    )),
    listUsers: vi.fn(async () => ({
      items: [{
        id: 'u1', username: 'demo2', display_name: '演示账号', status: 'active',
        is_system_admin: false, must_change_password: false, created_at: 0,
        last_login_at: null, workspaces: [{ id: 'w1', name: '制作一组', role: 'production' }],
      }],
    })),
    createWorkspace: vi.fn(async () => ({})),
    createUser: vi.fn(async () => ({})),
    updateWorkspace: vi.fn(async () => ({})),
    updateWorkspaceMember: vi.fn(async () => ({})),
    removeWorkspaceMember: vi.fn(async () => ({})),
    updateUser: vi.fn(async () => ({})),
  },
}))

/** 把渲染树拍平成一个 class 名集合，用来断言版式骨架在。 */
function classNames(tree: TestRenderer.ReactTestRenderer): Set<string> {
  const found = new Set<string>()
  const walk = (node: unknown) => {
    if (!node || typeof node !== 'object') return
    const item = node as { props?: Record<string, unknown>; children?: unknown[] }
    const cls = item.props?.className
    if (typeof cls === 'string') cls.split(/\s+/).filter(Boolean).forEach((c) => found.add(c))
    ;(item.children ?? []).forEach(walk)
  }
  walk(tree.toJSON() as unknown)
  return found
}

async function render(component: React.ComponentType) {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(component))
  })
  return renderer
}

describe('登录页左右分栏', () => {
  it('挂载出品牌栏与表单两栏', async () => {
    const { default: LoginPage } = await import('./LoginPage')
    const tree = await render(LoginPage)
    const cls = classNames(tree)
    expect(cls.has('auth-shell')).toBe(true)
    expect(cls.has('auth-aside')).toBe(true)
    expect(cls.has('auth-main')).toBe(true)
    expect(cls.has('auth-panel')).toBe(true)
  })
})

describe('首次改密页', () => {
  it('与登录页同一套外壳，三个字段各自独立成行', async () => {
    const { default: ForcePasswordChangePage } = await import('./ForcePasswordChangePage')
    const tree = await render(ForcePasswordChangePage)
    const cls = classNames(tree)
    expect(cls.has('auth-shell')).toBe(true)
    expect(cls.has('auth-aside')).toBe(true)
    // 版式事故的根因是 input 嵌在 label 里、跟标签挤在同一行；
    // 现在每个字段都是 .login-field 包一层 label + input 兄弟节点。
    const inputs: unknown[] = []
    const walk = (node: unknown) => {
      if (!node || typeof node !== 'object') return
      const item = node as { type?: string; children?: unknown[] }
      if (item.type === 'input') inputs.push(item)
      ;(item.children ?? []).forEach(walk)
    }
    walk(tree.toJSON() as unknown)
    expect(inputs).toHaveLength(3)
  })
})

describe('成员与团队', () => {
  it('默认停在成员栏，两个新建动作是按钮而不是常驻表单', async () => {
    const { default: TeamAdminPage } = await import('./TeamAdminPage')
    const tree = await render(TeamAdminPage)
    const cls = classNames(tree)
    expect(cls.has('team-admin-tabs')).toBe(true)
    expect(cls.has('team-admin-bar-actions')).toBe(true)
    // 弹窗未打开时不应该有 backdrop
    expect(cls.has('evidence-backdrop')).toBe(false)

    const labels: string[] = []
    const walk = (node: unknown) => {
      if (!node || typeof node !== 'object') return
      const item = node as { type?: string; children?: unknown[] }
      if (item.type === 'button') {
        const text = (item.children ?? []).filter((c) => typeof c === 'string').join('')
        if (text) labels.push(text)
      }
      ;(item.children ?? []).forEach(walk)
    }
    walk(tree.toJSON() as unknown)
    expect(labels).toContain('新建团队')
    expect(labels).toContain('创建账号')
  })

  it('成员行不再内嵌下拉框：团队是只读徽章，改角色走弹窗', async () => {
    const { default: TeamAdminPage } = await import('./TeamAdminPage')
    const tree = await render(TeamAdminPage)

    // 表格里一个 select 都不该有——原来每行内嵌两个下拉框，正是拥挤的来源
    const selectsInTable = tree.root.findAllByType('select')
    expect(selectsInTable).toHaveLength(0)
    expect(classNames(tree).has('team-chip')).toBe(true)

    // 「管理」把编辑挪进弹窗，那里才铺得开
    const manage = tree.root
      .findAllByType('button')
      .find((node) => node.props.className === 'text-action')
    expect(manage).toBeDefined()
    await act(async () => { manage!.props.onClick() })
    expect(classNames(tree).has('member-team-row')).toBe(true)
    expect(tree.root.findAllByType('select').length).toBeGreaterThan(0)
  })

  it('破坏性操作走站内弹窗，不再弹浏览器原生框', async () => {
    // window.confirm / prompt 在移动端是系统弹窗，跟站内完全两套视觉，
    // 也写不下「停用会波及多少人」这种上下文。
    const source = readFileSync(new URL('./TeamAdminPage.tsx', import.meta.url), 'utf-8')
    const calls = source.match(/window\.(confirm|prompt|alert)\(/g)
    expect(calls).toBeNull()

    const { default: TeamAdminPage } = await import('./TeamAdminPage')
    const tree = await render(TeamAdminPage)
    const disable = tree.root
      .findAllByType('button')
      .find((node) => node.children.includes('禁用'))
    expect(disable).toBeDefined()
    await act(async () => { disable!.props.onClick() })
    // DecisionDialog 复用 .impact-dialog，弹出来才算接上了
    expect(classNames(tree).has('impact-dialog')).toBe(true)
  })

  it('操作列的 flex 容器挂在 td 里面，不是 td 本身', async () => {
    // 2026-08-25：给 <td> 设了 display:flex，那一列就被摘出表格布局，
    // 行高和下边线跟其它列对不齐（横线断在操作列前面）。
    const { default: TeamAdminPage } = await import('./TeamAdminPage')
    const tree = await render(TeamAdminPage)
    const holders = tree.root.findAll(
      (node) => node.props?.className === 'team-admin-actions',
    )
    expect(holders.length).toBeGreaterThan(0)
    for (const holder of holders) expect(holder.type).toBe('div')
  })
})

describe('整屏页面不能落进侧栏留出的槽里', () => {
  // 2026-08-25：登录页左边空出一条 224px 的纸色带。根因是 #root 上那条
  // `padding-left: 224px` —— 它是给固定侧栏让位的，但登录页根本不挂侧栏。
  // 这类问题渲染测试看不见（DOM 是对的，错的是全局 CSS），所以在这里锁一道。
  const css = readFileSync(new URL('../index.css', import.meta.url), 'utf-8')

  /** 收集所有把 #root 的左内边距归零的选择器。 */
  const exempted = new Set<string>()
  for (const block of css.split('}')) {
    const [selector, body = ''] = block.split('{')
    if (!selector.includes('#root') || !/padding-left:\s*0/.test(body)) continue
    for (const cls of selector.matchAll(/\.[a-z0-9-]+/g)) exempted.add(cls[0])
  }

  it.each(['.auth-shell', '.auth-loading'])('%s 撤掉了 #root 的侧栏槽', (pageClass) => {
    expect(exempted.has(pageClass)).toBe(true)
  })
})
