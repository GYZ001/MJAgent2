import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import {
  PrepPackView,
  PrepStepper,
  ScreenplayResumeButton,
  characterAppellationTag,
  compressEventOrders,
  coverageGateSummary,
  eventIdsToOrders,
  findPortraitImage,
  findSceneReferenceImage,
  formatSourceSpan,
  isPrepPack,
  normalizeStage,
  provenanceMethodHint,
  resolveStages,
  screenplayGeneratePayload,
  screenplayResumeActionLabel,
  screenplayResumeOutcomeSummary,
  sortedEventChain,
  stageStateLabel,
  stageStateTone,
} from './ScriptPage'

const rebuildProduction = {
  operation: 'baseline_rebuild' as const,
  mode: 'baseline_rebuild' as const,
  mode_label: '按新合同重建剧本',
  phase: 'BLUEPRINT_GENERATION',
  baseline_done: false,
  first_evaluation_done: false,
  task_active: false,
  can_resume_baseline: true,
  can_resume_repair: false,
}

describe('screenplayResumeActionLabel', () => {
  it('uses the backend baseline rebuild label', () => {
    expect(screenplayResumeActionLabel(rebuildProduction)).toBe('按新合同重建剧本')
  })

  it('keeps the compatibility label only for older responses', () => {
    expect(screenplayResumeActionLabel({
      operation: 'baseline',
      phase: 'SCENE_SHARD_GENERATION',
      baseline_done: false,
      first_evaluation_done: false,
      task_active: false,
      can_resume_baseline: true,
      can_resume_repair: false,
    })).toBe('继续首版场次生成')
  })
})

describe('ScreenplayResumeButton', () => {
  it('renders the backend mode label and dispatches the mounted button click', () => {
    const onResume = vi.fn()
    const button = ScreenplayResumeButton({
      production: rebuildProduction,
      busy: false,
      onResume,
    })

    expect(renderToStaticMarkup(button)).toContain('按新合同重建剧本')
    button.props.onClick()

    expect(onResume).toHaveBeenCalledOnce()
  })
})

describe('screenplayResumeOutcomeSummary', () => {
  it('uses the server receipt summary instead of a fixed resume toast', () => {
    expect(screenplayResumeOutcomeSummary({
      mode: 'baseline_rebuild',
      summary: '服务端：已启动兼容合同重建',
    })).toBe('服务端：已启动兼容合同重建')
  })

  it('falls back to the server mode when an older receipt has no summary', () => {
    expect(screenplayResumeOutcomeSummary({ mode: 'baseline_rebuild' }))
      .toBe('已按当前合同启动剧本基线重建')
  })
})

describe('screenplayGeneratePayload', () => {
  it('sends only the idempotency key when no retry fence is active', () => {
    expect(screenplayGeneratePayload('key-1', undefined)).toEqual({
      idempotency_key: 'key-1',
    })
    expect(
      screenplayGeneratePayload('key-1', { requires_fresh_retry_grant: false }),
    ).toEqual({ idempotency_key: 'key-1' })
  })

  it('authorizes the paid retry with the expected unknown receipts when fenced', () => {
    const receipts = [{ call_id: 61640 }, { call_id: 61641 }]
    expect(
      screenplayGeneratePayload('key-2', {
        requires_fresh_retry_grant: true,
        unknown_receipts: receipts,
      }),
    ).toEqual({
      idempotency_key: 'key-2',
      authorize_blueprint_retry: true,
      expected_blueprint_unknown_receipts: receipts,
    })
  })

  it('defaults unknown receipts to an empty list when the fence lacks them', () => {
    expect(
      screenplayGeneratePayload('key-3', { requires_fresh_retry_grant: true }),
    ).toEqual({
      idempotency_key: 'key-3',
      authorize_blueprint_retry: true,
      expected_blueprint_unknown_receipts: [],
    })
  })
})

describe('isPrepPack', () => {
  it('accepts a payload carrying a non-empty prep_pack_version', () => {
    expect(isPrepPack({ prep_pack_version: '1.0.0', episode_no: 1 })).toBe(true)
  })

  it('rejects the legacy heavy screenplay shape so callers never render it as a prep pack', () => {
    expect(isPrepPack({ title: '第一集', full_script_text: '正文' })).toBe(false)
  })

  it('rejects null, undefined, and non-object payloads without throwing', () => {
    expect(isPrepPack(null)).toBe(false)
    expect(isPrepPack(undefined)).toBe(false)
    expect(isPrepPack('episode_prep_pack')).toBe(false)
    expect(isPrepPack({ prep_pack_version: '' })).toBe(false)
  })
})

describe('sortedEventChain', () => {
  it('sorts events by order regardless of input order', () => {
    const events = [
      { event_id: 'ev_003', order: 3, summary: 'c', source_evidence: [], key_lines: [] },
      { event_id: 'ev_001', order: 1, summary: 'a', source_evidence: [], key_lines: [] },
      { event_id: 'ev_002', order: 2, summary: 'b', source_evidence: [], key_lines: [] },
    ]
    expect(sortedEventChain(events).map(e => e.event_id)).toEqual(['ev_001', 'ev_002', 'ev_003'])
  })

  it('tolerates a missing or null event list', () => {
    expect(sortedEventChain(null)).toEqual([])
    expect(sortedEventChain(undefined)).toEqual([])
  })
})

