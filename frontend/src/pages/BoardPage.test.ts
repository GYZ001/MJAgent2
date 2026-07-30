import { describe, expect, it } from 'vitest'
import type { Shot, StoryboardStatus } from '../api'
import {
  buildStoryboardChanges,
  isStoryboardProblemShot,
  storyboardGateIssueLabel,
  storyboardProgressCopy,
  storyboardSaveDisabledReason,
  storyboardShotCheckpointLabel,
  storyboardSpokenChars,
} from './BoardPage'

function shot(overrides: Partial<Shot> = {}): Shot {
  return {
    id: 's1', episode_id: 'e1', shot_no: 1, duration_s: 5, shot_size: '中景', camera_move: '固定',
    scene_setting: '白天，房间', characters: ['少年'], action_desc: '少年推门拿起信件',
    first_frame_desc: '少年站在门外', last_frame_desc: '少年拿着信件', source_excerpt: '少年推开房门。',
    narration: '', dialogues: [{ speaker: '少年', line: '这封信是谁写的？', emotion: '疑惑' }], transition: '硬切',
    continuity_from_prev: 0, adopted_version_id: null, est_cost_cny: 0, versions: [], video_stale: false,
    spoken_limit: 12, audio_timeline: [{ start_s: 0, end_s: 2, type: 'dialogue', text: '旧台词' }],
    ...overrides,
  }
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

  it('质量优化建议不混入必须处理的问题镜', () => {
    const value = shot({ qa_warnings: ['画面动作含可精简的细节词'] })
    expect(isStoryboardProblemShot(value)).toBe(false)
  })

  it('把门禁字段翻译为可执行的制作语言', () => {
    expect(storyboardGateIssueLabel('shots[1](shot_no=2).first_frame_desc 太短'))
      .toBe('第 2 镜：首帧画面 太短')
    expect(storyboardGateIssueLabel('shot_no=3.primary_action 缺失'))
      .toBe('第 3 镜：镜头动作 缺失')
    expect(storyboardGateIssueLabel('QA 门禁未通过')).toBe('质检 必检项未通过')
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

  it('在镜头轨道区分已校验和待校验工作副本', () => {
    expect(storyboardShotCheckpointLabel(2, storyboardStatus())?.label).toBe('已校验')
    expect(storyboardShotCheckpointLabel(3, storyboardStatus())?.label).toBe('待校验')
    expect(storyboardShotCheckpointLabel(3, storyboardStatus({ state: 'confirmed' }))).toBeNull()
  })
})
