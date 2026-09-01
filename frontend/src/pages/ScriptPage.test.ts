import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import {
  PrepPackView,
  PrepStepper,
  ScreenplayResumeButton,
  assetCoverageText,
  characterAppellationTag,
  compressSegmentIndexes,
  coverageGateSummary,
  findPortraitImage,
  findSceneReferenceImage,
  isLegacyPrepPackFormat,
  isPrepPack,
  normalizeStage,
  prepPackMajorVersion,
  prepPackStatusMessage,
  provenanceMethodHint,
  resolveStages,
  screenplayGeneratePayload,
  screenplayResumeActionLabel,
  screenplayResumeOutcomeSummary,
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

describe('prepPackMajorVersion / isLegacyPrepPackFormat', () => {
  it('parses the major version out of a dotted version string', () => {
    expect(prepPackMajorVersion('1.11.1')).toBe(1)
    expect(prepPackMajorVersion('2.0.0')).toBe(2)
    expect(prepPackMajorVersion('10.2.3')).toBe(10)
  })

  it('treats an unparseable version as older than anything real (major 0)', () => {
    expect(prepPackMajorVersion('')).toBe(0)
    expect(prepPackMajorVersion('not-a-version')).toBe(0)
  })

  it('flags the real-world regression pack (1.11.1, pre-2.0.0 architecture narrowing) as legacy', () => {
    expect(isLegacyPrepPackFormat({ prep_pack_version: '1.11.1' })).toBe(true)
  })

  it('does not flag 2.0.0+ packs as legacy', () => {
    expect(isLegacyPrepPackFormat({ prep_pack_version: '2.0.0' })).toBe(false)
    expect(isLegacyPrepPackFormat({ prep_pack_version: '2.3.1' })).toBe(false)
  })
})

describe('assetCoverageText — 真 bug 修复：字段缺失不能渲染成测量后的 0', () => {
  it('says the field is absent (not "0 segments") when segment_indexes is undefined', () => {
    // 1.11.x 等旧产物没有这个字段——运行时是 undefined，不是空数组。
    expect(assetCoverageText(undefined)).toBe('旧版数据，未记录原文覆盖')
  })

  it('reports a real measured zero distinctly from an absent field', () => {
    expect(assetCoverageText([])).toBe('覆盖 0 段原文')
    expect(assetCoverageText([])).not.toContain('旧版数据')
  })

  it('reports the compressed range for a real non-empty measurement', () => {
    expect(assetCoverageText([1, 2, 4])).toBe('覆盖 3 段原文 · 第 1~2,4 段')
    expect(assetCoverageText([3])).toBe('覆盖 1 段原文 · 第 3 段')
  })
})

describe('prepPackStatusMessage', () => {
  it('renames the backend screenplay-era wording to the current prep-pack deliverable name', () => {
    expect(prepPackStatusMessage('剧本已交付，尚无分镜')).toBe('映射包已交付，尚无分镜')
    expect(prepPackStatusMessage('剧本已交付｜分镜生成中')).toBe('映射包已交付｜分镜生成中')
    expect(prepPackStatusMessage('剧本已交付｜分镜停在第 3 镜')).toBe('映射包已交付｜分镜停在第 3 镜')
  })

  it('leaves messages without the stale wording untouched', () => {
    expect(prepPackStatusMessage('状态同步中')).toBe('状态同步中')
  })
})

describe('compressSegmentIndexes', () => {
  it('merges a fully consecutive run into a single a~b segment', () => {
    expect(compressSegmentIndexes([1, 2, 3])).toBe('1~3')
    expect(compressSegmentIndexes([1, 2, 3, 4, 5, 6, 7, 8, 9])).toBe('1~9')
  })

  it('lists fully non-consecutive segment indexes as individual comma-separated values', () => {
    expect(compressSegmentIndexes([1, 3, 5, 7])).toBe('1,3,5,7')
  })

  it('mixes runs and singles per the user-specified format (e.g. "1,3,5~7")', () => {
    expect(compressSegmentIndexes([1, 3, 5, 6, 7])).toBe('1,3,5~7')
    expect(compressSegmentIndexes([1, 2, 3, 7, 8, 9, 10, 11])).toBe('1~3,7~11')
  })

  it('renders a lone index as a single number, not a self-range', () => {
    expect(compressSegmentIndexes([5])).toBe('5')
  })

  it('returns an empty string for an empty input so callers fall back to the plain count', () => {
    expect(compressSegmentIndexes([])).toBe('')
  })

  it('sorts and de-duplicates unordered, repeated input before compressing', () => {
    expect(compressSegmentIndexes([3, 1, 3, 2])).toBe('1~3')
    expect(compressSegmentIndexes([7, 1, 1, 9, 8])).toBe('1,7~9')
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

// 真实 EP1 数据自证（project proj_3ac0b627fa46 / episode ep_3d523ff4d0a4，取自
// data/manju.db 的 screenplay_json）。环境里新落地的登录鉴权拦住了匿名 curl 走查
// API，改为在这里把库里的真实产物直接当固定样本跑一遍渲染管线，比单次 curl 快照
// 更可回归。stages 取自 app/production/revision.py 的真实 stage_order（十步重型
// 流水线遗留，EP1 已发布，全部 status="completed"）——这正是用户反馈里"十个框"的
// 真实来源。
//
// 2.0.0：event_chain/hook/cliffhanger 已从 payload 撤销（见 app/production/
// prep_pack.py 模块 docstring 的 2.0.0 说明），asset_manifest 条目改用
// segment_indexes（原文段号）取代 event_ids——这里改用与原真实样本 event_ids
// 数量一致的段号占位（不是真实回放的段号本身，真实段号需要重新生成一份 2.0.0
// 产物才能拿到；本测试关心的是"字段读取路径正确"，不依赖具体段号取值）。
describe('real EP1 payload walkthrough (no live auth session available; verified against the DB row + backend source directly)', () => {
  const ep1Pack = {
    prep_pack_version: '2.0.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 62 },
    asset_manifest: {
      characters: [
        { identity_id: 'bible:孟浩', display_name: '孟浩', portrait_id: 'portrait_ecc9491a63f4', segment_indexes: [1, 2] },
        { identity_id: 'bible:王有材', display_name: '王有材', portrait_id: 'portrait_bb6813d0733d', segment_indexes: [6, 7] },
        { identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_e01eec6ef5ef', segment_indexes: [10] },
      ],
      scenes: [
        { scene_id: 'scene:大青山山顶', display_name: '大青山山顶', scene_reference_id: 'scene_e6dab3555673', segment_indexes: [1] },
        { scene_id: 'scene:半山青石空地', display_name: '半山青石空地', scene_reference_id: 'scene_a9c9f33fad29', segment_indexes: [14, 17] },
      ],
    },
    appellation_map: [
      { raw_mention: '文生少年', segment_index: 3, identity_id: 'bible:孟浩', canonical_appellation: '孟浩' },
    ],
    coverage_ledger: {
      total_segments: 62,
      delivered: Array.from({ length: 29 }, (_, i) => i + 1),
      merged: [],
      retained_as_context: Array.from({ length: 33 }, (_, i) => i + 30),
      proven_duplicates: [],
      uncovered: [],
    },
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

  it('renders the appellation map row linking the raw in-text mention to the canonical name', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: ep1Pack as any, bible: null, sourceFallback: '第 1 章' }))
    expect(html).toContain('「文生少年」→ 孟浩')
  })
})

// P1 补渲染：asset_manifest.characters[].aliases（1.2.0+）与 asset_manifest.functional_extras
// （1.3.0+，群演/一次性人物）。真实样本取自 EP13（project proj_3ac0b627fa46 / episode
// ep_820ad3cefde7，data/manju.db 的 screenplay_json）：3 个具名角色 aliases 均为
// 空数组（这个项目目前没有产出过非空 aliases 的真实样本），5 条 functional_extras。
// 用真实数据验证字段读取正确性，再用一条合成数据验证 aliases 非空时小签确实渲染
// （覆盖协调方举的"小胖子"场景）。2.0.0：event_ids 改用 segment_indexes（原文段号）
// 取代，数量与原真实样本保持一致，不代表重新回放出的真实段号。
describe('PrepPackView renders aliases and functional_extras (real EP13 data)', () => {
  const ep13AssetManifest = {
    characters: [
      { identity_id: 'bible:孟浩', display_name: '孟浩', portrait_id: 'portrait_ecc9491a63f4', segment_indexes: Array.from({ length: 12 }, (_, i) => i + 1), aliases: [] },
      { identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_e01eec6ef5ef', segment_indexes: [1, 2, 3], aliases: [] },
      { identity_id: 'bible:曹阳', display_name: '曹阳', portrait_id: 'portrait_95288d031252', segment_indexes: [5, 6, 7, 8, 9, 10, 11, 12], aliases: [] },
    ],
    scenes: [
      { scene_id: 'scene:靠山宗外宗区域', display_name: '靠山宗外宗区域', scene_reference_id: 'scene_05d482b34f00', segment_indexes: [1, 2, 3, 4] },
    ],
    functional_extras: [
      { label: '外宗弟子', segment_indexes: [1] },
      { label: '养丹坊中年男子', segment_indexes: [2] },
      { label: '宝阁弟子', segment_indexes: [2] },
      { label: '昨日被坑修士', segment_indexes: [5, 6, 8, 11] },
      { label: '公开区其他修士', segment_indexes: [5, 6, 9, 10] },
    ],
  }

  const buildPack = (assetManifest: typeof ep13AssetManifest) => ({
    prep_pack_version: '2.0.0',
    episode_no: 13,
    episode_scope: { chapter_indexes: [13], source_segment_count: 52 },
    asset_manifest: assetManifest,
    coverage_ledger: {
      total_segments: 52, delivered: Array.from({ length: 27 }, (_, i) => i + 1),
      merged: [], retained_as_context: Array.from({ length: 25 }, (_, i) => i + 28),
      proven_duplicates: [], uncovered: [],
    },
  }) as any

  it('renders all 5 real functional_extras with correct labels and segment coverage, using the icon placeholder (no <img>)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(ep13AssetManifest), bible: null, sourceFallback: '第 13 章' }))
    expect(html).toContain('群演 / 一次性人物')
    expect(html).toContain('群演 / 一次性人物 · 5')
    for (const extra of ep13AssetManifest.functional_extras) {
      expect(html).toContain(extra.label)
    }
    // 段落覆盖数按各自 segment_indexes 长度展示
    expect(html).toContain('覆盖 4 段原文') // 昨日被坑修士 / 公开区其他修士 都是 4
    expect(html).toContain('覆盖 1 段原文') // 外宗弟子 / 养丹坊中年男子 / 宝阁弟子 都是 1
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
        { identity_id: 'bible:李富贵', display_name: '李富贵', portrait_id: 'portrait_x', segment_indexes: [1], aliases: ['小胖子'] },
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
        { identity_id: 'bible:x', display_name: '甲', portrait_id: '', segment_indexes: [], aliases: ['乙名', '  ', '丙名'] },
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

  it('hides the props section entirely when the field is absent (pre-2.0.0 packs)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(ep13AssetManifest), bible: null, sourceFallback: '第 13 章' }))
    expect(html).not.toContain('道具')
  })
})

// 真 bug 回归（协调方截图复现，episode ep_3d523ff4d0a4，prep_pack_version 1.11.1）：
// 转型前的旧版映射包没有 segment_indexes / appellation_map / props 字段——运行时
// 是 undefined，不是空数组。之前的实现把 undefined 兜底成 [] 再取 .length，
// 于是渲染出「覆盖 0 段原文」「称谓映射 0 条 · 本集未发现需要归一的模糊人物称谓」，
// 看起来像真实测量结果，实际含义是"这份数据是旧格式，这个维度压根没跑过"。
describe('PrepPackView legacy-format regression (真 bug：字段缺失不能冒充测量结果)', () => {
  // 真实旧产物形状：无 segment_indexes（用 event_ids 记账）、无 appellation_map、
  // 无 props——这里用 `as any` 显式不带这三个字段，而不是赋 undefined，更贴近
  // 后端真实响应里"这个键压根不存在"的情形。
  const legacyPack = {
    prep_pack_version: '1.11.1',
    episode_no: 7,
    episode_scope: { chapter_indexes: [7], source_segment_count: 40 },
    asset_manifest: {
      characters: [
        { identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x', aliases: [], display_appellation: '许师姐' },
      ],
      scenes: [
        { scene_id: 'scene:靠山宗', display_name: '靠山宗', scene_reference_id: 'scene_y' },
      ],
    },
    coverage_ledger: {
      total_segments: 40, delivered: Array.from({ length: 40 }, (_, i) => i + 1),
      merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  } as any

  it('shows an explicit legacy-format notice naming the actual version, not a silent blank', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: legacyPack, bible: null, sourceFallback: '第 7 章' }))
    expect(html).toContain('旧版映射包')
    expect(html).toContain('1.11.1')
    expect(html).toContain('重新生成映射包')
  })

  it('never renders "覆盖 0 段原文" for a character whose segment_indexes field is simply absent', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: legacyPack, bible: null, sourceFallback: '第 7 章' }))
    expect(html).not.toContain('覆盖 0 段原文')
    expect(html).toContain('旧版数据，未记录原文覆盖')
    expect(html).toContain('许清')
  })

  it('never asserts "本集未发现需要归一的模糊人物称谓" — that claim was never verified on this data', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: legacyPack, bible: null, sourceFallback: '第 7 章' }))
    expect(html).not.toContain('本集未发现需要归一的模糊人物称谓')
    expect(html).not.toContain('称谓映射')
  })

  it('still renders per-character appellation tags from the pre-2.0.0 display_appellation field (unaffected by the legacy gate)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: legacyPack, bible: null, sourceFallback: '第 7 章' }))
    expect(html).toContain('本集：许师姐')
  })

  it('does not show the legacy notice for a 2.0.0+ pack', () => {
    const modernPack = { ...legacyPack, prep_pack_version: '2.0.0' }
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: modernPack, bible: null, sourceFallback: '第 7 章' }))
    expect(html).not.toContain('旧版映射包')
  })
})

