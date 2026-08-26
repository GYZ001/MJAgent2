import { describe, expect, it } from 'vitest'
import type { Shot, StoryboardPackSegment, StoryboardStatus } from '../api'
import {
  isStoryboardPackSegmentShot,
  isStoryboardProblemShot,
  storyboardGateIssueLabel,
  storyboardEmptyCopy,
  storyboardPackBeatOverview,
  storyboardPackDegradedCapabilitiesExportText,
  storyboardPackResourceGapSummary,
  storyboardPackTargetModelLabel,
  storyboardProgressCopy,
  storyboardPrimaryAction,
  storyboardShotCheckpointLabel,
  storyboardStartPreviewCopy,
  storyboardToolbarActions,
} from './BoardPage'

// 分镜台只剩段视图一条渲染路径（2026-08-26 用户拍板：旧逐镜编辑连同它绑定的经典
// 字段形状已整块拆除，测试期没有需要兼容的重要数据）。这里的 shot() 只是构造一个
// 满足 Shot 接口必填字段的最小基座，packShot() 在其上叠加 storyboard_pack_segment
// 才是分镜台唯一消费的形状。
function shot(overrides: Partial<Shot> = {}): Shot {
  return {
    id: 's1', episode_id: 'e1', shot_no: 1, duration_s: 15, shot_size: '', camera_move: '',
    scene_time: '', scene_name: '', scene_setting: '',
    characters: [], action_desc: '',
    first_frame_desc: '', last_frame_desc: '', source_excerpt: '',
    narration: '', dialogues: [], transition: '',
    continuity_from_prev: 0, adopted_version_id: null, est_cost_cny: 0, versions: [], video_stale: false,
    ...overrides,
  }
}

function packSegment(overrides: Partial<StoryboardPackSegment> = {}): StoryboardPackSegment {
  return {
    segment_no: 1, duration_s: 15, synopsis: '少年拿到密信', source_segment_indexes: [1, 2],
    prompt_text: '电影级预告片质感，多镜头叙事，镜头之间硬切。镜头1……',
    shot_count: 3, dialogue: [], resources: { characters: [], scenes: [], props: [] },
    degraded_capabilities: [], beats: [{ beat_id: 'B01', summary: '获得密信', segment_indexes: [1, 2] }],
    beat_ids: ['B01'], target_model: 'seedance_2',
    storyboard_version: '2.0.0',
    ...overrides,
  }
}

/** 分镜台的段落行：storyboard_pack_segment 是这一行唯一权威内容。 */
function packShot(overrides: Partial<Shot> = {}, segmentOverrides: Partial<StoryboardPackSegment> = {}): Shot {
  return shot({
    storyboard_pack_segment: packSegment(segmentOverrides),
    ...overrides,
  })
}

function storyboardStatus(overrides: Partial<StoryboardStatus> = {}): StoryboardStatus {
  return {
    contract_version: 'storyboard-workspace.v1', snapshot_version: 1, state_fingerprint: 'fp',
    state: 'paused', headline: '局部修复已暂停', screenplay_available: true,
    planned_shots: 17, produced_shots: 9, validated_shots: 2,
    draft_shots: 9, safe_checkpoint_shots: 2, pending_revalidation_shots: 7,
    resume_from_shot: 3, final_shot_valid: false, hard_gates_passed: false,
    confirmed: false, editable: true, confirmable: false, recommended_action: 'resume_storyboard',
    ...overrides,
  }
}

