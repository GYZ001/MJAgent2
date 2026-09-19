import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 用户实测反馈（2026-09-19）：账号管理/资源页多个面板的列表 state 初值为
// null，渲染时写 `(items ?? []).map(...)`，首屏在途期间是一片空白、没有
// 任何"正在加载"信号，只有 error 分支才有反馈——后台实际要 6~10 秒才渲染
// 完，用户在"没变化"与"其实在加载"之间反复误判。这里用 react-test-renderer
// 挂载真实组件，用"永不 resolve 的 promise"复现那段等待窗口，断言此时能
// 看到"正在加载"；再让 promise resolve 成空结果，断言变成"暂无"而不是仍
// 然一片空白。JSX 里 `正在加载{objectName}` 这类插值会被 react-test-renderer
// 序列化成相邻的两个数组元素而不是拼接后的整串（ScriptPage.render.test.ts
// 已踩过这个坑），所以这里不用 JSON.stringify 后整句 toContain，而是先定位
// 到 QueryState 渲染出的目标容器节点，再深度拼接文本。

const mocks = vi.hoisted(() => ({
  listUsers: vi.fn(), listDeletedUsers: vi.fn(), me: vi.fn(), deleteMyAccount: vi.fn(),
  listTeams: vi.fn(), listRoles: vi.fn(), getCurrentOrg: vi.fn(),
  listQuotaAllocations: vi.fn(), listQuotaPlans: vi.fn(), getUsageTop: vi.fn(),
  listInvitations: vi.fn(), createInvitation: vi.fn(), revokeInvitation: vi.fn(),
}))

