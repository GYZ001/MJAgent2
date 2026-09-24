import { describe, expect, it } from 'vitest'
import { adaptationModeLabel, adaptationPanelTitle, durationComparisonText } from './storyboardAdaptation'

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
