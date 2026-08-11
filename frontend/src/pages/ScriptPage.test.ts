import { describe, expect, it } from 'vitest'

import { screenplayResumeActionLabel } from './ScriptPage'

describe('screenplayResumeActionLabel', () => {
  it('uses the backend baseline rebuild label', () => {
    expect(screenplayResumeActionLabel({
      operation: 'baseline_rebuild',
      mode: 'baseline_rebuild',
      mode_label: '按新合同重建剧本',
      phase: 'BLUEPRINT_GENERATION',
      baseline_done: false,
      first_evaluation_done: false,
      task_active: false,
      can_resume_baseline: true,
      can_resume_repair: false,
    })).toBe('按新合同重建剧本')
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