vi.mock('../../api', () => ({
  api: { ...mocks },
  ApiError: class ApiError extends Error {
    constructor(public status: number, message: string) { super(message) }
  },
  me: mocks.me,
  deleteMyAccount: mocks.deleteMyAccount,
  getCurrentOrg: mocks.getCurrentOrg,
  listQuotaAllocations: mocks.listQuotaAllocations,
  listQuotaPlans: mocks.listQuotaPlans,
  getUsageTop: mocks.getUsageTop,
  listInvitations: mocks.listInvitations,
  listRoles: mocks.listRoles,
  listTeams: mocks.listTeams,
  createInvitation: mocks.createInvitation,
  revokeInvitation: mocks.revokeInvitation,
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import MembersTab from './MembersTab'
import TeamsTab from './TeamsTab'
import RolesTab from './RolesTab'
import InvitationsPanel from './InvitationsPanel'
import AllocationsSection from '../resources/AllocationsSection'
import TopRankingSection from '../resources/TopRankingSection'
import ResourceAdminPage from '../../pages/ResourceAdminPage'

const pending = () => new Promise<never>(() => {})

async function mount(element: React.ReactElement) {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => { renderer = TestRenderer.create(element) })
  await act(async () => {}) // 多 flush 一轮，兜住多级 await 才落地的 setState
  return renderer
}

function deepText(node: TestRenderer.ReactTestInstance): string {
  return node.children.map((c) => (typeof c === 'string' ? c : deepText(c))).join('')
}

/** 按 className 子串定位 QueryState 渲染出的容器（三态各自一个外层 div），
 *  再拼出完整文本——避免插值文本被拆成相邻数组元素导致整句匹配失败。 */
function textByClass(renderer: TestRenderer.ReactTestRenderer, cls: string): string {
  const hit = renderer.root.findAll(
    (n) => typeof n.props.className === 'string' && n.props.className.includes(cls),
  )[0]
  return hit ? deepText(hit) : ''
}

beforeEach(() => {
  Object.values(mocks).forEach((fn) => fn.mockReset())
  mocks.me.mockResolvedValue({
    user: { id: 'u1', username: 'admin', display_name: '管理员' },
    is_system_admin: true, must_change_password: false,
  })
  mocks.listTeams.mockResolvedValue({ items: [] })
  mocks.listRoles.mockResolvedValue({ items: [], permission_catalog: [] })
})

describe('账号管理/资源页——首屏在途骨架与空态（不再是一片空白）', () => {
  it('MembersTab：账号列表在途显示"正在加载"，resolve 成空数组后显示"暂无"', async () => {
    mocks.listUsers.mockReturnValue(pending())
    mocks.listDeletedUsers.mockReturnValue(pending())
    let renderer = await mount(React.createElement(MembersTab))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载账号')
    act(() => { renderer.unmount() })

    mocks.listUsers.mockResolvedValue({ items: [] })
    mocks.listDeletedUsers.mockResolvedValue({ items: [] })
    renderer = await mount(React.createElement(MembersTab))
    expect(textByClass(renderer, 'query-empty')).toContain('暂无账号')
    act(() => { renderer.unmount() })
  })

  it('ResourceAdminPage：组织信息在途显示"正在加载"，取回失败后给出明确错误而不是空白页', async () => {
    mocks.getCurrentOrg.mockReturnValue(pending())
    let renderer = await mount(React.createElement(ResourceAdminPage))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载组织信息')
    act(() => { renderer.unmount() })

    mocks.getCurrentOrg.mockRejectedValue(new Error('boom'))
    renderer = await mount(React.createElement(ResourceAdminPage))
    expect(textByClass(renderer, 'query-error')).toContain('组织信息加载失败')
    act(() => { renderer.unmount() })
  })

  it('TeamsTab：团队列表在途显示"正在加载"', async () => {
    mocks.listTeams.mockReturnValue(pending())
    mocks.listRoles.mockReturnValue(pending())
    const renderer = await mount(React.createElement(TeamsTab))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载团队')
    act(() => { renderer.unmount() })
  })

  it('RolesTab：角色列表在途显示"正在加载"', async () => {
    mocks.listRoles.mockReturnValue(pending())
    const renderer = await mount(React.createElement(RolesTab))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载角色')
    act(() => { renderer.unmount() })
  })

  it('InvitationsPanel：邀请链接列表在途显示"正在加载"', async () => {
    mocks.listInvitations.mockReturnValue(pending())
    const renderer = await mount(React.createElement(InvitationsPanel))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载邀请链接')
    act(() => { renderer.unmount() })
  })

  it('AllocationsSection：配额分配列表在途显示"正在加载"', async () => {
    mocks.listQuotaAllocations.mockReturnValue(pending())
    mocks.listQuotaPlans.mockReturnValue(pending())
    const renderer = await mount(React.createElement(AllocationsSection, { orgId: 'org_1' }))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载配额分配')
    act(() => { renderer.unmount() })
  })

  it('TopRankingSection：用量排行在途显示"正在加载"', async () => {
    mocks.getUsageTop.mockReturnValue(pending())
    const renderer = await mount(React.createElement(TopRankingSection))
    expect(textByClass(renderer, 'query-loading')).toContain('正在加载用量排行')
    act(() => { renderer.unmount() })
  })
})

// 首屏加载失败绝不能被渲染成"这里本来就是空的"。这三个面板的 error state 同时
// 承载"加载失败"与"创建/签发失败"，所以列表为空时到底该显示哪一句，取决于它是
// 真的空集还是压根没加载出来——判错的方向只有一个：把失败说成空集，然后用
// "先新建一个"引导用户去做一件此刻根本做不成的事（CLAUDE.md：界面承诺必须与
// 实际行为一致 / 拦住用户时必须给出路）。
describe('账号管理——首屏加载失败不得伪装成空集', () => {
  it('TeamsTab：列表请求失败时给出「团队加载失败」而不是「还没有团队」', async () => {
    mocks.listTeams.mockRejectedValue(new Error('boom'))
    const renderer = await mount(React.createElement(TeamsTab))
    expect(textByClass(renderer, 'query-error')).toContain('团队加载失败')
    expect(JSON.stringify(renderer.toJSON())).not.toContain('还没有团队')
    act(() => { renderer.unmount() })
  })

  it('RolesTab：列表请求失败时给出「角色加载失败」而不是「还没有可用角色」', async () => {
    mocks.listRoles.mockRejectedValue(new Error('boom'))
    const renderer = await mount(React.createElement(RolesTab))
    expect(textByClass(renderer, 'query-error')).toContain('角色加载失败')
    expect(JSON.stringify(renderer.toJSON())).not.toContain('还没有可用角色')
    act(() => { renderer.unmount() })
  })

  it('InvitationsPanel：列表请求失败时给出「邀请链接加载失败」而不是「还没有邀请记录」', async () => {
    mocks.listInvitations.mockRejectedValue(new Error('boom'))
    const renderer = await mount(React.createElement(InvitationsPanel))
    expect(textByClass(renderer, 'query-error')).toContain('邀请链接加载失败')
    expect(JSON.stringify(renderer.toJSON())).not.toContain('还没有邀请记录')
    act(() => { renderer.unmount() })
  })
})