describe('分镜台状态文案与问题筛选', () => {
  it('零段落时按真实任务终态解释空白区', () => {
    expect(storyboardEmptyCopy(storyboardStatus({ state: 'running' }))).toContain('正在生成')
    expect(storyboardEmptyCopy(storyboardStatus({ state: 'failed' }))).toContain('任务未完成')
  })

  it('质量优化建议不混入必须处理的问题段', () => {
    const value = shot({ qa_warnings: ['画面动作含可精简的细节词'] })
    expect(isStoryboardProblemShot(value)).toBe(false)
  })

  it('运行中只提供暂停，所有有内容的停止态都允许清空', () => {
    expect(storyboardToolbarActions('running')).toEqual({ pause: true, clear: false })
    expect(storyboardToolbarActions('paused')).toEqual({ pause: false, clear: true })
    expect(storyboardToolbarActions('failed')).toEqual({ pause: false, clear: true })
    expect(storyboardToolbarActions('ready_to_confirm')).toEqual({ pause: false, clear: true })
    expect(storyboardToolbarActions('confirmed')).toEqual({ pause: false, clear: true })
  })

  it('把门禁字段翻译为可执行的制作语言', () => {
    expect(storyboardGateIssueLabel('shots[1](shot_no=2).first_frame_desc 太短'))
      .toBe('第 2 段：生成起点 太短')
    expect(storyboardGateIssueLabel('shot_no=3.primary_action 缺失'))
      .toBe('第 3 段：镜头动作 缺失')
    expect(storyboardGateIssueLabel('QA 门禁未通过')).toBe('质检 必检项未通过')
  })

  it('明确区分目标、工作副本和已校验段落', () => {
    const copy = storyboardProgressCopy(storyboardStatus({ final_shot_valid: true }))
    expect(copy.summary).toBe('目标 17 段 · 工作副本 9 段 · 已校验 2 段')
    expect(copy.detail).toContain('第 3–9 段仍待校验')
    expect(copy.detail).toContain('从第 3 段继续修复')
    expect(copy.detail).toContain('人工确认前')
  })

  it('完整收束但门禁失败时明确重开修复而不是追加段落', () => {
    const status = storyboardStatus({
      planned_shots: 14,
      produced_shots: 14,
      validated_shots: 14,
      draft_shots: 14,
      safe_checkpoint_shots: 14,
      pending_revalidation_shots: 0,
      resume_from_shot: 15,
      resume_mode: 'repair_existing',
      final_shot_valid: true,
      hard_gate_issue_count: 2,
    })

    const progress = storyboardProgressCopy(status)
    expect(progress.detail).toContain('重开整集修复')
    expect(progress.detail).toContain('不是从第 15 段续写')

    const preview = storyboardStartPreviewCopy({
      preview_token: 'preview-1',
      action: 'resume',
      resume_mode: 'repair_existing',
      kept_validated_shots: 14,
      planned_shots: 14,
      remaining_shots: 0,
      checkpoint: { available: true, phase: 'SUCCEEDED', resume_from_shot: 15 },
      current_gate_issue_count: 2,
    })
    expect(preview.title).toBe('继续修复视频提示词')
    expect(preview.confirmLabel).toBe('开始修复')
    expect(preview.summary).toContain('现有 14 段保持不变')
    expect(preview.detail).toContain('不是从第 15 段续写')
  })

  it('完整段落只缺发布证据时明确继续审读发布且不改写段落', () => {
    const status = storyboardStatus({
      planned_shots: 14,
      produced_shots: 14,
      validated_shots: 14,
      draft_shots: 14,
      safe_checkpoint_shots: 14,
      pending_revalidation_shots: 0,
      resume_mode: 'finalize_evidence',
      final_shot_valid: true,
      hard_gates_passed: true,
    })

    expect(storyboardProgressCopy(status).detail).toContain('不会改写现有段落')

    const preview = storyboardStartPreviewCopy({
      preview_token: 'preview-2',
      action: 'resume',
      resume_mode: 'finalize_evidence',
      kept_validated_shots: 14,
      planned_shots: 14,
      remaining_shots: 0,
      checkpoint: { available: true, phase: 'WAITING_HUMAN', resume_from_shot: 15 },
    })
    expect(preview.title).toBe('完成视频提示词发布证据')
    expect(preview.confirmLabel).toBe('继续审读发布')
    expect(preview.detail).toContain('仅继续冷观众审读')
  })

  it('把一次观看权威提升为发布证据阶段的主操作', () => {
    const status = storyboardStatus({
      planned_shots: 14,
      produced_shots: 14,
      validated_shots: 14,
      resume_mode: 'finalize_evidence',
      final_shot_valid: true,
      hard_gates_passed: true,
    })

    expect(storyboardPrimaryAction(
      status,
      { ready: false, status: 'needs_review', blockers: [] },
      {
        artifact_id: 'review-1',
        version: 1,
        status: 'validated',
        decision: 'pass',
        low_percentile: {},
        inference_variance: 0,
        reason: '',
      },
    )).toEqual({
      intent: 'activate_ai_one_watch',
      label: '运行 AI 一次观看模拟',
    })

    expect(storyboardPrimaryAction(
      status,
      {
        ready: false,
        status: 'awaiting_republish',
        authority_mode: 'ai_simulation',
        blockers: [],
      },
      null,
    )).toEqual({
      intent: 'resume_storyboard',
      label: '完成发布证据',
    })
  })

  it('真实门禁失败时仍要求继续修复，不能进入一次观看或确认', () => {
    const action = storyboardPrimaryAction(
      storyboardStatus({
        state: 'failed',
        resume_mode: 'repair_existing',
        hard_gates_passed: false,
        hard_gate_issue_count: 1,
      }),
      { ready: false, status: 'needs_review', blockers: [] },
      {
        artifact_id: 'review-1',
        version: 1,
        status: 'validated',
        decision: 'pass',
        low_percentile: {},
        inference_variance: 0,
        reason: '',
      },
    )

    expect(action).toEqual({
      intent: 'resume_storyboard',
      label: '继续生成视频提示词',
    })
  })

  it('在段落轨道区分已校验和待校验工作副本', () => {
    expect(storyboardShotCheckpointLabel(2, storyboardStatus())?.label).toBe('已校验')
    expect(storyboardShotCheckpointLabel(3, storyboardStatus())?.label).toBe('待校验')
    expect(storyboardShotCheckpointLabel(3, storyboardStatus({ state: 'confirmed' }))).toBeNull()
  })
})