describe('compressEventOrders', () => {
  it('merges a fully consecutive run into a single a~b segment', () => {
    expect(compressEventOrders([1, 2, 3])).toBe('1~3')
    expect(compressEventOrders([1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe('1~9')
  })

  it('lists fully non-consecutive orders as individual comma-separated values', () => {
    expect(compressEventOrders([1, 3, 5, 7])).toBe('1,3,5,7')
  })

  it('mixes runs and singles per the user-specified format (e.g. "1,3,5~7")', () => {
    expect(compressEventOrders([1, 3, 5, 6, 7])).toBe('1,3,5~7')
    expect(compressEventOrders([1, 2, 3, 7, 8, 9, 10, 11])).toBe('1~3,7~11')
  })

  it('renders a lone order as a single number, not a self-range', () => {
    expect(compressEventOrders([5])).toBe('5')
  })

  it('returns an empty string for an empty input so callers fall back to the plain count', () => {
    expect(compressEventOrders([])).toBe('')
  })

  it('sorts and de-duplicates unordered, repeated input before compressing', () => {
    expect(compressEventOrders([3, 1, 3, 2])).toBe('1~3')
    expect(compressEventOrders([7, 1, 1, 9, 8])).toBe('1,7~9')
  })
})

describe('eventIdsToOrders', () => {
  const events = [
    { event_id: 'ev_001', order: 1, summary: 'a', source_evidence: [], key_lines: [] },
    { event_id: 'ev_002', order: 2, summary: 'b', source_evidence: [], key_lines: [] },
    { event_id: 'ev_003', order: 3, summary: 'c', source_evidence: [], key_lines: [] },
  ]

  it('resolves event_ids to their event_chain order numbers', () => {
    expect(eventIdsToOrders(['ev_001', 'ev_003'], events)).toEqual([1, 3])
  })

  it('defensively skips an event_id absent from event_chain instead of throwing or emitting undefined', () => {
    expect(eventIdsToOrders(['ev_001', 'ev_999', 'ev_002'], events)).toEqual([1, 2])
  })

  it('tolerates a missing or empty event_ids list without throwing', () => {
    expect(eventIdsToOrders(undefined, events)).toEqual([])
    expect(eventIdsToOrders(null, events)).toEqual([])
    expect(eventIdsToOrders([], events)).toEqual([])
  })
})

describe('coverageGateSummary', () => {
  it('reports the green gate with per-account counts when nothing is uncovered', () => {
    const summary = coverageGateSummary({
      total_segments: 4,
      delivered: [1, 2],
      merged: [3],
      retained_as_context: [4],
      proven_duplicates: [],
      uncovered: [],
    })
    expect(summary).toEqual({
      ok: true,
      uncoveredCount: 0,
      uncoveredLabels: [],
      deliveredCount: 2,
      mergedCount: 1,
      retainedCount: 1,
      duplicateCount: 0,
      paratextCount: 0,
      paratextLabels: [],
      totalSegments: 4,
    })
  })

  it('reports the red gate and lists missing segment indexes as labels', () => {
    const summary = coverageGateSummary({
      total_segments: 5,
      delivered: [1],
      merged: [],
      retained_as_context: [],
      proven_duplicates: [],
      uncovered: [2, 3],
    })
    expect(summary.ok).toBe(false)
    expect(summary.uncoveredCount).toBe(2)
    expect(summary.uncoveredLabels).toEqual(['2', '3'])
  })

  it('extracts segment_index from object-shaped ledger entries as a defensive fallback', () => {
    const summary = coverageGateSummary({
      total_segments: 2,
      delivered: [],
      merged: [],
      retained_as_context: [],
      proven_duplicates: [],
      uncovered: [{ segment_index: 7 }, { segment_index: '8' }],
    } as any)
    expect(summary.uncoveredLabels).toEqual(['7', '8'])
  })

  it('treats a missing ledger as a clean gate with zero counts', () => {
    expect(coverageGateSummary(null)).toEqual({
      ok: true,
      uncoveredCount: 0,
      uncoveredLabels: [],
      deliveredCount: 0,
      mergedCount: 0,
      retainedCount: 0,
      duplicateCount: 0,
      paratextCount: 0,
      paratextLabels: [],
      totalSegments: 0,
    })
  })

  // 第五账（1.4.0+）：副文本——章节名/作者留言段等，是合法覆盖，不算未覆盖。
  it('counts paratext as a legitimate covered account, separate from uncovered', () => {
    const summary = coverageGateSummary({
      total_segments: 5,
      delivered: [1, 2],
      merged: [],
      retained_as_context: [3],
      proven_duplicates: [],
      uncovered: [],
      paratext: [4, 5],
    })
    expect(summary.ok).toBe(true)
    expect(summary.paratextCount).toBe(2)
    expect(summary.paratextLabels).toEqual(['4', '5'])
    expect(summary.uncoveredCount).toBe(0)
  })

  it('defaults paratext to zero/empty for 1.3.0-and-earlier packs that lack the field entirely', () => {
    const summary = coverageGateSummary({
      total_segments: 3,
      delivered: [1, 2, 3],
      merged: [],
      retained_as_context: [],
      proven_duplicates: [],
      uncovered: [],
      // paratext 字段整个不存在（1.3.0 及更早产物）
    })
    expect(summary.paratextCount).toBe(0)
    expect(summary.paratextLabels).toEqual([])
  })
})

describe('findPortraitImage', () => {
  const bible = {
    characters: [
      {
        name: '甲',
        role: '',
        appearance_canonical: '',
        personality: '',
        speech_style: '',
        relationships: [],
        portraits: [
          { id: 'portrait-1', ep_start: 1, ep_end: null, image_url: 'https://example.test/a.png' },
        ],
      },
    ],
    world: { era: '', genre: '', visual_style_canonical: '' },
  } as any

  it('finds the image url for a matching portrait id across all characters', () => {
    expect(findPortraitImage(bible, 'portrait-1')).toBe('https://example.test/a.png')
  })

  it('returns null when the portrait id is empty, missing, or unmatched', () => {
    expect(findPortraitImage(bible, '')).toBeNull()
    expect(findPortraitImage(bible, null)).toBeNull()
    expect(findPortraitImage(bible, 'portrait-unknown')).toBeNull()
    expect(findPortraitImage(null, 'portrait-1')).toBeNull()
  })
})

describe('findSceneReferenceImage', () => {
  const bible = {
    characters: [],
    world: { era: '', genre: '', visual_style_canonical: '' },
    scenes: [
      {
        name: '客栈',
        scene_canonical: '',
        scene_refs: [
          { id: 'scene-ref-1', ep_start: 1, ep_end: null, image_url: 'https://example.test/s.png' },
        ],
      },
    ],
  } as any

  it('finds the image url for a matching scene reference id across all scenes', () => {
    expect(findSceneReferenceImage(bible, 'scene-ref-1')).toBe('https://example.test/s.png')
  })

  it('returns null when the scene reference id is empty, missing, or unmatched', () => {
    expect(findSceneReferenceImage(bible, '')).toBeNull()
    expect(findSceneReferenceImage(bible, undefined)).toBeNull()
    expect(findSceneReferenceImage(bible, 'scene-ref-unknown')).toBeNull()
    expect(findSceneReferenceImage(null, 'scene-ref-1')).toBeNull()
  })
})

describe('stageStateTone / stageStateLabel', () => {
  it('normalizes the confirmed new-shape state values (pending/active/done/blocked)', () => {
    expect(stageStateTone('pending')).toBe('pending')
    expect(stageStateTone('active')).toBe('active')
    expect(stageStateTone('done')).toBe('done')
    expect(stageStateTone('blocked')).toBe('blocked')
  })

  it('still normalizes the legacy heavy-pipeline status values for backward compat', () => {
    expect(stageStateTone('completed')).toBe('done')
    expect(stageStateTone('in_progress')).toBe('active')
    expect(stageStateTone('running')).toBe('active')
    expect(stageStateTone('failed')).toBe('blocked')
  })

  it('falls back unrecognized state values to pending rather than throwing', () => {
    expect(stageStateTone('some_future_state')).toBe('pending')
  })

  it('labels known states in Chinese and passes through unrecognized ones verbatim', () => {
    expect(stageStateLabel('done')).toBe('已完成')
    expect(stageStateLabel('active')).toBe('进行中')
    expect(stageStateLabel('completed')).toBe('已完成')
    expect(stageStateLabel('blocked')).toBe('门禁未通过')
    expect(stageStateLabel('mystery_state')).toBe('mystery_state')
  })
})

describe('normalizeStage', () => {
  it('reads the new backend shape {key, display_name, state} directly', () => {
    expect(normalizeStage({ key: 'a', display_name: '事件抽取', state: 'done' }, 0))
      .toEqual({ key: 'a', text: '事件抽取', tone: 'done', stateLabel: '已完成' })
  })

  it('falls back to the legacy shape {key, label, status} when display_name/state are absent', () => {
    expect(normalizeStage({ key: 'b', label: '身份冻结', status: 'in_progress' }, 1))
      .toEqual({ key: 'b', text: '身份冻结', tone: 'active', stateLabel: '进行中' })
  })

  it('falls all the way back to the key, then an ordinal, when every text field is missing or empty', () => {
    expect(normalizeStage({ key: 'c' }, 2).text).toBe('c')
    expect(normalizeStage({ key: '', display_name: '', label: '' }, 2).text).toBe('阶段 3')
  })

  it('treats empty-string display_name/label as missing and keeps falling through', () => {
    expect(normalizeStage({ key: 'd', display_name: '', label: '备用名', state: '' }, 3).text).toBe('备用名')
  })

  it('defaults state to pending when both state and status are missing', () => {
    expect(normalizeStage({ key: 'e', display_name: '待命名' }, 4).tone).toBe('pending')
  })
})

describe('resolveStages', () => {
  // 用户报告过首屏闪现旧十步阶段带；根因是曾经"prep_pack_stages 缺失/为空时回退
  // 渲染旧 stages"的逻辑，该回退已被物理移除——resolveStages 现在只读
  // prep_pack_stages，压根不看旧 stages 字段，不管它是否存在、是否非空。
  const legacyTenStages = Array.from({ length: 10 }, (_, i) => ({
    key: `STAGE_${i}`, label: `阶段${i + 1}`, status: 'completed',
  }))
  const realPrepPackStages = [
    { key: 'event_chain_extraction', display_name: '事件链抽取', state: 'done' },
    { key: 'asset_mapping', display_name: '资产映射', state: 'done' },
    { key: 'hook_cliffhanger', display_name: '抽取开场钩子与结尾悬念', state: 'done' },
    { key: 'coverage_and_publish', display_name: '覆盖对账与原子发布', state: 'done' },
  ]

  it('uses prep_pack_stages (4 items) and ignores a legacy stages field entirely when both happen to be present', () => {
    // 后端投影还没完全统一到单源的过渡期，理论上两个字段可能同时出现在同一份响应里；
    // 即便如此，旧字段也必须被彻底忽略——用 any 绕过类型（resolveStages 的参数类型
    // 已经不再声明 stages），验证运行时行为，不只是类型层面的"不用它"。
    const stages = resolveStages({ stages: legacyTenStages, prep_pack_stages: realPrepPackStages } as any)
    expect(stages).toHaveLength(4)
    expect(stages).toBe(realPrepPackStages)
    const html = renderToStaticMarkup(PrepStepper({ stages }))
    expect((html.match(/prep-stepper-item/g) ?? []).length).toBe(4)
    for (const stage of realPrepPackStages) expect(html).toContain(stage.display_name)
  })

  it('returns an empty list — NOT the legacy 10-step stages — when prep_pack_stages is absent', () => {
    expect(resolveStages({ stages: legacyTenStages } as any)).toEqual([])
  })

  it('returns an empty list when prep_pack_stages is present but empty, even with legacy stages alongside', () => {
    expect(resolveStages({ stages: legacyTenStages, prep_pack_stages: [] } as any)).toEqual([])
  })

  it('returns an empty list, not throwing, when production is null/undefined or the field is missing', () => {
    expect(resolveStages(null)).toEqual([])
    expect(resolveStages(undefined)).toEqual([])
    expect(resolveStages({})).toEqual([])
  })
})

describe('PrepStepper renders non-empty labels for any stage shape (self-check)', () => {
  it('handles the new shape, the legacy shape, and shapes with missing fields without ever rendering empty text', () => {
    // 混合真实场景：新形状、后端当前仍在发的旧形状（十步重型流水线遗留）、
    // 只有 key 的极端缺字段形状，以及 display_name/label 都是空字符串的形状。
    const stages = [
      { key: 'a', display_name: '事件抽取', state: 'done' },
      { key: 'b', label: '身份冻结', status: 'in_progress' },
      { key: 'c' },
      { key: 'd', display_name: '', label: '', state: 'blocked' },
    ]
    const html = renderToStaticMarkup(PrepStepper({ stages }))
    // 任何一个 prep-stepper-label 都不能是空标签（这正是"十个框还在但字没了"的 bug 现象）。
    expect(html).not.toMatch(/prep-stepper-label"[^>]*>\s*<\/span>/)
    expect(html).toContain('事件抽取')
    expect(html).toContain('身份冻结')
    expect(html).toContain('>c<')
    expect(html).toContain('>d<')
  })

  it('renders a compact single ordered list regardless of stage count (old 10-step or new 4-5 step payloads)', () => {
    const tenLegacyStages = Array.from({ length: 10 }, (_, i) => ({
      key: `STAGE_${i}`,
      label: `阶段${i + 1}`,
      status: i < 10 ? 'completed' : 'pending',
    }))
    const html = renderToStaticMarkup(PrepStepper({ stages: tenLegacyStages }))
    expect((html.match(/prep-stepper-item/g) ?? []).length).toBe(10)
    expect(html).not.toMatch(/prep-stepper-label"[^>]*>\s*<\/span>/)
  })
})

describe('formatSourceSpan', () => {
  it('formats a multi-segment span as a range', () => {
    expect(formatSourceSpan({ from_segment: 12, to_segment: 15 })).toBe('覆盖原文段 12-15')
  })

  it('formats a single-segment span without a dash', () => {
    expect(formatSourceSpan({ from_segment: 7, to_segment: 7 })).toBe('覆盖原文段 7')
  })

  it('returns null for 1.0.0 payloads that lack source_span entirely', () => {
    expect(formatSourceSpan(undefined)).toBeNull()
    expect(formatSourceSpan(null)).toBeNull()
  })
})

// 真实 EP1 数据自证（project proj_3ac0b627fa46 / episode ep_3d523ff4d0a4，取自
// data/manju.db 的 screenplay_json，prep_pack_version 1.1.0，2026-08-24 落表）。
// 环境里新落地的登录鉴权拦住了匿名 curl 走查 API，改为在这里把库里的真实产物直接
// 当固定样本跑一遍渲染管线，比单次 curl 快照更可回归。stages 取自
// app/production/revision.py 的真实 stage_order（十步重型流水线遗留，EP1 已发布，
// 全部 status="completed"）——这正是用户反馈里"十个框"的真实来源。
describe('real EP1 payload walkthrough (no live auth session available; verified against the DB row + backend source directly)', () => {
  const ep1Pack = {
    prep_pack_version: '1.1.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 62 },
    event_chain: [
      {
        event_id: 'ev_001', order: 1,
        summary: '黄昏时分，孟浩坐在大青山山顶，背景介绍赵国书生对东土大唐的向往。',
        source_span: { from_segment: 1, to_segment: 3 },
        source_evidence: [
          { segment_index: 1, quote: '第一章书生孟浩' },
          { segment_index: 3, quote: '落在了此刻于这青山顶端，坐在那里的一个文生少年身上。' },
        ],
        key_lines: [],
      },
      {
        event_id: 'ev_017', order: 17,
        summary: '作者现身呼吁读者收藏、投推荐票，并预告晚间新书发布会活动。',
        source_span: { from_segment: 60, to_segment: 62 },
        source_evidence: [
          { segment_index: 60, quote: '书生孟浩和大家见面啦，收藏和推荐票，一个都不要少呀' },
          { segment_index: 62, quote: '晚上还有一章，今晚八点有语音活动，新书发布会。' },
        ],
        key_lines: [],
      },
    ],
    asset_manifest: {
      characters: [
        { identity_id: 'bible:孟浩', display_name: '孟浩', portrait_id: 'portrait_ecc9491a63f4', event_ids: ['ev_001', 'ev_002'] },
        { identity_id: 'bible:王有材', display_name: '王有材', portrait_id: 'portrait_bb6813d0733d', event_ids: ['ev_006', 'ev_007'] },
        { identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_e01eec6ef5ef', event_ids: ['ev_010'] },
      ],
      scenes: [
        { scene_id: 'scene:大青山山顶', display_name: '大青山山顶', scene_reference_id: 'scene_e6dab3555673', event_ids: ['ev_001'] },
        { scene_id: 'scene:半山青石空地', display_name: '半山青石空地', scene_reference_id: 'scene_a9c9f33fad29', event_ids: ['ev_014', 'ev_017'] },
      ],
    },
    coverage_ledger: {
      total_segments: 62,
      delivered: Array.from({ length: 29 }, (_, i) => i + 1),
      merged: [],
      retained_as_context: Array.from({ length: 33 }, (_, i) => i + 30),
      proven_duplicates: [],
      uncovered: [],
    },
    hook: '三年科举再次落榜，孟浩一贫如洗，坐在山顶为欠债和生计发愁，看不到希望。',
    cliffhanger: '孟浩得知自己即将进入靠山宗当杂役，内心充满期待，却不知等待他的将是何种命运。',
  }

  // app/production/revision.py 的真实 stage_order；EP1 published=true 时全部 status="completed"。
  const ep1LiveStages = [
    { key: 'CHARACTER_DISCOVERY', label: '人物识别', status: 'completed' },
    { key: 'BLUEPRINT_GENERATION', label: '叙事蓝图', status: 'completed' },
    { key: 'IDENTITY_FREEZE', label: '身份冻结', status: 'completed' },
    { key: 'ENVELOPE_GENERATION', label: '全局包络', status: 'completed' },
    { key: 'SCENE_SHARD_GENERATION', label: '场次写作', status: 'completed' },
    { key: 'IR_MERGE', label: '全局编译', status: 'completed' },
    { key: 'STRUCTURE_VALIDATION', label: '结构校验', status: 'completed' },
    { key: 'QUALITY_SCORING', label: '质量评分', status: 'completed' },
    { key: 'PUBLISHING', label: '原子发布', status: 'completed' },
    { key: 'SUCCEEDED', label: '已完成', status: 'completed' },
  ]

  it('is recognized as a valid prep pack', () => {
    expect(isPrepPack(ep1Pack)).toBe(true)
  })

  it('sorts and preserves non-empty summaries for every real event', () => {
    const events = sortedEventChain(ep1Pack.event_chain as any)
    expect(events.map(e => e.order)).toEqual([1, 17])
    for (const event of events) {
      expect(event.summary.trim().length).toBeGreaterThan(0)
      expect(formatSourceSpan(event.source_span)).toMatch(/^覆盖原文段 \d+-\d+$/)
    }
  })

  it('computes a green coverage gate matching the real ledger (0 uncovered out of 62 segments)', () => {
    const gate = coverageGateSummary(ep1Pack.coverage_ledger as any)
    expect(gate.ok).toBe(true)
    expect(gate.uncoveredCount).toBe(0)
    expect(gate.deliveredCount).toBe(29)
    expect(gate.retainedCount).toBe(33)
  })

  it('resolves every real character portrait_id and scene_reference_id to a non-empty roster name', () => {
    // 用真实 portrait_id/scene_reference_id 命中一个人造 Bible（真实 image_url 由后端
    // build_media_url 生成，这里只验证 id 匹配机制本身，已在 findPortraitImage 单测中
    // 单独覆盖 URL 取值路径）。
    const bible = {
      characters: [
        { name: '孟浩', portraits: [{ id: 'portrait_ecc9491a63f4', image_url: '/media/a.jpg' }] },
        { name: '王有材', portraits: [{ id: 'portrait_bb6813d0733d', image_url: '/media/b.jpg' }] },
        { name: '许清', portraits: [{ id: 'portrait_e01eec6ef5ef', image_url: '/media/c.jpg' }] },
      ],
      world: { era: '', genre: '', visual_style_canonical: '' },
      scenes: [
        { name: '大青山山顶', scene_refs: [{ id: 'scene_e6dab3555673', image_url: '/media/d.jpg' }] },
        { name: '半山青石空地', scene_refs: [{ id: 'scene_a9c9f33fad29', image_url: '/media/e.jpg' }] },
      ],
    } as any
    for (const character of ep1Pack.asset_manifest.characters) {
      expect(character.display_name.trim().length).toBeGreaterThan(0)
      expect(findPortraitImage(bible, character.portrait_id)).toMatch(/^\/media\//)
    }
    for (const scene of ep1Pack.asset_manifest.scenes) {
      expect(scene.display_name.trim().length).toBeGreaterThan(0)
      expect(findSceneReferenceImage(bible, scene.scene_reference_id)).toMatch(/^\/media\//)
    }
  })

  it('renders the real published-episode stage list (old shape, 10 items) with zero empty labels', () => {
    // 这是"十个框还在"问题的真实数据来源：published=true 时 revision.py 给出的就是
    // {key, label, status} 十项、全 completed，不是协调方假设的新 4 步形状。
    const html = renderToStaticMarkup(PrepStepper({ stages: ep1LiveStages }))
    expect((html.match(/prep-stepper-item/g) ?? []).length).toBe(10)
    expect(html).not.toMatch(/prep-stepper-label"[^>]*>\s*<\/span>/)
    for (const stage of ep1LiveStages) {
      expect(html).toContain(stage.label)
    }
    // 十项全部 completed → 全部应归一化为 done 语调（绿色勾选），不是待开始的灰点。
    const doneCount = (html.match(/data-tone="done"/g) ?? []).length
    expect(doneCount).toBe(10)
  })

  it('produces a non-empty hook and cliffhanger', () => {
    expect(ep1Pack.hook.trim().length).toBeGreaterThan(0)
    expect(ep1Pack.cliffhanger.trim().length).toBeGreaterThan(0)
  })
})

// P1 补渲染：asset_manifest.characters[].aliases（1.2.0+）与 asset_manifest.functional_extras
// （1.3.0+，群演/一次性人物）。真实样本取自 EP13（project proj_3ac0b627fa46 / episode
// ep_820ad3cefde7，data/manju.db 的 screenplay_json，prep_pack_version 1.3.0）：
// 3 个具名角色 aliases 均为空数组（这个项目目前没有产出过非空 aliases 的真实样本），
// 5 条 functional_extras。用真实数据验证字段读取正确性，再用一条合成数据验证
// aliases 非空时小签确实渲染（覆盖协调方举的"小胖子"场景）。
describe('PrepPackView renders aliases and functional_extras (real EP13 data)', () => {
  const ep13AssetManifest = {
    characters: [
      { identity_id: 'bible:孟浩', display_name: '孟浩', portrait_id: 'portrait_ecc9491a63f4', event_ids: Array.from({ length: 12 }, (_, i) => `ev_${String(i + 1).padStart(3, '0')}`), aliases: [] },
      { identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_e01eec6ef5ef', event_ids: ['ev_001', 'ev_002', 'ev_003'], aliases: [] },
      { identity_id: 'bible:曹阳', display_name: '曹阳', portrait_id: 'portrait_95288d031252', event_ids: ['ev_005', 'ev_006', 'ev_007', 'ev_008', 'ev_009', 'ev_010', 'ev_011', 'ev_012'], aliases: [] },
    ],
    scenes: [
      { scene_id: 'scene:靠山宗外宗区域', display_name: '靠山宗外宗区域', scene_reference_id: 'scene_05d482b34f00', event_ids: ['ev_001', 'ev_002', 'ev_003', 'ev_004'] },
    ],
    functional_extras: [
      { label: '外宗弟子', event_ids: ['ev_001'] },
      { label: '养丹坊中年男子', event_ids: ['ev_002'] },
      { label: '宝阁弟子', event_ids: ['ev_002'] },
      { label: '昨日被坑修士', event_ids: ['ev_005', 'ev_006', 'ev_008', 'ev_011'] },
      { label: '公开区其他修士', event_ids: ['ev_005', 'ev_006', 'ev_009', 'ev_010'] },
    ],
  }

  const buildPack = (assetManifest: typeof ep13AssetManifest) => ({
    prep_pack_version: '1.3.0',
    episode_no: 13,
    episode_scope: { chapter_indexes: [13], source_segment_count: 52 },
    event_chain: [
      { event_id: 'ev_001', order: 1, summary: '测试事件', source_evidence: [], key_lines: [] },
    ],
    asset_manifest: assetManifest,
    coverage_ledger: {
      total_segments: 52, delivered: Array.from({ length: 27 }, (_, i) => i + 1),
      merged: [], retained_as_context: Array.from({ length: 25 }, (_, i) => i + 28),
      proven_duplicates: [], uncovered: [],
    },
    hook: 'h', cliffhanger: 'c',
  }) as any

  it('renders all 5 real functional_extras with correct labels and event counts, using the icon placeholder (no <img>)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(ep13AssetManifest), bible: null, sourceFallback: '第 13 章' }))
    expect(html).toContain('群演 / 一次性人物')
    expect(html).toContain('群演 / 一次性人物 · 5')
    for (const extra of ep13AssetManifest.functional_extras) {
      expect(html).toContain(extra.label)
    }
    // 事件覆盖数按各自 event_ids 长度展示
    expect(html).toContain('覆盖 4 个事件') // 昨日被坑修士 / 公开区其他修士 都是 4
    expect(html).toContain('覆盖 1 个事件') // 外宗弟子 / 养丹坊中年男子 / 宝阁弟子 都是 1
    // 群演占位用统一图标，不生成 <img> 标签（它们没有 portrait_id/scene_reference_id 可查图）
    const extrasSectionStart = html.indexOf('群演 / 一次性人物')
    const extrasSectionEnd = html.indexOf('出场场景')
    const extrasSectionHtml = html.slice(extrasSectionStart, extrasSectionEnd)
    expect(extrasSectionHtml).not.toContain('<img')
    expect(extrasSectionHtml).toContain('prep-roster-icon')
  })

  it('renders no alias tag for the real EP13 characters, whose aliases are all empty arrays', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(ep13AssetManifest), bible: null, sourceFallback: '第 13 章' }))
    expect(html).not.toContain('prep-roster-alias')
    for (const character of ep13AssetManifest.characters) {
      expect(html).toContain(character.display_name)
    }
  })

  it('renders the "本集称谓" alias tag when aliases is non-empty (synthetic case per the reported example)', () => {
    const withAlias = {
      ...ep13AssetManifest,
      characters: [
        { identity_id: 'bible:李富贵', display_name: '李富贵', portrait_id: 'portrait_x', event_ids: ['ev_001'], aliases: ['小胖子'] },
      ],
    }
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(withAlias), bible: null, sourceFallback: '第 13 章' }))
    expect(html).toContain('prep-roster-alias')
    expect(html).toContain('小胖子')
    expect(html).toContain('李富贵')
  })

  it('joins multiple aliases with 、 and ignores blank-string entries', () => {
    const withAliases = {
      ...ep13AssetManifest,
      characters: [
        { identity_id: 'bible:x', display_name: '甲', portrait_id: '', event_ids: [], aliases: ['乙名', '  ', '丙名'] },
      ],
    }
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(withAliases), bible: null, sourceFallback: '第 13 章' }))
    expect(html).toContain('乙名、丙名')
  })

  it('hides the whole functional_extras section (no heading) when the field is absent (pre-1.3.0 packs)', () => {
    const { functional_extras: _drop, ...withoutExtras } = ep13AssetManifest
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(withoutExtras as any), bible: null, sourceFallback: '第 13 章' }))
    expect(html).not.toContain('群演 / 一次性人物')
  })

  it('hides the section when functional_extras is an empty array too', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack({ ...ep13AssetManifest, functional_extras: [] }), bible: null, sourceFallback: '第 13 章' }))
    expect(html).not.toContain('群演 / 一次性人物')
  })
})

