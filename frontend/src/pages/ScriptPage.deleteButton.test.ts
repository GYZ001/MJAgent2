import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// P2-1 回归（界面承诺必须与实际行为一致）：「删除失败映射包」按钮此前不区分
// 用户主动停止（screenplay_production.stage_stop_reason === 'paused'，见
// app/production/revision.py 1321-1328 行：active/published 时清空该字段，
// 门禁未过是 'blocked'，run.status==='FAILED' 才是 'failed'，其余——包括用户
// 点「停止映射包任务」触发的取消——落在 'paused'）与真失败，统一写「失败」，
// 对主动停止的用户不准确。独立成文件是因为 ScriptPage.tsx / ScriptPage.test.ts /
// ScriptPage.render.test.ts 三个文件的行数基线都已顶到实测值（见
// app/FILE_CONVENTIONS.toml），没有余量再往里加新的 describe/fixture。

const mockScriptState: { ep: unknown } = { ep: null }

vi.mock('../App', () => ({
  useNav: () => ({
    episodeId: 'ep_test', projectId: 'proj_test', chapterIdx: null, view: 'script',
    go: () => {}, requestNavigation: () => {}, toast: () => {}, registerNavigationGuard: () => {},
  }),
  useScriptEpisode: () => ({
    data: mockScriptState.ep, error: null, status: null, loading: false,
    refresh: async () => mockScriptState.ep,
  }),
  useProject: () => ({ data: null, error: null, status: null, loading: false, refresh: async () => null }),
  usePoll: () => ({ data: null, error: null, loading: false, refresh: async () => null }),
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import ScriptPage from './ScriptPage'

/** 与 ScriptPage.render.test.ts 同款最小宿主存根（node 环境没有真实 DOM，
 *  EpisodeCrumb/ServerTaskTimer 的 useEffect 会摸 window 定时器 API）。 */
function installHostStubs() {
  const passthrough = { addEventListener: () => {}, removeEventListener: () => {} }
  ;(globalThis as { window?: unknown }).window = {
    ...passthrough,
    setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
    setInterval: (...args: Parameters<typeof setInterval>) => setInterval(...args),
    clearInterval: (id: ReturnType<typeof setInterval>) => clearInterval(id),
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

/** 没有任何产物（screenplay/prep_pack 都是 null）、但有可续跑检查点的分集——
 *  这是删除按钮走"else"分支（非「删除当前映射包」）的唯一前提条件。
 *  stage_stop_reason 由调用方按场景覆盖。 */
function noOutputEpisode(stageStopReason: 'paused' | 'failed' | 'blocked') {
  return {
    id: 'ep_test', episode_no: 2, title: '停止测试集', hook: '', cliffhanger: '', synopsis: '',
    source_chapters: [2], target_duration_s: 300, status: 'drafting',
    screenplay_status: 'failed', screenplay: null, prep_pack: null,
    screenplay_artifact_id: null, screenplay_evidence: null,
    screenplay_production: {
      operation: 'baseline', phase: 'BLUEPRINT_GENERATION', baseline_done: false,
      first_evaluation_done: false, task_active: false,
      can_resume_repair: true, can_resume_baseline: false,
      stage_stop_reason: stageStopReason,
    },
  }
}

describe('ScriptPage 删除按钮文案随 stage_stop_reason 区分（P2-1）', () => {
  beforeEach(installHostStubs)
  afterEach(uninstallHostStubs)

  it('用户主动停止（paused）：显示「删除已停止的映射包」，不写「失败」', () => {
    mockScriptState.ep = noOutputEpisode('paused')
    let renderer: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer!.toJSON())

    expect(serialized).toContain('删除已停止的映射包')
    expect(serialized).not.toContain('删除失败映射包')

    act(() => { renderer!.unmount() })
  })

  it('真失败（failed）：仍显示「删除失败映射包」，不误报成用户主动停止', () => {
    mockScriptState.ep = noOutputEpisode('failed')
    let renderer: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer!.toJSON())

    expect(serialized).toContain('删除失败映射包')
    expect(serialized).not.toContain('删除已停止的映射包')

    act(() => { renderer!.unmount() })
  })
})
