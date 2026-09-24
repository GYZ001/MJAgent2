import { describe, expect, it } from 'vitest'
import { adaptationModeLabel, adaptationPanelTitle, durationComparisonText, overTargetText } from './storyboardAdaptation'

describe('adaptationModeLabel', () => {
  it('recorded=false 时如实说明是旧分镜，不冒充忠实原著', () => {
    expect(adaptationModeLabel({ recorded: false, adaptation_mode: 'faithful' })).toBe('旧分镜，生成时未记录改编档位')
  })
  it('recorded=true 时翻成中文档位名', () => {
    expect(adaptationModeLabel({ recorded: true, adaptation_mode: 'short_drama' })).toBe('短剧节奏')
    expect(adaptationModeLabel({ recorded: true, adaptation_mode: 'faithful' })).toBe('忠实原著')
  })
  it('未知档位原样透出，不静默吞掉', () => {
    expect(adaptationModeLabel({ recorded: true, adaptation_mode: 'weird_mode' })).toBe('weird_mode')
  })
})

describe('adaptationPanelTitle', () => {
  it('带删减/台词计数', () => {
    expect(adaptationPanelTitle(3, 5)).toBe('本集删减 · 3 处原文 / 5 句台词')
  })
  it('没有删减时计数为 0，仍如实显示', () => {
    expect(adaptationPanelTitle(0, 0)).toBe('本集删减 · 0 处原文 / 0 句台词')
  })
})

describe('durationComparisonText', () => {
  it('段数与目标时长都有时给出对比', () => {
    expect(durationComparisonText(6, 90)).toBe('6 段 · 约 90 秒（目标 90 秒）')
  })
  it('没有目标时长时只给约合时长', () => {
    expect(durationComparisonText(6, null)).toBe('6 段 · 约 90 秒')
  })
  it('segmentCount 为 null（老分集未记录）时返回空串，不编造 0 段', () => {
    expect(durationComparisonText(null, 90)).toBe('')
  })
})

describe('overTargetText', () => {
  it('新字段齐全时如实说明最终段数、时长与上限', () => {
    const text = overTargetText({
      segment_count: 16, final_duration_s: 240, target_duration_s: 90, max_duration_s: 120,
      kept_dialogue_chars: null, dialogue_budget_chars: null, planned_over_cap: false,
    })
    expect(text).toBe('本集最终 16 段约 240 秒，超出短剧目标约 90 秒（上限 120 秒）')
  })

  it('保留台词超预算时追加口播时长原因', () => {
    const text = overTargetText({
      segment_count: 16, final_duration_s: 240, target_duration_s: 90, max_duration_s: 120,
      kept_dialogue_chars: 900, dialogue_budget_chars: 432, planned_over_cap: false,
    })
    expect(text).toContain('本集最终 16 段约 240 秒，超出短剧目标约 90 秒（上限 120 秒）')
    expect(text).toContain('保留台词 900 字需要约 250 秒口播') // 900 / 432 * 120 = 250
  })

  it('保留台词未超预算时不追加口播原因（超目标另有别的根因）', () => {
    const text = overTargetText({
      segment_count: 16, final_duration_s: 240, target_duration_s: 90, max_duration_s: 120,
      kept_dialogue_chars: 100, dialogue_budget_chars: 432, planned_over_cap: false,
    })
    expect(text).not.toContain('口播')
  })

  it('planned_over_cap 为真时追加模型规划阶段就已超上限的说明', () => {
    const text = overTargetText({
      segment_count: 10, final_duration_s: 150, target_duration_s: 90, max_duration_s: 120,
      kept_dialogue_chars: null, dialogue_budget_chars: null, planned_over_cap: true,
    })
    expect(text).toContain('模型多次调整后规划段数仍超上限')
  })

  it('老留档缺 final_duration_s 等新字段时降级为固定文案，不编造数字', () => {
    const text = overTargetText({
      segment_count: 9, final_duration_s: undefined, target_duration_s: 90, max_duration_s: undefined,
      kept_dialogue_chars: undefined, dialogue_budget_chars: undefined, planned_over_cap: undefined,
    })
    expect(text).toBe('模型多次调整后仍超出短剧上限')
  })
})