// 布局重做（协调方打回：「右边有内容，左边是大片空的」——不是重设计，是把事件链
// 从两栏骨架里挖走）：资源清单是主体内容，不是塞进窄侧栏的附属；任何一类没有
// 数据就整节不渲染，不留空容器；完全没有素材时只留一行纯文字，不留占位框。
describe('PrepPackView layout — no empty containers, resource manifest is the primary content', () => {
  const buildPack = (assetManifest: Record<string, unknown>) => ({
    prep_pack_version: '2.0.0',
    episode_no: 9,
    episode_scope: { chapter_indexes: [9], source_segment_count: 12 },
    asset_manifest: assetManifest,
    coverage_ledger: {
      total_segments: 12, delivered: Array.from({ length: 12 }, (_, i) => i + 1),
      merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  }) as any

  it('renders a single plain-text line — no per-category empty placeholder cards — when nothing was found at all', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack({ characters: [], scenes: [] }), bible: null, sourceFallback: '第 9 章',
    }))
    expect(html).toContain('本集尚未识别到任何人物、场景或道具')
    expect(html).not.toContain('prep-roster')
    expect(html).not.toContain('未列出')
  })

  it('renders only the categories that actually have data — a scenes-only pack shows no 人物/群演/道具 heading', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack({
        characters: [],
        scenes: [{ scene_id: 'scene:x', display_name: '场景X', scene_reference_id: null, segment_indexes: [1, 2] }],
      }),
      bible: null, sourceFallback: '第 9 章',
    }))
    expect(html).toContain('出场场景')
    expect(html).not.toContain('出场人物')
    expect(html).not.toContain('群演')
    expect(html).not.toContain('道具')
    expect(html).not.toContain('本集尚未识别到任何人物、场景或道具')
  })

  it('compresses episode scope into a single small-text line, not a card section', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack({ characters: [], scenes: [] }), bible: null, sourceFallback: '第 9 章',
    }))
    expect(html).toContain('prep-scope-line')
    expect(html).toContain('原文段 12 段')
    expect(html).not.toContain('本集范围')
  })

  it('merges the appellation table into the character card (no separate half-screen block) and keeps it collapsed by default', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: {
        ...buildPack({
          characters: [{ identity_id: 'bible:许清', display_name: '许清', portrait_id: '', segment_indexes: [1], display_appellation: '许师姐' }],
          scenes: [],
        }),
        appellation_map: [{ raw_mention: '许师姐', segment_index: 1, identity_id: 'bible:许清', canonical_appellation: '许清' }],
      },
      bible: null, sourceFallback: '第 9 章',
    }))
    expect(html).toContain('本集：许师姐')
    // <details> without an `open` attribute renders collapsed by default.
    expect(html).toContain('<details')
    expect(html).not.toMatch(/<details[^>]*\bopen\b/)
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
    prep_pack_version: '2.0.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 10 },
    asset_manifest: { characters, scenes: [] },
    coverage_ledger: {
      total_segments: 10, delivered: Array.from({ length: 10 }, (_, i) => i + 1),
      merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  }) as any

  it('shows both this-episode wording and the canonical name when they differ, distinguishably tagged', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x',
        segment_indexes: [1], visual_entity_id: 'bible:许清',
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
        segment_indexes: [1], visual_entity_id: 'bible:孟浩', display_appellation: '孟浩',
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
        segment_indexes: [1],
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
        segment_indexes: [1], provenance: { method: 'candidate_verdict' },
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    // title 现在是覆盖区间全文 + 绑定依据两行拼接（见 assetCoverageText 溢出修复：
    // meta 的 title 不再只放 provenanceHint，还要能悬停拿到完整压缩区间），
    // 绑定依据本身仍然在 title 里，只是不再是唯一内容。
    expect(html).toContain('title="覆盖 1 段原文 · 第 1 段\n绑定依据：候选判别"')
  })

  it('renders nothing extra when provenance is absent (pre-1.6.0 packs)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x',
        segment_indexes: [1],
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
    prep_pack_version: '2.0.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 5 },
    asset_manifest: { characters: [], scenes: [] },
    coverage_ledger: coverageLedger,
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

// 真 bug 修复（协调方截图复现）：资源卡「覆盖 N 段原文 · 第 X~Y,Z 段」这一行没有
// 任何换行/截断处理，真实 EP1 数据里主角一集覆盖三十多段是常态（孟浩 34 段、
// 王有材 15 段），压缩后的区间字符串是一长串不含空格的数字，默认换行找不到断点，
// 会整段溢出卡片、和相邻内容叠印，糊成一团读不出来。
//
// 修复在 CSS 层（styles/ScriptPage.css 的 .prep-roster-meta：nowrap + ellipsis +
// overflow:hidden），这里的渲染测试测不到"是否真的单行不溢出"（jsdom 都没装，
// 更不用说布局引擎），只能锁住 DOM 契约：不管段数多少，完整的压缩区间字符串
// 始终原样出现在文档里（渲染文本 + title 双重存在），CSS 截断只影响视觉呈现，
// 不会把数据本身删掉——用户 2026-08-25 专门要过 `1,3,5~7` 这套压缩格式，
// 不许为了排版好看而丢信息。
describe('PrepPackView coverage text stays intact and recoverable at real EP1 scale (overflow/overlap fix)', () => {
  // 真实量级样本：EP1 共 62 段原文，孟浩覆盖 34 段（协调方给出的真实分布形状，
  // 压缩后形如 "3~7,10~12,14~17,20,22,...32~34…"）；许清只覆盖 3 段，作为短卡对照。
  const mengHaoSegments = [
    3, 4, 5, 6, 7, 10, 11, 12, 14, 15, 16, 17, 20, 22, 24, 25, 28, 29, 30,
    32, 33, 34, 36, 37, 40, 41, 42, 45, 47, 50, 51, 55, 58, 60,
  ]
  const expectedMengHaoRange = compressSegmentIndexes(mengHaoSegments)

  const buildPack = () => ({
    prep_pack_version: '2.0.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 62 },
    asset_manifest: {
      characters: [
        { identity_id: 'bible:孟浩', display_name: '孟浩', portrait_id: '', segment_indexes: mengHaoSegments },
        { identity_id: 'bible:许清', display_name: '许清', portrait_id: '', segment_indexes: [3, 10, 24] },
      ],
      scenes: [
        { scene_id: 'scene:大青山山顶', display_name: '大青山山顶', scene_reference_id: '', segment_indexes: Array.from({ length: 24 }, (_, i) => i + 1) },
      ],
    },
    coverage_ledger: {
      total_segments: 62, delivered: mengHaoSegments, merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  }) as any

  it('locks the real 34-segment compressed shape so a future edit cannot silently shorten the sample without anyone noticing', () => {
    expect(mengHaoSegments).toHaveLength(34)
    expect(expectedMengHaoRange).toBe('3~7,10~12,14~17,20,22,24~25,28~30,32~34,36~37,40~42,45,47,50~51,55,58,60')
  })

  it('renders the full 34-segment compressed range as visible text — CSS truncation clips display, never the data', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(), bible: null, sourceFallback: '第 1 章' }))
    expect(html).toContain(`覆盖 34 段原文 · 第 ${expectedMengHaoRange} 段`)
  })

  it('also carries the full 34-segment range in the meta span title, so it is recoverable on hover after the line is visually clipped', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(), bible: null, sourceFallback: '第 1 章' }))
    expect(html).toContain(`title="覆盖 34 段原文 · 第 ${expectedMengHaoRange} 段`)
  })

  it('renders the short 3-segment card with the same untruncated text+title contract as the 34-segment card', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(), bible: null, sourceFallback: '第 1 章' }))
    expect(html).toContain('覆盖 3 段原文 · 第 3,10,24 段')
    expect(html).toContain('title="覆盖 3 段原文 · 第 3,10,24 段"')
  })

  it('carries the 24-segment scene coverage range in both text and title (props/scenes get the same treatment as characters)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(), bible: null, sourceFallback: '第 1 章' }))
    const expectedSceneRange = compressSegmentIndexes(Array.from({ length: 24 }, (_, i) => i + 1))
    expect(expectedSceneRange).toBe('1~24')
    expect(html).toContain(`覆盖 24 段原文 · 第 ${expectedSceneRange} 段`)
    expect(html).toContain(`title="覆盖 24 段原文 · 第 ${expectedSceneRange} 段"`)
  })

  it('gives the long scene name a title too, so a truncated display name is still recoverable on hover (顺带修复)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, { pack: buildPack(), bible: null, sourceFallback: '第 1 章' }))
    expect(html).toContain('title="大青山山顶"')
  })
})

