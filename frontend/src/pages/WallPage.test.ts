import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError, type Shot, type ShotVersion, type StoryboardPackSegment } from '../api'
import {
  bulkGenerateDisabledReason,
  bulkGenerateEstimate,
  formatResolution,
  isSegmentShot,
  parseTechnicalValidation,
  qualificationChangedRetryVersion,
  referenceImageLabel,
  resolveCurrentVersion,
  resolveSelectedShotId,
  segmentGenerateDisabledReason,
  segmentPhase,
  segmentPhaseCounts,
  stampClassForStatus,
  versionStatusLabel,
  versionStatusTone,
} from './WallPage'

// 生成台只剩段视图一条渲染路径（2026-08-26 用户拍板：storyboard_pack/2.0.1，一个
// 15 秒段 = shots 表一行）。shot() 只构造满足 Shot 接口必填字段的最小基座，
// packShot() 在其上叠加 storyboard_pack_segment 才是生成台唯一消费的形状。
function shot(overrides: Partial<Shot> = {}): Shot {
  return {
    id: 's1', episode_id: 'e1', shot_no: 1, duration_s: 15, shot_size: '', camera_move: '',
    scene_time: '', scene_name: '', scene_setting: '',
    characters: [], action_desc: '',
    first_frame_desc: '', last_frame_desc: '', source_excerpt: '',
    narration: '', dialogues: [], transition: '',
    continuity_from_prev: 0, adopted_version_id: null, versions: [], video_stale: false,
    ...overrides,
  }
}

function packSegment(overrides: Partial<StoryboardPackSegment> = {}): StoryboardPackSegment {
  return {
    segment_no: 1, duration_s: 15, synopsis: '少年拿到密信', source_segment_indexes: [1, 2],
    prompt_text: '电影级预告片质感，多镜头叙事……',
    shot_count: 3, dialogue: [], resources: { characters: [], scenes: [], props: [] },
    degraded_capabilities: [], beats: [], beat_ids: [], target_model: 'seedance_2',
    storyboard_version: '2.0.1',
    ...overrides,
  }
}

function packShot(overrides: Partial<Shot> = {}, segmentOverrides: Partial<StoryboardPackSegment> = {}): Shot {
  return shot({
    storyboard_pack_segment: packSegment(segmentOverrides),
    ...overrides,
  })
}

function version(overrides: Partial<ShotVersion> = {}): ShotVersion {
  return {
    id: 'v1', version_no: 1, prompt_text: '', status: 'succeeded', latency_s: 5.2,
    ...overrides,
  }
}

describe('段落是唯一渲染单位', () => {
  it('storyboard_pack_segment 非 null 才是可展示的段落行', () => {
    expect(isSegmentShot(shot())).toBe(false)
    expect(isSegmentShot(packShot())).toBe(true)
  })
})

describe('当前版本解析', () => {
  it('优先展示已采纳版本，即使它不是版本号最大的一条', () => {
    const s = packShot({
      adopted_version_id: 'v1',
      versions: [version({ id: 'v1', version_no: 1 }), version({ id: 'v2', version_no: 2, status: 'failed' })],
    })
    expect(resolveCurrentVersion(s)?.id).toBe('v1')
  })

  it('未采纳时回退到版本号最大的最新尝试', () => {
    const s = packShot({
      versions: [version({ id: 'v1', version_no: 1 }), version({ id: 'v2', version_no: 3 }), version({ id: 'v3', version_no: 2 })],
    })
    expect(resolveCurrentVersion(s)?.id).toBe('v2')
  })

  it('没有任何尝试时返回 null，不编造数据', () => {
    expect(resolveCurrentVersion(packShot({ versions: [] }))).toBeNull()
  })
})

describe('段落生成阶段与统计', () => {
  it('四种阶段：待生成 / 生成中 / 已完成 / 需处理', () => {
    expect(segmentPhase(packShot({ versions: [] }))).toBe('pending')
    expect(segmentPhase(packShot({ versions: [version({ status: 'queued' })] }))).toBe('generating')
    expect(segmentPhase(packShot({ versions: [version({ status: 'waiting_provider' })] }))).toBe('generating')
    expect(segmentPhase(packShot({ versions: [version({ status: 'succeeded' })] }))).toBe('succeeded')
    expect(segmentPhase(packShot({ versions: [version({ status: 'failed' })] }))).toBe('attention')
    expect(segmentPhase(packShot({ versions: [version({ status: 'waiting_human' })] }))).toBe('attention')
    expect(segmentPhase(packShot({ versions: [version({ status: 'quarantined' })] }))).toBe('attention')
  })

  it('按全部段落汇总四态计数', () => {
    const shots = [
      packShot({ id: 's1' }, {}),
      packShot({ id: 's2', versions: [version({ status: 'running' })] }),
      packShot({ id: 's3', versions: [version({ status: 'succeeded' })] }),
      packShot({ id: 's4', versions: [version({ status: 'failed' })] }),
    ]
    expect(segmentPhaseCounts(shots)).toEqual({ pending: 1, generating: 1, succeeded: 1, attention: 1 })
  })
})