// 画面与字幕分离（1.7.0+，见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.3）：
// asset_manifest.characters[] 新增 display_appellation（本集原文称谓，决定字幕/
// 取图措辞展示）与 visual_entity_id（取图口径，本页不直接展示，只做类型透传）。
// 数据取自任务描述给出的真实 EP1 样本（display_name=许清/display_appellation=
// 银色长袍女子/provenance.method=candidate_verdict，以及孟浩/王有材两条 direct
// 绑定 display_appellation 与 display_name 相同的样本）。三种形状覆盖：不同时都
// 显示、相同时不重复、字段整个缺失时退回只显示 display_name。
describe('PrepPackView renders display_appellation vs display_name (画面与字幕分离)', () => {
  const buildPack = (characters: Record<string, unknown>[]) => ({
    prep_pack_version: '1.7.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 10 },
    event_chain: [
      { event_id: 'ev_001', order: 1, summary: '测试事件', source_evidence: [], key_lines: [] },
    ],
    asset_manifest: { characters, scenes: [] },
    coverage_ledger: {
      total_segments: 10, delivered: Array.from({ length: 10 }, (_, i) => i + 1),
      merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
    hook: 'h', cliffhanger: 'c',
  }) as any

  it('shows both this-episode wording and the canonical name when they differ, distinguishably tagged', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x',
        event_ids: ['ev_001'], visual_entity_id: 'bible:许清',
        display_appellation: '银色长袍女子',
        provenance: { method: 'candidate_verdict', anchor_segments: [1], anchor_phrase: '许师姐武功高强，众人皆知。' },
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).toContain('许清')
    expect(html).toContain('银色长袍女子')
    // 必须能一眼区分谁是本集叫法：本集称谓小签自带"本集："前缀，不靠悬浮才能分辨。
    expect(html).toContain('本集：银色长袍女子')
  })

  it('does not duplicate the label when display_appellation equals display_name (most already-named characters)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:孟浩', display_name: '孟浩', portrait_id: 'portrait_y',
        event_ids: ['ev_001'], visual_entity_id: 'bible:孟浩', display_appellation: '孟浩',
        provenance: { method: 'direct', anchor_segments: [1], anchor_phrase: '孟浩' },
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).not.toContain('本集：')
    expect((html.match(/孟浩/g) ?? []).length).toBe(1)
  })

  it('falls back to display_name only when display_appellation is absent (pre-1.7.0 packs), never rendering blank/undefined', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:王有材', display_name: '王有材', portrait_id: 'portrait_z',
        event_ids: ['ev_001'],
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).toContain('王有材')
    expect(html).not.toContain('undefined')
    expect(html).not.toContain('本集：')
  })

  it('renders provenance.method as a low-key title hint mapped to a Chinese label, not as visible text', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x',
        event_ids: ['ev_001'], provenance: { method: 'candidate_verdict' },
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).toContain('title="绑定依据：候选判别"')
  })

  it('renders nothing extra when provenance is absent (pre-1.6.0 packs)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x',
        event_ids: ['ev_001'],
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).not.toContain('绑定依据')
  })
})