// 顺带修复：本集称谓/别名小签（.prep-roster-alias）原来是 flex:none + 无宽度上限，
// 长称谓会撑破徽标本身、把整行名字挤出卡片。CSS 加了 max-width + 省略号兜底
// （styles/ScriptPage.css），title 也从纯占位说明改成拼接真实称谓文本——原来
// title 只有"本集原文称谓；谱内正名见前"这句解释，看不到实际内容，截断后更看不到。
describe('PrepPackView appellation/alias badge title carries the real content, not just the static hint (顺带修复：长称谓同类溢出)', () => {
  const buildPack = (characters: Record<string, unknown>[]) => ({
    prep_pack_version: '2.0.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 10 },
    asset_manifest: { characters, scenes: [] },
    coverage_ledger: {
      total_segments: 10, delivered: Array.from({ length: 10 }, (_, i) => i + 1),
      merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  }) as any

  it('embeds the appellation text itself in the badge title (not just the generic explanation)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:许清', display_name: '许清', portrait_id: 'portrait_x',
        segment_indexes: [1], display_appellation: '银色长袍女子',
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).toContain('title="本集原文称谓；谱内正名见前：银色长袍女子"')
  })

  it('embeds the joined alias text itself in the badge title', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPack([{
        identity_id: 'bible:x', display_name: '甲', portrait_id: '',
        segment_indexes: [1], aliases: ['乙名', '丙名'],
      }]),
      bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).toContain('title="本集称谓：乙名、丙名"')
  })
})

