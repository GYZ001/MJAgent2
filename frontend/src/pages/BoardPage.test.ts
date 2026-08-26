import { describe, expect, it } from 'vitest'
import type { Shot, StoryboardPackSegment, StoryboardStatus } from '../api'
import {
  buildStoryboardChanges,
  isStoryboardPackSegmentShot,
  isStoryboardProblemShot,
  storyboardGateIssueLabel,
  storyboardDeleteUsesFullClear,
  storyboardEmptyCopy,
  storyboardCharacterFilterOptions,
  storyboardPackBeatOverview,
  storyboardPackDegradedCapabilitiesExportText,
  storyboardPackResourceGapSummary,
  storyboardPackTargetModelLabel,
  storyboardProgressCopy,
  storyboardInputStrategy,
  storyboardPrimaryAction,
  storyboardSaveDisabledReason,
  storyboardShotCheckpointLabel,
  storyboardShotOverCapacity,
  storyboardSpokenChars,
  shotSpokenLimit,
  storyboardStartPreviewCopy,
  storyboardToolbarActions,
} from './BoardPage'

function shot(overrides: Partial<Shot> = {}): Shot {
  return {
    id: 's1', episode_id: 'e1', shot_no: 1, duration_s: 5, shot_size: '中景', camera_move: '固定',
    scene_time: '白天', scene_name: '房间', scene_setting: '白天，房间',
    characters: ['少年'], action_desc: '少年推门拿起信件',
    first_frame_desc: '少年站在门外', last_frame_desc: '少年拿着信件', source_excerpt: '少年推开房门。',
    narration: '', dialogues: [{ speaker: '少年', line: '这封信是谁写的？', emotion: '疑惑' }], transition: '硬切',
    continuity_from_prev: 0, adopted_version_id: null, est_cost_cny: 0, versions: [], video_stale: false,
    spoken_limit: 12, audio_timeline: [{ start_s: 0, end_s: 2, type: 'dialogue', text: '旧台词' }],
    ...overrides,
  }
}

function packSegment(overrides: Partial<StoryboardPackSegment> = {}): StoryboardPackSegment {
  return {
    segment_no: 1, duration_s: 15, synopsis: '少年拿到密信', source_segment_indexes: [1, 2],
    prompt_text: '电影级预告片质感，多镜头叙事，镜头之间硬切。镜头1……',
    shot_count: 3, dialogue: [], resources: { characters: [], scenes: [], props: [] },
    degraded_capabilities: [], beat_ids: ['B01'], target_model: 'seedance_2',
    storyboard_version: '2.0.0',
    ...overrides,
  }
}

