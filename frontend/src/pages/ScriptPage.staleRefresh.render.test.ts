import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// useScriptEpisode 内部走 usePoll（详情用 useEpisode，轻量状态用一个独立
// usePoll<ScreenplayLightStatus>——见 App.tsx），已有 ep 后再轮询失败不清空
// ep，error 此前只喂给早退的 `<QueryState hasData={false}>` 分支，ep 到手后
// 就再没人看（2026-09-23 补丁）。ScriptPage.render.test.ts 已经卡在
// FILE_CONVENTIONS.toml 的行数棘轮上（521 行，精确实测值、零缓冲），装不下
// 这条新场景，另开文件；mock 配方照抄那份文件的写法，只把 error 改成可变字段。

const mockScriptState: { ep: unknown; project: unknown; error: string | null } = {
  ep: null, project: null, error: null,
}

vi.mock('../App', () => ({
  useNav: () => ({
    episodeId: 'ep_test', projectId: 'proj_test', chapterIdx: null, view: 'script',
    go: () => {}, requestNavigation: () => {}, toast: () => {}, registerNavigationGuard: () => {},
  }),
  useScriptEpisode: () => ({
    data: mockScriptState.ep,
    error: mockScriptState.error,
    status: null,
    loading: false,
    refresh: async () => mockScriptState.ep,
  }),
  useProject: () => ({
    data: mockScriptState.project, error: null, status: null, loading: false,
    refresh: async () => mockScriptState.project,
  }),
  // EpisodeCrumb 自己也从 '../App' 取 usePoll 发起分集切换器的独立轮询；不 mock
  // 会在渲染期直接打真实网络（同 ScriptPage.render.test.ts 的注释）。
  usePoll: () => ({ data: null, error: null, loading: false, refresh: async () => null }),
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import ScriptPage from './ScriptPage'

/** 同 ScriptPage.render.test.ts：node 测试环境没有 window/document，被渲染树里
 *  的子组件（EpisodeCrumb 防抖、ServerTaskTimer 计时器）在 useEffect 里摸了
 *  window.setTimeout 等宿主 API，需要一个最小存根。 */
function installHostStubs() {
  const passthrough = { addEventListener: () => {}, removeEventListener: () => {} }
  ;(globalThis as { window?: unknown }).window = {
    ...passthrough,
    setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
    setInterval: (...args: Parameters<typeof setInterval>) => setInterval(...args),
    clearInterval: (id: ReturnType<typeof setInterval>) => clearInterval(id),
    requestAnimationFrame: (cb: FrameRequestCallback) => setTimeout(() => cb(Date.now()), 0) as unknown as number,
    cancelAnimationFrame: (id: number) => clearTimeout(id),
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
  }
  ;(globalThis as { document?: unknown }).document = {
    ...passthrough, activeElement: null, body: { style: {} }, visibilityState: 'visible',
  }
}
function uninstallHostStubs() {
  delete (globalThis as { window?: unknown }).window
  delete (globalThis as { document?: unknown }).document
}

// 与 ScriptPage.render.test.ts 的 emptyNewEpisode() 同一份最小、已验证可渲染的
// 空集夹具（无 prep_pack/screenplay，不触发 PrepPackView 那条更重的渲染路径）。
const emptyNewEpisode = () => ({
  id: 'ep_test', episode_no: 4, title: '空白集', hook: '', cliffhanger: '', synopsis: '',
  source_chapters: [4], target_duration_s: 300, status: 'planned', screenplay_status: 'pending',
  screenplay: null, prep_pack: null, screenplay_artifact_id: null, screenplay_evidence: null,
  screenplay_state: {
    version: 1, code: 'pending', message: '尚未生成可交付剧本',
    recommended_action: 'generate_screenplay' as const, screenplay_status: 'pending',
    storyboard_status: 'no_screenplay', storyboard_running: false, publish_blocked: true,
  },
})

describe('映射台——已有集详情时后台轮询刷新失败不得被吞', () => {
  beforeEach(() => {
    installHostStubs()
    mockScriptState.ep = emptyNewEpisode()
    mockScriptState.project = null
    mockScriptState.error = null
  })
  afterEach(() => { uninstallHostStubs() })

  it('轮询失败时显示「映射台刷新失败」横幅，旧集详情仍在展示', () => {
    let renderer!: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })

    mockScriptState.error = '轮询失败测试'
    act(() => { renderer.update(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('映射台刷新失败')
    expect(serialized).toContain('轮询失败测试')
    expect(serialized).toContain('映射包') // 反向断言：旧集详情没有被清空/顶掉

    act(() => { renderer.unmount() })
  })

  it('error 清空后横幅消失', () => {
    let renderer!: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })

    mockScriptState.error = '轮询失败测试'
    act(() => { renderer.update(React.createElement(ScriptPage)) })
    expect(JSON.stringify(renderer.toJSON())).toContain('刷新失败')

    mockScriptState.error = null
    act(() => { renderer.update(React.createElement(ScriptPage)) })
    expect(JSON.stringify(renderer.toJSON())).not.toContain('刷新失败')

    act(() => { renderer.unmount() })
  })
})
