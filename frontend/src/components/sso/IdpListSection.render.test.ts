import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// interop_verified 是诚实标注（app/sso/profiles.py）：企业微信/飞书/钉钉三家
// 目前是 false（从未对接过真实服务器），标准 OIDC 是 true。管理面必须把这
// 一点显式标出来，不能让"支持"两个字看起来像"已经验证过"——这里用真挂载
// 而不是纯函数测试，因为要验的正是"这段文案确实出现在渲染结果里"。

const { mockListIdps, mockUpdateIdp, mockDeleteIdp, MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    mockListIdps: vi.fn(),
    mockUpdateIdp: vi.fn(async () => ({})),
    mockDeleteIdp: vi.fn(async () => ({ ok: true })),
    MockApiError,
  }
})

vi.mock('../../api', () => ({
  ApiError: MockApiError,
  listIdps: mockListIdps,
  updateIdp: mockUpdateIdp,
  deleteIdp: mockDeleteIdp,
  createIdp: vi.fn(async () => ({})),
}))
// 弹窗焦点圈定要摸 document.body，vitest 跑在 environment: 'node' 下没有 DOM；
// 本文件不测编辑弹窗的打开，桩掉即可。
vi.mock('../../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import IdpListSection from './IdpListSection'

function idpRow(overrides: Partial<Record<string, unknown>>): Record<string, unknown> {
  return {
    id: 'idp_x', org_id: null, kind: 'oidc', name: '未命名', enabled: true,
    issuer: null, client_id: 'cid', has_client_secret: true,
    discovery_url: null, authorize_url: null, token_url: null, userinfo_url: null, jwks_url: null,
    scopes: 'openid', claim_map_json: '{}', provision_json: '{}', allowed_domains: null,
    created_at: 0, updated_at: 0, interop_verified: true, interop_note: '',
    ...overrides,
  }
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.filter((c): c is string => typeof c === 'string').join('')
}

async function renderSection() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(IdpListSection))
  })
  return renderer
}

describe('IdP 列表：互通验证状态标记', () => {
  beforeEach(() => {
    mockListIdps.mockReset()
    mockUpdateIdp.mockClear()
    mockDeleteIdp.mockClear()
  })

  it('标准 OIDC 显示"已验证"，企业微信显示"未经验证"告警文案', async () => {
    mockListIdps.mockResolvedValue({
      items: [
        idpRow({
          id: 'idp_oidc', kind: 'oidc', name: '公司 Okta', interop_verified: true,
          interop_note: '已验证：id_token 签名 + iss/aud/exp/nonce/sub 五项声明校验走真实代码路径测试。',
        }),
        idpRow({
          id: 'idp_wecom', kind: 'wecom', name: '企业微信', enabled: false, interop_verified: false,
          interop_note: '未经真实互联验证：本环境没有可用的企业微信/飞书/钉钉沙箱账号，此 profile 只做过通用 OAuth2 抽象的单元测试。',
        }),
      ],
    })

    const renderer = await renderSection()
    expect(mockListIdps).toHaveBeenCalledTimes(1)

    const paragraphs = renderer.root.findAll((node) => node.type === 'p')
    const allText = paragraphs.map(textOf)

    const verifiedLine = allText.find((t) => t.startsWith('已验证'))
    expect(verifiedLine).toBeDefined()
    expect(verifiedLine).toContain('id_token 签名')

    const unverifiedLine = allText.find((t) => t.startsWith('⚠ 未经验证'))
    expect(unverifiedLine).toBeDefined()
    expect(unverifiedLine).toContain('未经真实互联验证')
    expect(unverifiedLine).toContain('企业微信/飞书/钉钉沙箱账号')

    // 未验证的这一行必须带上后端诚实标注的文案，不能只显示"未经验证"四个字
    // 就完事——用户需要知道具体没验证过什么。
    const unverifiedParagraph = paragraphs.find((p) => textOf(p).startsWith('⚠ 未经验证'))
    expect(unverifiedParagraph?.props.className).toBe('field-error')
    const verifiedParagraph = paragraphs.find((p) => textOf(p).startsWith('已验证'))
    expect(verifiedParagraph?.props.className).toBe('hint')
  })

  it('零配置时不渲染任何一行 IdP，只显示空态提示', async () => {
    mockListIdps.mockResolvedValue({ items: [] })
    const renderer = await renderSection()
    const text = renderer.root.findAll((node) => node.type === 'p').map(textOf).join('\n')
    expect(text).toContain('尚未配置任何身份提供方')
  })
})
