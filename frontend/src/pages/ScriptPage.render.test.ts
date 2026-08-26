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
import ScriptPage, { PrepPackView } from './ScriptPage'

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
  screenplay_state: { version: 1, code: 'running', message: '正在生成映射包', recommended_action: 'stop_screenplay' as const, screenplay_status: 'running', storyboard_status: 'no_screenplay', storyboard_running: false, publish_blocked: true },
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
    screenplay_state: { version: 2, code: 'ready', message: '映射包已交付', recommended_action: 'generate_storyboard' as const, screenplay_status: 'ready', storyboard_status: 'no_screenplay', storyboard_running: false, publish_blocked: false },
    prep_pack: {
      prep_pack_version: '2.0.0',
      episode_no: 1,
      episode_scope: { chapter_indexes: [1], source_segment_count: 10 },
      asset_manifest: { characters: [], scenes: [] },
      coverage_ledger: {
        total_segments: 10, delivered: [1], merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
      },
    },
  }
}

// 协调方打回复现（真实 episode ep_3d523ff4d0a4，prep_pack_version 1.11.1）：转型前
// 的旧版映射包没有 segment_indexes/appellation_map/props 字段。整页（不只
// PrepPackView 单测）过一遍，确认后端仍在吐的"剧本已交付"一类措辞在这条路径上
// 也被改写，且资源卡不把字段缺失渲染成"覆盖 0 段"。
const legacyPackEpisode = () => {
  const base = readyEpisode()
  return {
    ...base,
    prep_pack: {
      prep_pack_version: '1.11.1',
      episode_no: 1,
      episode_scope: { chapter_indexes: [1], source_segment_count: 40 },
      asset_manifest: {
        characters: [
          { identity_id: 'bible:许清', display_name: '许清', portrait_id: '', aliases: [], display_appellation: '许师姐' },
        ],
        scenes: [
          { scene_id: 'scene:靠山宗', display_name: '靠山宗', scene_reference_id: '' },
        ],
      },
      coverage_ledger: {
        total_segments: 40, delivered: Array.from({ length: 40 }, (_, i) => i + 1),
        merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
      },
    },
    screenplay_state: { ...base.screenplay_state, message: '剧本已交付' },
  }
}

// 没有任何映射包的全新分集：screenplay_status='pending'，prep_pack/screenplay
// 都是 null，production 都还没起。这条路径不经过 PrepPackView（isPrepPack 判
// false），主视觉是顶部主操作 + 后端状态一句话——同样不许出现看起来像测量结果的
// 假 0（如 QueryState 兜底态里编出"0 个人物"这类文案）。
const emptyNewEpisode = () => ({
  id: 'ep_test',
  episode_no: 4,
  title: '空白集',
  hook: '',
  cliffhanger: '',
  synopsis: '',
  source_chapters: [4],
  target_duration_s: 300,
  status: 'planned',
  cost_cny: 0,
  screenplay_status: 'pending',
  screenplay: null,
  prep_pack: null,
  screenplay_artifact_id: null,
  screenplay_evidence: null,
  screenplay_state: { version: 1, code: 'pending', message: '尚未生成可交付剧本', recommended_action: 'generate_screenplay' as const, screenplay_status: 'pending', storyboard_status: 'no_screenplay', storyboard_running: false, publish_blocked: true },
})

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