describe('分镜台段落展示（docs/STORYBOARD_PROMPT_IR_DESIGN.md 冻结契约）', () => {
  it('storyboard_pack_segment 非 null 是这一行有内容可展示的标记', () => {
    expect(isStoryboardPackSegmentShot(shot())).toBe(false)
    expect(isStoryboardPackSegmentShot(packShot())).toBe(true)
  })

  it('契约自己的模型词表与视频模型选择器的供应商 key 不是同一套', () => {
    expect(storyboardPackTargetModelLabel('seedance_2')).toBe('Seedance 2.0')
    expect(storyboardPackTargetModelLabel('minimax_h3')).toBe('MiniMax H3')
    expect(storyboardPackTargetModelLabel('unknown_model')).toBe('unknown_model')
  })

  it('节拍概览按 beats（自包含摘要）反推段落去向，按 beat_id 升序排列', () => {
    const shots = [
      packShot({ id: 's1' }, { segment_no: 1, beats: [{ beat_id: 'B02', summary: '密信现踪', segment_indexes: [3] }] }),
      packShot({ id: 's2' }, {
        segment_no: 2,
        beats: [
          { beat_id: 'B01', summary: '获得密信', segment_indexes: [1, 2] },
          { beat_id: 'B02', summary: '', segment_indexes: [3] },
        ],
      }),
      packShot({ id: 's3' }, { segment_no: 3, beats: [{ beat_id: 'B01', summary: '', segment_indexes: [1, 2] }] }),
      shot({ id: 's4' }), // 没有 storyboard_pack_segment 的行必须被忽略而不是报错
    ]
    expect(storyboardPackBeatOverview(shots)).toEqual([
      { beat_id: 'B01', summary: '获得密信', segment_nos: [2, 3] },
      { beat_id: 'B02', summary: '密信现踪', segment_nos: [1, 2] },
    ])
  })

  it('节拍概览在没有任何段落行时返回空数组，不是抛错或编造数据', () => {
    expect(storyboardPackBeatOverview([shot(), shot({ id: 's2' })])).toEqual([])
  })

  it('资源缺口统计按段落引用计数，区分有素材与只有文字描述两种状态', () => {
    const shots = [
      packShot({ id: 's1' }, {
        resources: {
          characters: [
            { identity_id: 'char-1', portrait_id: 'portrait-1', description: '' },
            { identity_id: 'char-2', portrait_id: null, description: '一名少年' },
          ],
          scenes: [
            { scene_id: 'scene-1', scene_reference_id: null, description: '破旧的柴房' },
          ],
          props: [{ label: '密信', description: '一封火漆密封的信' }],
        },
      }),
      packShot({ id: 's2' }, {
        resources: {
          characters: [{ identity_id: 'char-1', portrait_id: 'portrait-1', description: '' }],
          scenes: [{ scene_id: 'scene-2', scene_reference_id: 'ref-2', description: '' }],
          props: [],
        },
      }),
    ]
    expect(storyboardPackResourceGapSummary(shots)).toEqual({
      charactersLinked: 2, charactersTotal: 3,
      scenesLinked: 1, scenesTotal: 2,
      propsTotal: 1,
    })
  })

  it('degraded_capabilities 必须显示出来，导出清单带段号回指，没有降级项时返回空串', () => {
    const shots = [
      packShot({ id: 's1' }, { segment_no: 1, degraded_capabilities: ['牌匾文字改「无字」，交后期合成「靠山宗」'] }),
      packShot({ id: 's2' }, { segment_no: 2, degraded_capabilities: [] }),
      packShot({ id: 's3' }, { segment_no: 3, degraded_capabilities: ['书信内容改「无字」，交后期合成「三日后，城西见」'] }),
    ]
    expect(storyboardPackDegradedCapabilitiesExportText(shots)).toBe(
      '第 1 段：牌匾文字改「无字」，交后期合成「靠山宗」\n第 3 段：书信内容改「无字」，交后期合成「三日后，城西见」',
    )
    expect(storyboardPackDegradedCapabilitiesExportText([shot(), packShot({ id: 's4' }, { degraded_capabilities: [] })])).toBe('')
  })
})
