import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 操作审计页：系统管理员用来溯源「谁在什么时候经哪个入口触发了什么」。用
// react-test-renderer 真挂载，覆盖筛选联动、行展开懒加载详情、URL 带初始
// user_id、以及加载失败给出「重试」出路（CLAUDE.md「拦住用户时必须给出路」）。

const FIXED_NOW_MS = 1_700_000_000_000

const { mockApi, MockApiError } = vi.hoisted(() => {
  const mockFacets = {
    events: [
      { event: 'storyboard.generate', event_label: '生成分镜', count: 5 },
      { event: 'auth.login', event_label: null, count: 2 },
    ],
    users: [{ user_id: 'u1', username: 'demo', count: 3 }],
    outcomes: [{ outcome: 'ok', count: 5 }, { outcome: 'failed', count: 1 }],
    sources: [{ source: 'ui', count: 5 }, { source: 'agent', count: 1 }],
    projects: [{ project_id: 'p1', project_name: '斗破苍穹', count: 5 }],
  }
  const event1 = {
    id: 'ae_1', ts: 1000, user_id: 'u1', username: 'demo', is_system_admin: false,
    source: 'ui', event: 'storyboard.generate', event_label: '生成分镜',
    method: 'POST', path: '/api/storyboard/start', project_id: 'p1', project_name: '斗破苍穹',
    episode_id: 'e1', target: 'episode_id=e1 · shot_no=3', outcome: 'ok',
    http_status: 200, error_id: null, error_code: null, summary: null, duration_ms: 820, ip: '1.2.3.4',
  }
  const event2 = {
    id: 'ae_2', ts: 900, user_id: null, username: null, is_system_admin: null,
    source: 'system', event: 'auth.login', event_label: null,
    method: null, path: null, project_id: null, project_name: null,
    episode_id: null, target: null, outcome: 'failed',
    http_status: 401, error_id: 'ERR-1', error_code: 'BAD_AUTH', summary: '密码错误', duration_ms: null, ip: null,
  }
  const detail1 = { ...event1, user_agent: 'Mozilla/5.0', args: { shot_no: 3 } }
  const mockApi = {
    getAuditFacets: vi.fn(async () => mockFacets),
    listAuditEvents: vi.fn(async () => ({ items: [event1, event2], next_cursor: null, server_time: 2000 })),
    getAuditEvent: vi.fn(async () => detail1),
  }
  class MockApiError extends Error {
    constructor(public status: number, message: string) { super(message) }
  }
  return { mockApi, MockApiError }
})

vi.mock('../api', () => ({ api: mockApi, ApiError: MockApiError }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import OperationAuditPage from './OperationAuditPage'

function installWindowStub(search = '') {
  ;(globalThis as { window?: unknown }).window = { location: { search } }
}
function uninstallWindowStub() {
  delete (globalThis as { window?: unknown }).window
}

async function renderPage() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(OperationAuditPage))
  })
  return renderer
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.filter((c): c is string => typeof c === 'string').join('')
}
function findButtons(renderer: TestRenderer.ReactTestRenderer, label: string) {
  return renderer.root.findAll((n) => n.type === 'button' && textOf(n) === label)
}

describe('操作审计页', () => {
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(FIXED_NOW_MS)
    installWindowStub()
    Object.values(mockApi).forEach((fn) => fn.mockClear())
  })
  afterEach(() => {
    uninstallWindowStub()
    vi.restoreAllMocks()
  })

  it('拉到 facets 与事件列表后渲染表格，事件别名/结果/来源都显示中文标签', async () => {
    const renderer = await renderPage()
    expect(mockApi.getAuditFacets).toHaveBeenCalledTimes(1)
    expect(mockApi.listAuditEvents).toHaveBeenCalledTimes(1)

    const rows = renderer.root.findAll((n) => n.type === 'tr')
    expect(rows.length).toBeGreaterThanOrEqual(2)
    const text = JSON.stringify(renderer.toJSON())
    expect(text).toContain('生成分镜')
    expect(text).toContain('成功')
    expect(text).toContain('失败')
    expect(text).toContain('页面') // event1.source === 'ui'
    expect(text).toContain('未登录') // event2.username === null
  })

  it('切换时间预设与结果筛选后重新调用 API，参数正确', async () => {
    const renderer = await renderPage()
    const preset30 = findButtons(renderer, '近 30 天')[0]
    await act(async () => { preset30.props.onClick() })
    const sinceCall = mockApi.listAuditEvents.mock.calls.at(-1)![0]
    expect(sinceCall.since).toBe(Math.floor(FIXED_NOW_MS / 1000) - 30 * 86400)

    const outcomeSelect = renderer.root.findByProps({ 'aria-label': '按结果筛选' })
    await act(async () => { outcomeSelect.props.onChange({ target: { value: 'failed' } }) })
    const outcomeCall = mockApi.listAuditEvents.mock.calls.at(-1)![0]
    expect(outcomeCall.outcome).toBe('failed')
  })

  it('展开一行会请求详情接口并显示 args JSON', async () => {
    const renderer = await renderPage()
    const toggle = renderer.root.findAll(
      (n) => n.type === 'button' && n.props['aria-label'] === '展开生成分镜的详情',
    )[0]
    await act(async () => { toggle.props.onClick() })
    expect(mockApi.getAuditEvent).toHaveBeenCalledWith('ae_1')
    expect(JSON.stringify(renderer.toJSON())).toContain('shot_no')
  })

  it('加载失败显示重试；点重试后恢复', async () => {
    mockApi.listAuditEvents.mockRejectedValueOnce(new MockApiError(500, '服务器异常'))
    const renderer = await renderPage()
    renderer.root.findByProps({ role: 'alert' }) // 断言存在，即真的走了错误分支
    expect(JSON.stringify(renderer.toJSON())).toContain('服务器异常')

    const retryBtn = findButtons(renderer, '重试')[0]
    await act(async () => { retryBtn.props.onClick() })
    expect(mockApi.listAuditEvents).toHaveBeenCalledTimes(2)
    const rows = renderer.root.findAll((n) => n.type === 'tr')
    expect(rows.length).toBeGreaterThanOrEqual(2)
  })

  it('URL 带 user_id 时初始筛选带上它', async () => {
    uninstallWindowStub()
    installWindowStub('?user_id=u1')
    const renderer = await renderPage()
    const userSelect = renderer.root.findByProps({ 'aria-label': '按用户筛选' })
    expect(userSelect.props.value).toBe('u1')
    const firstCall = mockApi.listAuditEvents.mock.calls[0][0]
    expect(firstCall.user_id).toBe('u1')
  })
})
