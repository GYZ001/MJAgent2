import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// React #310 事故（2026-08-24）：ScriptPage 组件体内曾把一个 useMemo 写在
// `if (!ep) return <QueryState .../>` 提前返回之后。首次渲染 ep 为空时提前返回、
// 这个 hook 从没被调用；轮询拿到数据后的下一次渲染会走到 useMemo 那一行，
// 同一个组件实例前后两次渲染的 hook 调用数不一致，violates Rules of Hooks，
// React 直接炸出 "Rendered more hooks than during the previous render"。
//
// 纯函数测试（ScriptPage.test.ts 里的 27+ 条）测不出这类问题——它们各自独立调用
// normalizeStage/PrepStepper 等纯函数或无状态组件，从不对同一个有状态组件实例做
// "先这样渲染、再那样重渲染"的时序验证，所以 273 条全绿也没能拦住这次事故。
// 这个文件专门补时序缺口：用 react-test-renderer（React 官方、不需要 jsdom 的测试
// 渲染器）对同一个 ScriptPage 实例做 mount -> update -> update -> update -> update，
// 在分支之间来回切换，真实复现 React 的 hook 顺序校验。

// vi.mock 会被提升到文件顶部；工厂里只能引用以 `mock` 开头的变量。
const mockScriptState: { ep: unknown; project: unknown } = { ep: null, project: null }

vi.mock('../App', () => ({
  useNav: () => ({
    episodeId: 'ep_test',
    projectId: 'proj_test',
    chapterIdx: null,
    view: 'script',
    go: () => {},
    requestNavigation: () => {},
    toast: () => {},
    registerNavigationGuard: () => {},
  }),
  useScriptEpisode: () => ({
    data: mockScriptState.ep,
    error: null,
    status: null,
    loading: false,
    refresh: async () => mockScriptState.ep,
  }),
  useProject: () => ({
    data: mockScriptState.project,
    error: null,
    status: null,
    loading: false,
    refresh: async () => mockScriptState.project,
  }),
  // EpisodeCrumb（ScriptPage 的子组件）自己也从 '../App' 取 usePoll 发起分集切换器
  // 的独立轮询；不 mock 这个会在渲染期直接打真实网络。
  usePoll: () => ({ data: null, error: null, loading: false, refresh: async () => null }),
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import ScriptPage from './ScriptPage'

/** 项目测试环境是 node（未装 jsdom）。react-test-renderer 本身不需要真实 DOM，
 *  但被渲染树里的子组件（EpisodeCrumb 的搜索防抖、ServerTaskTimer 的计时器）
 *  在 useEffect 里摸了 window.setTimeout/setInterval 等宿主 API，效果在
 *  act() 里会被真实 flush，所以需要一个最小 window/document 存根。 */
function installHostStubs() {
  const store = new Map<string, string>()
  const passthrough = {
    addEventListener: () => {},
    removeEventListener: () => {},
  }
  ;(globalThis as { window?: unknown }).window = {
    ...passthrough,
    setTimeout: (...args: Parameters<typeof setTimeout>) => setTimeout(...args),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
    setInterval: (...args: Parameters<typeof setInterval>) => setInterval(...args),
    clearInterval: (id: ReturnType<typeof setInterval>) => clearInterval(id),
    requestAnimationFrame: (cb: FrameRequestCallback) => setTimeout(() => cb(Date.now()), 0) as unknown as number,
    cancelAnimationFrame: (id: number) => clearTimeout(id),
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value) },
      removeItem: (key: string) => { store.delete(key) },
      clear: () => { store.clear() },
    },
  }
  ;(globalThis as { document?: unknown }).document = {
    ...passthrough,
    activeElement: null,
    body: { style: {} },
    visibilityState: 'visible',
  }
}

function uninstallHostStubs() {
  delete (globalThis as { window?: unknown }).window
  delete (globalThis as { document?: unknown }).document
}

const now = () => Math.floor(Date.now() / 1000)

const generatingEpisode = () => ({
  id: 'ep_test',
  episode_no: 1,
  title: '测试集',
  hook: '',
  cliffhanger: '',
  synopsis: '',
  source_chapters: [1],
  target_duration_s: 300,
  status: 'drafting',
  cost_cny: 0,
  screenplay_status: 'running',
  screenplay: null,
  prep_pack: null,
  screenplay_artifact_id: null,
  screenplay_evidence: null,
  screenplay_production: {
    operation: 'baseline' as const,
    phase: 'BLUEPRINT_GENERATION',
    baseline_done: false,
    first_evaluation_done: false,
    task_active: true,
    task_started_at: now() - 30,
    task_finished_at: null,
    can_resume_repair: false,
    can_resume_baseline: false,
    prep_pack_stages: [
      { key: 'event_chain_extraction', display_name: '事件链抽取', state: 'active' },
      { key: 'asset_mapping', display_name: '资产映射', state: 'pending' },
    ],
  },
  screenplay_state: { version: 1, code: 'running', message: '正在生成准备包', recommended_action: 'stop_screenplay' as const, screenplay_status: 'running', storyboard_status: 'no_screenplay', storyboard_running: false, publish_blocked: true },
})