// 资源卡点名联动称谓总表（次要、可折叠，见 PrepPackView 的信息分层注释）：
// 点击出场人物的名字，appellation_map 里属于同一 identity_id 的行被高亮
// （.prep-timeline-item.event-linked）并展开总表，再点同一条目取消。场景/群演/
// 道具没有 appellation_map 条目可联动，渲染为纯文本、不是可点按钮（见 PrepPackView）。
// PrepPackView 本身不依赖 useNav/useScriptEpisode，直接渲染，不需要上面的
// vi.mock('../App', ...) 桩件。
describe('PrepPackView roster-name click links the appellation table', () => {
  beforeEach(() => {
    installHostStubs()
  })
  afterEach(() => {
    uninstallHostStubs()
  })

  const linkedPack = () => ({
    prep_pack_version: '2.0.0',
    episode_no: 3,
    episode_scope: { chapter_indexes: [3], source_segment_count: 20 },
    asset_manifest: {
      // 孟浩覆盖段 1/2/4（压缩成 "1~2,4"）；许清覆盖段 3。
      characters: [
        { identity_id: 'char:孟浩', display_name: '孟浩', portrait_id: '', segment_indexes: [1, 2, 4] },
        { identity_id: 'char:许清', display_name: '许清', portrait_id: '', segment_indexes: [3] },
      ],
      scenes: [
        { scene_id: 'scene:靠山宗', display_name: '靠山宗', scene_reference_id: '', segment_indexes: [3] },
      ],
      functional_extras: [],
    },
    // 孟浩两行（段 1/2）、许清一行（段 3）——覆盖"点一个角色只高亮它自己的行，
    // 不牵连另一个角色"这条核心断言。
    appellation_map: [
      { raw_mention: '那少年', segment_index: 1, identity_id: 'char:孟浩', canonical_appellation: '孟浩' },
      { raw_mention: '书生', segment_index: 2, identity_id: 'char:孟浩', canonical_appellation: '孟浩' },
      { raw_mention: '女子', segment_index: 3, identity_id: 'char:许清', canonical_appellation: '许清' },
    ],
    coverage_ledger: {
      total_segments: 20, delivered: [], merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  }) as any

  const findRosterButtons = (renderer: TestRenderer.ReactTestRenderer) =>
    renderer.root.findAll(
      node => node.type === 'button' && typeof node.props.className === 'string'
        && node.props.className.includes('prep-roster-name-btn'),
    )

  const timelineItemFlags = (renderer: TestRenderer.ReactTestRenderer) =>
    renderer.root.findAllByType('li')
      .filter(node => typeof node.props.className === 'string' && node.props.className.includes('prep-timeline-item'))
      .map(node => node.props.className.includes('event-linked'))

  it('renders the compressed range next to the plain count for each roster item', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(PrepPackView, { pack: linkedPack(), bible: null, sourceFallback: '第 3 章' }),
      )
    })
    // .prep-roster-meta 现在只有一个花括号表达式（assetCoverageText 的返回值），
    // react-test-renderer 对单一子节点不会包成数组，直接是字符串本体。
    const metaTexts = renderer!.root
      .findAll(node => typeof node.props.className === 'string' && node.props.className === 'prep-roster-meta')
      .map(node => String(node.props.children))
    expect(metaTexts).toContain('覆盖 3 段原文 · 第 1~2,4 段')
    expect(metaTexts).toContain('覆盖 1 段原文 · 第 3 段')
    act(() => { renderer!.unmount() })
  })

  it('only characters get a clickable roster button — scenes render as plain text (no appellation_map entries to link to)', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(PrepPackView, { pack: linkedPack(), bible: null, sourceFallback: '第 3 章' }),
      )
    })
    expect(findRosterButtons(renderer!)).toHaveLength(2)
    act(() => { renderer!.unmount() })
  })

  it('nothing is highlighted/selected before any roster item is clicked', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(PrepPackView, { pack: linkedPack(), bible: null, sourceFallback: '第 3 章' }),
      )
    })
    expect(timelineItemFlags(renderer!)).toEqual([false, false, false])
    for (const button of findRosterButtons(renderer!)) {
      expect(button.props['aria-pressed']).toBe(false)
      expect(button.props.className).not.toContain('selected')
    }
    act(() => { renderer!.unmount() })
  })

  it('clicking a roster name highlights exactly its own appellation rows, and a second click clears it', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(PrepPackView, { pack: linkedPack(), bible: null, sourceFallback: '第 3 章' }),
      )
    })

    // 孟浩（第一个 roster 按钮）对应称谓映射表的前两行，不牵连许清那一行。
    act(() => { findRosterButtons(renderer!)[0].props.onClick() })

    expect(timelineItemFlags(renderer!)).toEqual([true, true, false])
    const mengHaoButton = findRosterButtons(renderer!)[0]
    expect(mengHaoButton.props['aria-pressed']).toBe(true)
    expect(mengHaoButton.props.className).toContain('selected')

    // 再点同一条目：取消高亮与选中态。
    act(() => { findRosterButtons(renderer!)[0].props.onClick() })

    expect(timelineItemFlags(renderer!)).toEqual([false, false, false])
    const mengHaoButtonAfter = findRosterButtons(renderer!)[0]
    expect(mengHaoButtonAfter.props['aria-pressed']).toBe(false)
    expect(mengHaoButtonAfter.props.className).not.toContain('selected')

    act(() => { renderer!.unmount() })
  })

  it('selecting a different roster item switches the link instead of accumulating (single-select)', () => {
    let renderer: TestRenderer.ReactTestRenderer
    act(() => {
      renderer = TestRenderer.create(
        React.createElement(PrepPackView, { pack: linkedPack(), bible: null, sourceFallback: '第 3 章' }),
      )
    })

    // 先选孟浩（两行），再选许清（一行）——应切换，不叠加。
    act(() => { findRosterButtons(renderer!)[0].props.onClick() })
    expect(timelineItemFlags(renderer!)).toEqual([true, true, false])

    act(() => { findRosterButtons(renderer!)[1].props.onClick() })
    expect(timelineItemFlags(renderer!)).toEqual([false, false, true])

    const buttons = findRosterButtons(renderer!)
    expect(buttons[0].props['aria-pressed']).toBe(false)
    expect(buttons[1].props['aria-pressed']).toBe(true)

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

// 协调方明确要求：改完在两种数据下把整页过一遍——旧版本映射包（1.11.1，真实
// episode ep_3d523ff4d0a4 那种形状）与没有任何映射包的空集。两种都不能出现
// "看起来像真实测量的零值"。这里渲染的是完整 ScriptPage（含顶部状态行/主操作），
// 不只是 PrepPackView 片段，覆盖 screenplayStateMessage 的改写路径。
describe('ScriptPage full-page walkthrough — legacy prep pack & empty episode (真 bug 回归 + 布局重做验收)', () => {
  beforeEach(() => {
    installHostStubs()
    mockScriptState.ep = null
    mockScriptState.project = null
  })
  afterEach(() => {
    uninstallHostStubs()
  })

  it('legacy pack (1.11.1): renames the backend "剧本" status line and shows the explicit legacy notice, never a fake 0', () => {
    mockScriptState.ep = legacyPackEpisode()
    let renderer: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer!.toJSON())

    // 状态行改写：后端仍吐"剧本已交付"，页面必须显示"映射包已交付"。
    expect(serialized).toContain('映射包已交付')
    expect(serialized).not.toContain('剧本已交付')
    // 旧版映射包的显式说明必须出现，带上真实版本号。
    expect(serialized).toContain('旧版映射包')
    expect(serialized).toContain('1.11.1')
    // 绝不能把 segment_indexes 缺失渲染成"覆盖 0 段"或未验证的"未发现"断言。
    expect(serialized).not.toContain('覆盖 0 段原文')
    expect(serialized).not.toContain('本集未发现需要归一的模糊人物称谓')
    // 资源清单仍然是主体内容：许清这张资源卡照常渲染，且带着本集称谓
    // （JSON.stringify 把 JSX 的 `本集：{appellation}` 序列化成两个相邻数组元素，
    // 不是拼接后的一整段字符串，所以分开断言，语义上等价于用户读到的那一整行）。
    expect(serialized).toContain('许清')
    expect(serialized).toContain('本集：')
    expect(serialized).toContain('许师姐')
    // 两个"进入分镜台"按钮的重复语义已合并为一个。
    expect((serialized!.match(/进入分镜台/g) ?? []).length).toBeLessThanOrEqual(1)
    expect(serialized).not.toContain('查看分镜台')

    act(() => { renderer!.unmount() })
  })

  it('empty episode (no prep_pack, no screenplay): renders the primary action with a renamed status line, no PrepPackView, no fabricated zeros', () => {
    mockScriptState.ep = emptyNewEpisode()
    let renderer: TestRenderer.ReactTestRenderer
    act(() => { renderer = TestRenderer.create(React.createElement(ScriptPage)) })
    const serialized = JSON.stringify(renderer!.toJSON())

    expect(serialized).toContain('映射包')
    expect(serialized).not.toContain('尚未生成可交付剧本')
    // 空集没有资源清单可言，不应该出现资源卡片或覆盖计数网格。
    expect(serialized).not.toContain('prep-roster')
    expect(serialized).not.toContain('覆盖 0 段原文')
    expect(serialized).not.toContain('出场人物')
    expect(serialized).not.toContain('称谓映射')

    act(() => { renderer!.unmount() })
  })
})
