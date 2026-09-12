import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi, beforeEach } from 'vitest'

// AcceptInvitePage 是唯一绕开 AuthProvider 直接挂载的页面（EP-03 第二阶段，
// 服务的是压根没有账号/会话的访客）。这里只验证三种关键渲染态：加载中、
// 邀请已失效（此处用"已过期"覆盖）、邀请仍有效时的设密表单——版式事故这类
// 纯函数测试看不见的问题，与 authPages.render.test.ts 同一条理由。

const previewInvitation = vi.fn()
const acceptInvitation = vi.fn()

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {
    status: number
    detail: unknown
    constructor(status: number, message: string, code?: string, category?: string, errorId?: string, detail?: unknown) {
      super(message)
      this.status = status
      this.detail = detail
    }
  },
  previewInvitation: (...args: unknown[]) => previewInvitation(...args),
  acceptInvitation: (...args: unknown[]) => acceptInvitation(...args),
}))

async function render(pathname: string) {
  // vitest 跑在 environment: 'node' 下没有全局 window（与 authPages.render.test.ts
  // 同一条已知限制）；AcceptInvitePage 挂载时用 window.location.pathname 取 token，
  // 用 vi.stubGlobal 补一个最小 window 而不是 Object.defineProperty（后者要求
  // window 已存在才能改属性，这里 window 压根不存在）。
  vi.stubGlobal('window', { location: { pathname, href: '' } })
  const { default: AcceptInvitePage } = await import('./AcceptInvitePage')
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(AcceptInvitePage))
  })
  return renderer
}

function texts(tree: TestRenderer.ReactTestRenderer): string {
  return JSON.stringify(tree.toJSON())
}

beforeEach(() => {
  vi.resetModules()
  vi.unstubAllGlobals()
  previewInvitation.mockReset()
  acceptInvitation.mockReset()
})

describe('邀请接受页', () => {
  it('预览请求未返回前显示加载态', async () => {
    previewInvitation.mockReturnValue(new Promise(() => {})) // 永不 resolve
    const tree = await render('/invite/tok123')
    expect(texts(tree)).toContain('正在校验邀请链接')
  })

  it('邀请已过期时展示明确原因，不出现设密表单', async () => {
    previewInvitation.mockResolvedValue({
      status: 'expired', username: 'zhangsan', display_name: '张三',
      org_name: null, team_name: null, role_name: null, expires_at: 0,
    })
    const tree = await act(async () => render('/invite/tok123'))
    await act(async () => {})
    expect(texts(tree)).toContain('已过期')
    expect(texts(tree)).not.toContain('invite-password')
  })

  it('邀请有效时渲染设密表单', async () => {
    previewInvitation.mockResolvedValue({
      status: 'pending', username: 'zhangsan', display_name: '张三',
      org_name: '默认组织', team_name: null, role_name: null, expires_at: 9999999999,
    })
    const tree = await render('/invite/tok123')
    await act(async () => {})
    const json = texts(tree)
    expect(json).toContain('invite-password')
    expect(json).toContain('zhangsan')
  })
})