describe('选中段落的稳定性', () => {
  it('当前选中仍存在时保持不变', () => {
    expect(resolveSelectedShotId([{ id: 's1' }, { id: 's2' }], 's2')).toBe('s2')
  })
  it('当前选中已消失时回退到第一段', () => {
    expect(resolveSelectedShotId([{ id: 's1' }, { id: 's2' }], 's9')).toBe('s1')
  })
  it('没有任何段落时返回 null', () => {
    expect(resolveSelectedShotId([], 's1')).toBeNull()
  })
})

describe('技术校验解析', () => {
  it('解出时长、体积与校验结论', () => {
    const raw = JSON.stringify({
      passed: true, issues: [], evidence: { duration_s: 15.104, size_bytes: 6164748 },
    })
    expect(parseTechnicalValidation(raw)).toEqual({
      passed: true, issues: [], durationS: 15.104, sizeBytes: 6164748,
    })
  })
  it('空值或非法 JSON 返回 null，不抛错', () => {
    expect(parseTechnicalValidation(null)).toBeNull()
    expect(parseTechnicalValidation(undefined)).toBeNull()
    expect(parseTechnicalValidation('{not json')).toBeNull()
  })
})

describe('分辨率展示', () => {
  it('有宽高时格式化为 宽×高', () => {
    expect(formatResolution(720, 1280)).toBe('720×1280')
  })
  it('尚未取得宽高时提示解析中，不显示 0×0', () => {
    expect(formatResolution(0, 0)).toBe('解析中…')
  })
})

describe('参考图展示标签', () => {
  it('人物/场景参考图带身份标签，其余退回来源', () => {
    expect(referenceImageLabel({ id: 'r1', type: 'character', source: 'asset_library', entity_name: '孟浩' }))
      .toBe('人物 · 孟浩')
    expect(referenceImageLabel({ id: 'r2', type: 'scene', source: 'asset_library', entity_name: '山巅' }))
      .toBe('场景 · 山巅')
    expect(referenceImageLabel({ id: 'r3', type: 'plot_key_frame', source: 'seedream_generated' }))
      .toBe('seedream_generated')
  })
})

describe('生成按钮可用性判据', () => {
  it('提交中时禁用', () => {
    expect(segmentGenerateDisabledReason({ submitting: true, currentStatus: null, eligible: true, blockers: [] }))
      .toBe('正在提交生成请求')
  })
  it('已有活动任务时禁用，不允许重复提交', () => {
    expect(segmentGenerateDisabledReason({ submitting: false, currentStatus: 'running', eligible: true, blockers: [] }))
      .toBe('当前已有任务在处理中')
    expect(segmentGenerateDisabledReason({ submitting: false, currentStatus: 'queued', eligible: true, blockers: [] }))
      .toBe('当前已有任务在处理中')
  })
  it('生成资格尚未加载完成时给出等待文案，不是假装通过', () => {
    expect(segmentGenerateDisabledReason({ submitting: false, currentStatus: null, eligible: null, blockers: [] }))
      .toBe('正在核对生成资格')
  })
  it('资格未通过时把 blockers 原文透出', () => {
    expect(segmentGenerateDisabledReason({
      submitting: false, currentStatus: null, eligible: false, blockers: ['分镜尚未确认'],
    })).toBe('分镜尚未确认')
    expect(segmentGenerateDisabledReason({ submitting: false, currentStatus: null, eligible: false, blockers: [] }))
      .toBe('当前生成资格未通过')
  })
  it('满足全部条件时可用', () => {
    expect(segmentGenerateDisabledReason({ submitting: false, currentStatus: 'succeeded', eligible: true, blockers: [] }))
      .toBe('')
    expect(segmentGenerateDisabledReason({ submitting: false, currentStatus: null, eligible: true, blockers: [] }))
      .toBe('')
  })
})

describe('八态状态文案与色调', () => {
  it('覆盖任务状态流转要求的全部状态', () => {
    expect(versionStatusLabel('queued')).toBe('排队中')
    expect(versionStatusLabel('waiting_provider')).toBe('等待生成服务')
    expect(versionStatusLabel('running')).toBe('生成中')
    expect(versionStatusLabel('succeeded')).toBe('已完成')
    expect(versionStatusLabel('failed')).toBe('失败')
    expect(versionStatusLabel('waiting_human')).toBe('等待人工处理')
    expect(versionStatusLabel('quarantined')).toBe('已隔离（不可用）')
  })
  it('未知状态原样透出，不假装成已知值', () => {
    expect(versionStatusLabel('some_new_status')).toBe('some_new_status')
  })
  it('色调三分：成功绿、失败/隔离红、活动态金，其余灰', () => {
    expect(versionStatusTone('succeeded')).toBe('green')
    expect(versionStatusTone('failed')).toBe('red')
    expect(versionStatusTone('quarantined')).toBe('red')
    expect(versionStatusTone('running')).toBe('gold')
    expect(versionStatusTone('waiting_human')).toBe('grey')
    expect(stampClassForStatus('succeeded')).toBe('stamp green')
  })
})

