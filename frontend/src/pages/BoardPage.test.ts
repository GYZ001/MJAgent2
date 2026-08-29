import { describe, expect, it } from 'vitest'
import type { Shot, StoryboardPackSegment, StoryboardStatus } from '../api'
import {
  isStoryboardProblemShot,
  storyboardGateIssueLabel,
  storyboardEmptyCopy,
  storyboardHeadlineLabel,
  storyboardPackBeatOverview,
  storyboardPackDegradedCapabilitiesExportText,
  storyboardPackResourceGapSummary,
  storyboardPackTargetModelLabel,
  storyboardProgressCopy,
  storyboardPrimaryAction,
  storyboardShotCheckpointLabel,
  storyboardStartPreviewCopy,
  storyboardToolbarActions,
  providerRecoveryActionLabel,
  type ProviderTaskBlocker,
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
    expect(copy.detail).toContain('全部段落通过校验前')
  })

  it('分镜台 2.0.3：整集调用尚无任何段落产出时不假装存在逐段续跑进度', () => {
    // 后端一次调用产出全部段落，落库单事务、要么整份写完要么什么都不写；
    // working===0 时不能再说"从第 N 段继续""逐段校验"，那是不存在的能力。
    const zeroWorking = {
      produced_shots: 0, validated_shots: 0, draft_shots: 0,
      safe_checkpoint_shots: 0, pending_revalidation_shots: 0, resume_from_shot: 1,
    }
    const running = storyboardProgressCopy(storyboardStatus({ state: 'running', ...zeroWorking }))
    expect(running.detail).not.toContain('从第')
    expect(running.detail).not.toContain('逐段校验')
    expect(running.detail).toContain('整集一次调用联合产出')

    const failed = storyboardProgressCopy(storyboardStatus({ state: 'failed', ...zeroWorking }))
    expect(failed.detail).not.toContain('从第')
    expect(failed.detail).not.toContain('逐段校验')
    expect(failed.detail).toContain('整集一次调用联合产出')
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

  it('产物齐全但发布证据仍待更新时不再单独提示"发布证据"仪式，落回通用继续文案', () => {
    // 2026-08-26 用户拍板：分镜提示词全部生成完就直接可进生成台，分镜台不
    // 再有一个独立的"完成发布证据"人工确认步骤。resume_mode==='finalize_
    // evidence' 落回和其余"已全部校验"状态相同的通用文案，不再单独提冷
    // 观众审读/校准校验/发布证据签发——那套流程此前对多数分集本来就不
    // 存在，继续单独提示会让界面在说一件已经不成立的事。
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

    const progress = storyboardProgressCopy(status)
    expect(progress.detail).toContain('即可进入生成台')
    expect(progress.detail).not.toContain('发布证据')
    expect(progress.detail).not.toContain('人工确认')

    const preview = storyboardStartPreviewCopy({
      preview_token: 'preview-2',
      action: 'resume',
      resume_mode: 'finalize_evidence',
      kept_validated_shots: 14,
      planned_shots: 14,
      remaining_shots: 0,
      checkpoint: { available: true, phase: 'WAITING_HUMAN', resume_from_shot: 15 },
    })
    expect(preview.title).toBe('继续生成视频提示词')
    expect(preview.confirmLabel).toBe('继续任务')
    expect(preview.detail).not.toContain('发布证据')
  })

  it('真实门禁失败时仍要求继续修复，不能进入确认', () => {
    const action = storyboardPrimaryAction(
      storyboardStatus({
        state: 'failed',
        resume_mode: 'repair_existing',
        hard_gates_passed: false,
        hard_gate_issue_count: 1,
      }),
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

  it('顶部状态条把后端"镜已通过"文案收窄成"段"，与段落轨道的叫法统一，不误伤"分镜"本身', () => {
    expect(storyboardHeadlineLabel('10/10 镜已通过，待完成发布证据')).toBe('10/10 段已通过，待完成发布证据')
    expect(storyboardHeadlineLabel('9/17 镜已通过，待更新发布证据')).toBe('9/17 段已通过，待更新发布证据')
    expect(storyboardHeadlineLabel('9/17 镜已通过，等待确认')).toBe('9/17 段已通过，等待确认')
    // 分镜台 2.0.3：全部段落由一次整集模型调用联合产出，"当前处理第 N 镜"
    // 这句话对这条管线永远不是真进度（resume_from 在生成完成前恒为 1），
    // 必须整句替换，不能只做"镜"->"段"的措辞替换。
    expect(storyboardHeadlineLabel('分镜任务进行中，当前处理第 3 镜')).toBe('分镜任务进行中，整集视频提示词正在联合生成')
    expect(storyboardHeadlineLabel('局部修复已暂停，将从第 3 镜继续')).toBe('局部修复已暂停，将从第 3 段继续')
    expect(storyboardHeadlineLabel('整集修复已暂停，可继续修复现有问题镜')).toBe('整集修复已暂停，可继续修复现有问题段')
    expect(storyboardHeadlineLabel('生成停在第 5 镜，可继续处理')).toBe('生成停在第 5 段，可继续处理')
    // "分镜"本身不受影响：不含数字/问题的"镜"字样原样保留
    expect(storyboardHeadlineLabel('当前分镜已确认')).toBe('当前分镜已确认')
    expect(storyboardHeadlineLabel('分镜台处于安全只读模式，可继续审阅')).toBe('分镜台处于安全只读模式，可继续审阅')
    expect(storyboardHeadlineLabel('剧本已就绪，尚未生成分镜')).toBe('剧本已就绪，尚未生成分镜')
  })
})

// 「清空视频提示词」撞上供应商付费任务尚未终态（PROVIDER_TASKS_NOT_TERMINAL）时的
// 恢复面板：后端给出 recovery_action，前端只做人话翻译，不能编出后端没说过的含义，
// 未知取值要有可读兜底而不是空白或崩溃。
function providerBlocker(overrides: Partial<ProviderTaskBlocker> = {}): ProviderTaskBlocker {
  return {
    job_id: 'job_1', shot_id: 's1', version_id: 'ver_1', job_status: 'waiting_human',
    provider_operation_id: 'op_1', provider_task_id: 'task_1', provider_create_state: 'accepted',
    claim_status: 'accepted', amount_cny: 12, recovery_status: 'waiting_human',
    recovery_action: 'review_provider_failure',
    ...overrides,
  }
}

describe('供应商任务恢复面板文案（PROVIDER_TASKS_NOT_TERMINAL）', () => {
  it('四种已知 recovery_action 都翻译成用户能懂的下一步', () => {
    for (const action of [
      'review_provider_failure', 'continue_provider_poll',
      'restore_provider_poll', 'reconcile_provider_create',
    ] as const) {
      const label = providerRecoveryActionLabel(providerBlocker({ recovery_action: action }))
      expect(label.length).toBeGreaterThan(0)
      expect(label).toContain('供应商')
    }
  })

  it('未知 recovery_action 有可读兜底，不是空白', () => {
    const label = providerRecoveryActionLabel(providerBlocker({ recovery_action: 'something_new' }))
    expect(label).toContain('something_new')
  })
})

describe('分镜台段落展示（docs/STORYBOARD_PROMPT_IR_DESIGN.md 冻结契约）', () => {
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

  it('节拍概览按 beat_id 里的数字自然排序，不按字符串字典序（真实 EP1 数据：b10/b11.../b16 不会插到 b1 和 b2 之间）', () => {
    const naturalOrder = [
      'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8', 'b9',
      'b10', 'b11', 'b12', 'b13', 'b14', 'b15', 'b16',
    ]
    // 字符串字典序会把这份输入排成 b1,b10,b11..b16,b2,b3..b9——这正是线上复现的乱序；
    // 这里故意打乱输入的 shots 顺序、segment_no 也不按 beat 编号递增，
    // 证明排序结果只取决于 beat_id 的数字，不依赖 shots 数组或段号顺序。
    const shuffled = [...naturalOrder].sort((a, b) => a.localeCompare(b))
    const shots = shuffled.map((beatId, index) => packShot(
      { id: `s-${beatId}` },
      { segment_no: shuffled.length - index, beats: [{ beat_id: beatId, summary: `节拍 ${beatId}`, segment_indexes: [shuffled.length - index] }] },
    ))
    expect(storyboardPackBeatOverview(shots).map(entry => entry.beat_id)).toEqual(naturalOrder)
  })

  it('beat_id 没有数字时排到最后而不是抛错或打乱其余节拍', () => {
    const shots = [
      packShot({ id: 's1' }, { segment_no: 1, beats: [{ beat_id: 'intro', summary: '开场', segment_indexes: [1] }] }),
      packShot({ id: 's2' }, { segment_no: 2, beats: [{ beat_id: 'b2', summary: '密信现踪', segment_indexes: [2] }] }),
      packShot({ id: 's3' }, { segment_no: 3, beats: [{ beat_id: 'b1', summary: '获得密信', segment_indexes: [3] }] }),
    ]
    expect(() => storyboardPackBeatOverview(shots)).not.toThrow()
    expect(storyboardPackBeatOverview(shots).map(entry => entry.beat_id)).toEqual(['b1', 'b2', 'intro'])
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