describe('characterAppellationTag', () => {
  it('returns the appellation when it differs from the canonical name', () => {
    expect(characterAppellationTag({ display_appellation: '银色长袍女子' }, '许清')).toBe('银色长袍女子')
  })

  it('returns null when the appellation equals the canonical name', () => {
    expect(characterAppellationTag({ display_appellation: '孟浩' }, '孟浩')).toBeNull()
  })

  it('returns null when the field is absent or blank', () => {
    expect(characterAppellationTag({}, '许清')).toBeNull()
    expect(characterAppellationTag({ display_appellation: '   ' }, '许清')).toBeNull()
    expect(characterAppellationTag({ display_appellation: undefined }, '许清')).toBeNull()
  })
})

describe('provenanceMethodHint', () => {
  it('maps a known method to a Chinese label', () => {
    expect(provenanceMethodHint('candidate_verdict')).toBe('绑定依据：候选判别')
    expect(provenanceMethodHint('direct')).toBe('绑定依据：直接匹配')
  })

  it('falls back to the raw value for an unrecognized method (does not swallow unknown info)', () => {
    expect(provenanceMethodHint('some_future_method')).toBe('绑定依据：some_future_method')
  })

  it('returns null when method is absent, so callers render no empty title', () => {
    expect(provenanceMethodHint(undefined)).toBeNull()
    expect(provenanceMethodHint(null)).toBeNull()
    expect(provenanceMethodHint('')).toBeNull()
  })
})