describe('PrepPackView 定妆照占位四态（用户拍板 2026-08-31：不许一律显示"无定妆照"）', () => {
  const buildPackWithCharacter = (character: Record<string, unknown>) => ({
    prep_pack_version: '2.0.0',
    episode_no: 1,
    episode_scope: { chapter_indexes: [1], source_segment_count: 5 },
    asset_manifest: { characters: [character], scenes: [] },
    coverage_ledger: {
      total_segments: 5, delivered: [1, 2, 3, 4, 5],
      merged: [], retained_as_context: [], proven_duplicates: [], uncovered: [],
    },
  }) as any

  const noImageCharacter = {
    identity_id: 'bible:张三', display_name: '张三', portrait_id: 'portrait_x', segment_indexes: [1],
  }

  it('本轮 refs 任务正在为这个角色出图 -> 显示"定妆照生成中"，不是"无定妆照"', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPackWithCharacter(noImageCharacter), bible: null, sourceFallback: '第 1 章',
      project: { refs_status: 'running', refs_target: null },
    }))
    expect(html).toContain('定妆照生成中')
    expect(html).not.toContain('无定妆照')
  })

  it('出图任务失败且命中这个角色 -> 显示"定妆照生成失败"，且给出可点击的补图入口', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPackWithCharacter(noImageCharacter), bible: null, sourceFallback: '第 1 章',
      project: { id: 'proj_1', refs_status: 'failed', refs_target: '张三' } as any,
    }))
    expect(html).toContain('定妆照生成失败')
    expect(html).toMatch(/<a[^>]+href="\/projects\/proj_1\/bible"[^>]*>定妆照生成失败<\/a>/)
  })

  it('既没在跑也没失败 -> 显示"定妆照待生成"', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPackWithCharacter(noImageCharacter), bible: null, sourceFallback: '第 1 章',
      project: { refs_status: 'ready', refs_target: null },
    }))
    expect(html).toContain('定妆照待生成')
  })

  it('project 未传（旧调用点/测试常见形态）时不崩溃，落到"定妆照待生成"', () => {
    const html = renderToStaticMarkup(createElement(PrepPackView, {
      pack: buildPackWithCharacter(noImageCharacter), bible: null, sourceFallback: '第 1 章',
    }))
    expect(html).toContain('定妆照待生成')
  })
})