/** 分镜台 2.0.0 段落行：经典逐镜字段留空，storyboard_pack_segment 才是权威内容。 */
function packShot(overrides: Partial<Shot> = {}, segmentOverrides: Partial<StoryboardPackSegment> = {}): Shot {
  return shot({
    shot_size: '', camera_move: '', duration_s: 15,
    dialogues: [], spoken_limit: undefined,
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

describe('分镜台结构化 diff 与问题筛选', () => {
  it('零镜头时按真实任务终态解释空白区', () => {
    expect(storyboardEmptyCopy(storyboardStatus({ state: 'running' }))).toContain('正在生成')
    expect(storyboardEmptyCopy(storyboardStatus({ state: 'failed' }))).toContain('任务未完成')
  })

  it('no-op 不产生任何保存字段', () => {
    const baseline = shot()
    expect(buildStoryboardChanges(baseline, structuredClone(baseline), null)).toEqual({})
  })

  it('普通台词修改只提交 dialogues，不携带旧时间轴', () => {
    const baseline = shot()
    const edited = structuredClone(baseline)
    edited.dialogues[0].line = '我必须马上查清真相。'
    const diff = buildStoryboardChanges(baseline, edited, null)
    expect(diff.dialogues).toEqual(edited.dialogues)
    expect(diff).not.toHaveProperty('audio_timeline')
  })

  it('时间与场景图标签独立提交，不再编辑兼容 scene_setting', () => {
    const baseline = shot()
    const edited = structuredClone(baseline)
    edited.scene_time = '18:30'
    edited.scene_name = '房间内堂'
    const diff = buildStoryboardChanges(baseline, edited, null)

    expect(diff).toEqual({ scene_time: '18:30', scene_name: '房间内堂' })
    expect(diff).not.toHaveProperty('scene_setting')
  })

  it('原文绑定作为结构化来源提交', () => {
    const baseline = shot()
    const binding = { chapter_id: 1, source_version_hash: 'hash', start_offset: 2, end_offset: 8 }
    expect(buildStoryboardChanges(baseline, structuredClone(baseline), binding)).toEqual({ source_binding: binding })
  })

  it('口播按去标点纯文字计数并进入问题筛选', () => {
    const value = shot({ spoken_limit: 5 })
    expect(storyboardSpokenChars(value)).toBe(7)
    expect(isStoryboardProblemShot(value)).toBe(true)
  })

  it('口播上限优先采信后端 spoken_limit，缺失时按后端公式 clamp(dur,5,15)*18//5 兜底', () => {
    // 后端下发时直接采用，列表与编辑器同一口径。
    expect(shotSpokenLimit(shot({ spoken_limit: 12 }))).toBe(12)
    // 缺失时按 config.max_spoken_chars_for_duration 兜底：5s→18、8s→28、10s→36、15s→54，并对时长做 5–15 clamp。
    expect(shotSpokenLimit(shot({ spoken_limit: undefined, duration_s: 5 }))).toBe(18)
    expect(shotSpokenLimit(shot({ spoken_limit: undefined, duration_s: 8 }))).toBe(28)
    expect(shotSpokenLimit(shot({ spoken_limit: undefined, duration_s: 10 }))).toBe(36)
    expect(shotSpokenLimit(shot({ spoken_limit: undefined, duration_s: 15 }))).toBe(54)
    expect(shotSpokenLimit(shot({ spoken_limit: undefined, duration_s: 3 }))).toBe(18)
    expect(shotSpokenLimit(shot({ spoken_limit: undefined, duration_s: 99 }))).toBe(54)
  })

  it('质量优化建议不混入必须处理的问题镜', () => {
    const value = shot({ qa_warnings: ['画面动作含可精简的细节词'] })
    expect(isStoryboardProblemShot(value)).toBe(false)
  })

  it('角色筛选不暴露叙事内部身份 ID', () => {
    const value = shot({
      characters: ['孟浩'],
      audio_cast: [
        'character-menghao',
        'voice-narrator',
        'passerby-c',
        'green-robed-cultivator',
      ],
    })
    expect(storyboardCharacterFilterOptions([value])).toEqual(['孟浩'])
  })

  it('删除全剧最后一镜时切换到整集清空流程', () => {
    const only = shot()
    expect(storyboardDeleteUsesFullClear([only], only.id)).toBe(true)
    expect(storyboardDeleteUsesFullClear([only, shot({ id: 's2', shot_no: 2 })], only.id)).toBe(false)
    expect(storyboardDeleteUsesFullClear([only], 'other-shot')).toBe(false)
  })

  it('运行中只提供暂停，所有有分镜的停止态都允许清空', () => {
    expect(storyboardToolbarActions('running')).toEqual({ pause: true, clear: false })
    expect(storyboardToolbarActions('paused')).toEqual({ pause: false, clear: true })
    expect(storyboardToolbarActions('failed')).toEqual({ pause: false, clear: true })
    expect(storyboardToolbarActions('ready_to_confirm')).toEqual({ pause: false, clear: true })
    expect(storyboardToolbarActions('confirmed')).toEqual({ pause: false, clear: true })
  })

  it('把门禁字段翻译为可执行的制作语言', () => {
    expect(storyboardGateIssueLabel('shots[1](shot_no=2).first_frame_desc 太短'))
      .toBe('第 2 镜：生成起点 太短')
    expect(storyboardGateIssueLabel('shot_no=3.primary_action 缺失'))
      .toBe('第 3 镜：镜头动作 缺失')
    expect(storyboardGateIssueLabel('QA 门禁未通过')).toBe('质检 必检项未通过')
  })

  it('按场景边界展示分镜输入策略，参考视频逻辑保持不变', () => {
    const previous = shot({ last_frame_desc: '上一镜真实结束状态' })
    expect(storyboardInputStrategy(previous).kind).toBe('scene_library')
    expect(storyboardInputStrategy(shot({ id: 's2', shot_no: 2 }), previous)).toMatchObject({
      kind: 'previous_video_tail',
      label: '上一视频真实尾帧 → 本镜唯一首帧',
    })
    expect(storyboardInputStrategy(shot({
      id: 's2', shot_no: 2, scene_time: '夜晚',
    }), previous).kind).toBe('scene_library')
    expect(storyboardInputStrategy(shot({
      mode_plan: { mode: 'VIDEO_INPUT_MODE' } as Shot['mode_plan'],
    }), previous).kind).toBe('reference_video')
  })

  it('保存预览禁用时给出具体恢复路径', () => {
    expect(storyboardSaveDisabledReason(false, false, 1)).toBe('尚未修改任何内容')
    expect(storyboardSaveDisabledReason(true, true, 1)).toContain('删减台词')
    expect(storyboardSaveDisabledReason(true, false, 0)).toContain('选择一个画面角色')
    expect(storyboardSaveDisabledReason(true, false, 1)).toBe('')
  })

  it('明确区分目标、工作副本和已校验镜头', () => {
    const copy = storyboardProgressCopy(storyboardStatus({ final_shot_valid: true }))
    expect(copy.summary).toBe('目标 17 镜 · 工作副本 9 镜 · 已校验 2 镜')
    expect(copy.detail).toContain('第 3–9 镜仍待校验')
    expect(copy.detail).toContain('从第 3 镜继续修复')
    expect(copy.detail).toContain('人工确认前')
  })

  it('完整收束但门禁失败时明确重开修复而不是追加镜头', () => {
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
    expect(progress.detail).toContain('不是从第 15 镜续写')

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
    expect(preview.title).toBe('继续修复分镜')
    expect(preview.confirmLabel).toBe('开始修复')
    expect(preview.summary).toContain('现有 14 镜保持不变')
    expect(preview.detail).toContain('不是从第 15 镜续写')
  })

  it('完整镜头只缺发布证据时明确继续审读发布且不改写镜头', () => {
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

    expect(storyboardProgressCopy(status).detail).toContain('不会改写现有镜头')

    const preview = storyboardStartPreviewCopy({
      preview_token: 'preview-2',
      action: 'resume',
      resume_mode: 'finalize_evidence',
      kept_validated_shots: 14,
      planned_shots: 14,
      remaining_shots: 0,
      checkpoint: { available: true, phase: 'WAITING_HUMAN', resume_from_shot: 15 },
    })
    expect(preview.title).toBe('完成分镜发布证据')
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
      label: '继续分镜任务',
    })
  })

  it('在镜头轨道区分已校验和待校验工作副本', () => {
    expect(storyboardShotCheckpointLabel(2, storyboardStatus())?.label).toBe('已校验')
    expect(storyboardShotCheckpointLabel(3, storyboardStatus())?.label).toBe('待校验')
    expect(storyboardShotCheckpointLabel(3, storyboardStatus({ state: 'confirmed' }))).toBeNull()
  })
})

describe('分镜台 2.0.0 段落展示（docs/STORYBOARD_PROMPT_IR_DESIGN.md 冻结契约）', () => {
  it('storyboard_pack_segment 非 null 是唯一权威标记', () => {
    expect(isStoryboardPackSegmentShot(shot())).toBe(false)
    expect(isStoryboardPackSegmentShot(packShot())).toBe(true)
  })

  it('段落行的口播超限公式不适用，不管台词多长都不算超限', () => {
    const overflowing = packShot({
      dialogues: [
        { speaker: 'char-1', line: '这句台词长到足以超过经典逐镜口播公式的任何合理上限，反复重复反复重复反复重复', emotion: '平静' },
      ],
      spoken_limit: 1,
    })
    expect(storyboardShotOverCapacity(overflowing)).toBe(false)
    expect(isStoryboardProblemShot(overflowing)).toBe(false)
    // 对照：经典逐镜行同样的台词/上限组合应判定超限，确认没有把判据本身削弱了。
    const classicOverflowing = shot({
      dialogues: [
        { speaker: '少年', line: '这句台词长到足以超过经典逐镜口播公式的任何合理上限，反复重复反复重复反复重复', emotion: '平静' },
      ],
      spoken_limit: 1,
    })
    expect(storyboardShotOverCapacity(classicOverflowing)).toBe(true)
  })

  it('契约自己的模型词表与视频模型选择器的供应商 key 不是同一套', () => {
    expect(storyboardPackTargetModelLabel('seedance_2')).toBe('Seedance 2.0')
    expect(storyboardPackTargetModelLabel('minimax_h3')).toBe('MiniMax H3')
    expect(storyboardPackTargetModelLabel('unknown_model')).toBe('unknown_model')
  })

  it('节拍概览按 beat_ids 反推段落去向，按 beat_id 升序排列', () => {
    const shots = [
      packShot({ id: 's1' }, { segment_no: 1, beat_ids: ['B02'] }),
      packShot({ id: 's2' }, { segment_no: 2, beat_ids: ['B01', 'B02'] }),
      packShot({ id: 's3' }, { segment_no: 3, beat_ids: ['B01'] }),
      shot({ id: 's4' }), // 经典逐镜行没有 storyboard_pack_segment，必须被忽略而不是报错
    ]
    expect(storyboardPackBeatOverview(shots)).toEqual([
      { beat_id: 'B01', segment_nos: [2, 3] },
      { beat_id: 'B02', segment_nos: [1, 2] },
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
