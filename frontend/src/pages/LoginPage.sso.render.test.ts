import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// EP-02 第二阶段：登录页的 IdP 入口渲染 + SSO 回跳失败时的错误展示。独立
// 成一个文件而不是塞进 authPages.render.test.ts，是因为这里需要按用例
// 改变 listSsoProviders 的返回值与 AuthContext.ssoLoginError，而那个文件
// 的 mock 是全文件共享的固定值，混在一起会让不相关的用例互相牵连。

const { mockListSsoProviders } = vi.hoisted(() => ({
  mockListSsoProviders: vi.fn(),
}))

let mockSsoLoginError: string | null = null

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({
    user: null,
    ssoLoginError: mockSsoLoginError,
    refresh: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  }),
}))
vi.mock('../api', () => ({
  ApiError: class ApiError extends Error { status = 0 },
  login: vi.fn(async () => {}),
  listSsoProviders: mockListSsoProviders,
  ssoStartUrl: (idpId: string, redirectTo: string) =>
    `/api/auth/sso/${idpId}/start?redirect_to=${encodeURIComponent(redirectTo)}`,
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import LoginPage from './LoginPage'

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.filter((c): c is string => typeof c === 'string').join('')
}

async function renderLoginPage() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(LoginPage))
  })
  return renderer
}

describe('登录页：SSO 入口与回跳失败展示', () => {
  beforeEach(() => {
    mockListSsoProviders.mockReset()
    mockSsoLoginError = null
  })

  it('拉到启用中的 IdP 后为每一个渲染一个登录按钮', async () => {
    mockListSsoProviders.mockResolvedValue({
      items: [
        { id: 'idp_1', name: '公司 Okta', kind: 'oidc' },
        { id: 'idp_2', name: '企业微信', kind: 'wecom' },
      ],
    })
    const renderer = await renderLoginPage()
    const buttons = renderer.root.findAll(
      (n) => n.type === 'button' && /使用「.+」登录/.test(textOf(n)),
    )
    expect(buttons).toHaveLength(2)
    expect(buttons.map(textOf)).toEqual(['使用「公司 Okta」登录', '使用「企业微信」登录'])
  })

  it('一个启用中的 IdP 都没有时完全不渲染该区块，不留空壳', async () => {
    mockListSsoProviders.mockResolvedValue({ items: [] })
    const renderer = await renderLoginPage()
    const providerBlocks = renderer.root.findAll(
      (n) => n.props?.className === 'sso-provider-list',
    )
    expect(providerBlocks).toHaveLength(0)
  })

  it('AuthContext 报告 SSO 回跳交换失败时，登录页展示明确的错误文案（不是白屏）', async () => {
    mockListSsoProviders.mockResolvedValue({ items: [] })
    mockSsoLoginError = '登录链接已失效，请重新发起登录'
    const renderer = await renderLoginPage()
    const alerts = renderer.root.findAll((n) => n.props?.role === 'alert')
    expect(alerts.map(textOf)).toContain('登录链接已失效，请重新发起登录')
  })
})