// 「生成所有视频」批量入口（2026-08-26 加）：按钮 -> DecisionDialog -> POST
// /episodes/{id}/generate（only_incomplete=true）。判据必须与后端
// _generate_episode_core 的 completed_ids 查询同一口径：只有已采纳或已有成功候选
// 的段才算「已完成」被跳过，生成中/需处理的段仍会被送进这次请求。
describe('批量生成预估：口径对齐后端 only_incomplete（只有已完成的段被跳过）', () => {
  const pending = shot({ id: 's-pending' })
  const generating = shot({
    id: 's-generating', versions: [version({ id: 'v-g', status: 'running' })],
  })
  const succeeded = shot({
    id: 's-done', versions: [version({ id: 'v-s', status: 'succeeded' })],
  })
  const attention = shot({
    id: 's-attn', versions: [version({ id: 'v-a', status: 'failed' })],
  })

  it('已完成的段不计入提交数', () => {
    const estimate = bulkGenerateEstimate([pending, generating, succeeded, attention])
    expect(estimate.totalCount).toBe(4)
    expect(estimate.succeededCount).toBe(1)
    expect(estimate.generatingCount).toBe(1)
    expect(estimate.attentionCount).toBe(1)
    expect(estimate.pendingCount).toBe(1)
    expect(estimate.submitCount).toBe(3)
  })

  it('全部已完成时提交数是 0，不虚报', () => {
    const estimate = bulkGenerateEstimate([succeeded])
    expect(estimate.submitCount).toBe(0)
  })
})

describe('批量生成按钮可用性判据', () => {
  it('提交中时禁用', () => {
    expect(bulkGenerateDisabledReason({ submitting: true, eligible: true, blockers: [], submitCount: 3 }))
      .toBe('正在提交批量生成请求')
  })
  it('资格尚未加载完成时给出等待文案，不是假装通过', () => {
    expect(bulkGenerateDisabledReason({ submitting: false, eligible: null, blockers: [], submitCount: 3 }))
      .toBe('正在核对生成资格')
  })
  it('资格未通过时把 blockers 原文透出，不吞掉真实原因', () => {
    expect(bulkGenerateDisabledReason({
      submitting: false, eligible: false, blockers: ['分镜尚未确认'], submitCount: 3,
    })).toBe('分镜尚未确认')
    expect(bulkGenerateDisabledReason({ submitting: false, eligible: false, blockers: [], submitCount: 3 }))
      .toBe('当前生成资格未通过')
  })
  it('没有待提交片段时禁用并如实说明原因', () => {
    expect(bulkGenerateDisabledReason({ submitting: false, eligible: true, blockers: [], submitCount: 0 }))
      .toBe('全部片段已完成，无需再次生成')
  })
  it('满足全部条件时可用', () => {
    expect(bulkGenerateDisabledReason({ submitting: false, eligible: true, blockers: [], submitCount: 3 }))
      .toBe('')
  })
})

describe('CON-409（REVIEW_QUALIFICATION_CHANGED）自动重试：只认这一种 409，只重试一次', () => {
  it('命中时直接取服务端已经带回来的新资格版本号，不用再发一次 GET', () => {
    const err = new ApiError(
      409, '上游或资产资格已变化，请重新预演', 'REVIEW_QUALIFICATION_CHANGED', undefined, undefined,
      { code: 'REVIEW_QUALIFICATION_CHANGED', qualification: { qualification_version: 'fresh-v2' } },
    )
    expect(qualificationChangedRetryVersion(err)).toBe('fresh-v2')
  })
  it('其余 409（资格未通过/资产未就绪等）原样交还，不在这里悄悄吞掉', () => {
    const blocked = new ApiError(
      409, '当前不可执行正向媒体生产', 'REVIEW_PRODUCTION_BLOCKED', undefined, undefined,
      { code: 'REVIEW_PRODUCTION_BLOCKED' },
    )
    expect(qualificationChangedRetryVersion(blocked)).toBeNull()
  })
  it('非 409 或非 ApiError 都不触发重试', () => {
    expect(qualificationChangedRetryVersion(new Error('network down'))).toBeNull()
    expect(qualificationChangedRetryVersion(new ApiError(500, 'boom'))).toBeNull()
  })
})
