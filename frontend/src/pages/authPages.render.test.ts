import { readFileSync } from 'node:fs'
import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'

// 登录页、首次改密页在 2026-08-25 改过版式（左右分栏）。纯函数测试覆盖不到
// JSX 结构，这里用 react-test-renderer 真挂载一遍：hook 顺序、必填 props、
// 事件绑定有问题会直接炸在这。

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