const readyEpisode = () => {
  const base = generatingEpisode()
  return {
    ...base,
    status: 'scripted',
    screenplay_status: 'ready',
    screenplay_production: {
      ...base.screenplay_production,
      task_active: false,
      prep_pack_stages: [
        { key: 'event_chain_extraction', display_name: '事件链抽取', state: 'done' },
        { key: 'asset_mapping', display_name: '资产映射', state: 'done' },
      ],
    },
    screenplay_state: { version: 2, code: 'ready', message: '准备包已交付', recommended_action: 'generate_storyboard' as const, screenplay_status: 'ready', storyboard_status: 'no_screenplay', storyboard_running: false, publish_blocked: false },
    prep_pack: {
      prep_pack_version: '1.1.0',
      episode_no: 1,
      episode_scope: { chapter_indexes: [1], source_segment_count: 10 },
      event_chain: [
        { event_id: 'ev_001', order: 1, summary: '测试事件', source_evidence: [], key_lines: [] },
      ],
      asset_manifest: { characters: [], scenes: [] },
      coverage_ledger: {
        total_segments: 10, delivered: [1], merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
      },
      hook: 'h', cliffhanger: 'c',
    },
  }
}

// 用户报告过首屏闪现旧十步阶段带：后端把集详情投影统一到 prep_pack_stages 单源的
// 过渡期里，响应有短暂窗口只带旧 `stages`（十步重型流水线遗留）、还没带
// `prep_pack_stages`。曾经的 resolveStages 在这种情况下会回退渲染旧十步，这就是
// 闪现的真实根因。回退已被物理移除：这里直接构造"只有旧 stages、没有
// prep_pack_stages"的响应，断言整棵渲染树里一个旧阶段名都不出现，且换成骨架占位。
const legacyOnlyEpisode = () => {
  const base = generatingEpisode()
  return {
    ...base,
    screenplay_production: {
      ...base.screenplay_production,
      // base（generatingEpisode）已经带了 prep_pack_stages；这里必须显式清掉，
      // 否则"只有旧字段"这个前提就不成立——resolveStages 会照常选中它，
      // 测出来的是"两字段并存"场景，不是本测试要覆盖的过渡期场景。
      prep_pack_stages: undefined,
      stages: [
        { key: 'CHARACTER_DISCOVERY', label: '人物识别', status: 'completed' },
        { key: 'BLUEPRINT_GENERATION', label: '叙事蓝图', status: 'completed' },
        { key: 'IDENTITY_FREEZE', label: '身份冻结', status: 'in_progress' },
        { key: 'ENVELOPE_GENERATION', label: '全局包络', status: 'pending' },
        { key: 'SCENE_SHARD_GENERATION', label: '场次写作', status: 'pending' },
        { key: 'IR_MERGE', label: '全局编译', status: 'pending' },
        { key: 'STRUCTURE_VALIDATION', label: '结构校验', status: 'pending' },
        { key: 'QUALITY_SCORING', label: '质量评分', status: 'pending' },
        { key: 'PUBLISHING', label: '原子发布', status: 'pending' },
        { key: 'SUCCEEDED', label: '已完成', status: 'pending' },
      ],
    },
  }
}
describe('ScriptPage stage-bar flash regression (legacy stages must never render, not even transiently)', () => {
  beforeEach(() => {
    installHostStubs()
    mockScriptState.ep = null
    mockScriptState.project = null
  })
  afterEach(() => {
    uninstallHostStubs()
  })

  it('renders the skeleton placeholder — never the old 10-step names — when only legacy stages is present', () => {
    mockScriptState.ep = legacyOnlyEpisode()
    let renderer: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer!.toJSON())

    for (const legacyName of ['人物识别', '叙事蓝图', '身份冻结', '全局包络', '场次写作', '全局编译', '质量评分', '原子发布']) {
      expect(serialized).not.toContain(legacyName)
    }
    expect(serialized).toContain('prep-stepper-skeleton')
    expect(serialized).not.toContain('prep-stepper-item')

    act(() => { renderer!.unmount() })
  })

  it('keeps rendering the skeleton across a re-render (not just on first mount) while legacy-only data persists', () => {
    mockScriptState.ep = null
    let renderer: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })

    mockScriptState.ep = legacyOnlyEpisode()
    act(() => { renderer!.update(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer!.toJSON())
    expect(serialized).not.toContain('人物识别')
    expect(serialized).toContain('prep-stepper-skeleton')

    act(() => { renderer!.unmount() })
  })
})

describe('ScriptPage hook-order regression (React #310)', () => {
  beforeEach(() => {
    installHostStubs()
    mockScriptState.ep = null
    mockScriptState.project = null
  })
  afterEach(() => {
    uninstallHostStubs()
  })

  it('survives no-data -> generating(no pack) -> ready(has pack) -> generating -> no-data on the SAME instance without throwing', () => {
    let renderer: TestRenderer.ReactTestRenderer | undefined
    const element = () => React.createElement(ScriptPage)

    expect(() => {
      act(() => { renderer = TestRenderer.create(element()) })
    }).not.toThrow()

    mockScriptState.ep = generatingEpisode()
    expect(() => {
      act(() => { renderer!.update(element()) })
    }).not.toThrow()

    mockScriptState.ep = readyEpisode()
    expect(() => {
      act(() => { renderer!.update(element()) })
    }).not.toThrow()

    // 反向：ready -> generating -> 无数据（模拟轮询回退/删除后重新进入加载态）
    mockScriptState.ep = generatingEpisode()
    expect(() => {
      act(() => { renderer!.update(element()) })
    }).not.toThrow()

    mockScriptState.ep = null
    expect(() => {
      act(() => { renderer!.update(element()) })
    }).not.toThrow()

    act(() => { renderer!.unmount() })
  })
})
