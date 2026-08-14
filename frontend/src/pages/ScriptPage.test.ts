import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import {
  ScreenplayResumeButton,
  screenplayResumeActionLabel,
  screenplayResumeOutcomeSummary,
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
