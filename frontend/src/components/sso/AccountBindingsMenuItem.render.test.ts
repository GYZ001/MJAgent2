import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 「拦人必须给出路」：解绑最后一个登录方式后端返回 422 并说明原因，界面
// 必须把这句话原样显示出来，不能只说"操作失败"。

const {
  mockListSsoProviders, mockListMyIdentities, mockStartSsoLink, mockUnlinkSso, MockApiError,
} = vi.hoisted(() => {
  class MockApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    mockListSsoProviders: vi.fn(),
    mockListMyIdentities: vi.fn(),
    mockStartSsoLink: vi.fn(async () => ({ authorize_url: 'https://idp.example/authorize' })),
    mockUnlinkSso: vi.fn(),
    MockApiError,
  }
})

vi.mock('../../api', () => ({
  ApiError: MockApiError,
  listSsoProviders: mockListSsoProviders,
  listMyIdentities: mockListMyIdentities,
  startSsoLink: mockStartSsoLink,
  unlinkSso: mockUnlinkSso,
}))
vi.mock('../../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import AccountBindingsMenuItem from './AccountBindingsMenuItem'

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.filter((c): c is string => typeof c === 'string').join('')
}

async function renderItem() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(AccountBindingsMenuItem))
  })
  return renderer
}

describe('账号绑定入口', () => {
  beforeEach(() => {
    mockListSsoProviders.mockReset()
    mockListMyIdentities.mockReset()
    mockUnlinkSso.mockReset()
  })

  it('系统没有配置任何 IdP 时整个入口都不渲染（不留空壳）', async () => {
    mockListSsoProviders.mockResolvedValue({ items: [] })
    const renderer = await renderItem()
    expect(renderer.toJSON()).toBeNull()
  })

  it('点开后按已绑定状态分别展示"解绑"/"绑定"按钮', async () => {
    mockListSsoProviders.mockResolvedValue({
      items: [{ id: 'idp_1', name: '公司 Okta', kind: 'oidc' }],
    })
    mockListMyIdentities.mockResolvedValue({
      items: [{ idp_id: 'idp_1', idp_name: '公司 Okta', idp_kind: 'oidc', linked_at: 0, last_login_at: null }],
    })
    const renderer = await renderItem()
    const openBtn = renderer.root.findAll((n) => n.type === 'button' && textOf(n) === '账号绑定')[0]
    await act(async () => {
      openBtn.props.onClick()
    })
    const unbindBtn = renderer.root.findAll((n) => n.type === 'button' && textOf(n) === '解绑')
    expect(unbindBtn).toHaveLength(1)
  })

  it('解绑最后一个登录方式被后端 422 拒绝时，原样显示拒绝原因', async () => {
    mockListSsoProviders.mockResolvedValue({
      items: [{ id: 'idp_1', name: '公司 Okta', kind: 'oidc' }],
    })
    mockListMyIdentities.mockResolvedValue({
      items: [{ idp_id: 'idp_1', idp_name: '公司 Okta', idp_kind: 'oidc', linked_at: 0, last_login_at: null }],
    })
    mockUnlinkSso.mockRejectedValue(
      new MockApiError(422, '这是你唯一的登录方式，解绑后将无法登录；请先设置本地口令或绑定另一个身份提供方'),
    )
    const renderer = await renderItem()
    const openBtn = renderer.root.findAll((n) => n.type === 'button' && textOf(n) === '账号绑定')[0]
    await act(async () => {
      openBtn.props.onClick()
    })
    const unbindBtn = renderer.root.findAll((n) => n.type === 'button' && textOf(n) === '解绑')[0]
    await act(async () => {
      await unbindBtn.props.onClick()
    })
    const alerts = renderer.root.findAll((n) => n.props?.role === 'alert')
    expect(alerts.map(textOf)).toContain(
      '这是你唯一的登录方式，解绑后将无法登录；请先设置本地口令或绑定另一个身份提供方',
    )
  })
})