// 覆盖门禁第五账（1.4.0+）：副文本 chip 只在非空时出现，且不影响绿灯判定。
describe('PrepPackView renders the paratext gate chip (5th account)', () => {
  const basePack = (coverageLedger: Record<string, unknown>) => ({
    prep_pack_version: '1.4.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 5 },
    event_chain: [],
    asset_manifest: { characters: [], scenes: [] },
    coverage_ledger: coverageLedger,
    hook: 'h', cliffhanger: 'c',
  }) as any

  it('shows the "副文本 N 段" chip with the segment list in its title when paratext is non-empty', () => {
    const pack = basePack({
      total_segments: 5, delivered: [1, 2, 3], merged: [], retained_as_context: [],
      proven_duplicates: [], uncovered: [], paratext: [4, 5],
    })
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack, bible: null, sourceFallback: '第 1 章' }))
    expect(html).toContain('副文本 2 段')
    expect(html).toContain('原文段：4、5')
    // 绿灯判定不受第五账影响；覆盖总数展示把它并入
    expect(html).toContain('全部原文段已覆盖（5/5 段）')
  })

  it('omits the chip entirely when paratext is absent (pre-1.4.0 packs) or an empty array', () => {
    const withoutField = basePack({
      total_segments: 3, delivered: [1, 2, 3], merged: [], retained_as_context: [],
      proven_duplicates: [], uncovered: [],
    })
    const htmlWithout = renderToStaticMarkup(createElement(PrepPackView, { pack: withoutField, bible: null, sourceFallback: '第 1 章' }))
    expect(htmlWithout).not.toContain('副文本')

    const withEmpty = basePack({
      total_segments: 3, delivered: [1, 2, 3], merged: [], retained_as_context: [],
      proven_duplicates: [], uncovered: [], paratext: [],
    })
    const htmlEmpty = renderToStaticMarkup(createElement(PrepPackView, { pack: withEmpty, bible: null, sourceFallback: '第 1 章' }))
    expect(htmlEmpty).not.toContain('副文本')
  })
})
