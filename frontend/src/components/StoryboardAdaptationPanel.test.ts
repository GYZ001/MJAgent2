import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type StoryboardAdaptationSummary } from '../api'
import StoryboardAdaptationPanel from './StoryboardAdaptationPanel'

vi.mock('../api', () => ({ api: { getStoryboardAdaptation: vi.fn() } }))
afterEach(() => vi.resetAllMocks())

async function mount(episodeId = 'ep-1') {
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => {
    view = TestRenderer.create(React.createElement(StoryboardAdaptationPanel, { episodeId }))
    await Promise.resolve()
  })
  return view
}

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === 'string' ? c : textOf(c))).join('')
}

const WITH_DROPS: StoryboardAdaptationSummary = {
  recorded: true, adaptation_mode: 'short_drama', target_duration_s: 90, segment_count: 6, over_target: false,
  dropped_source_spans: [
    { source_segment_index: 1, from_unit: 1, to_unit: 2, reason: '次要支线，压缩篇幅', chapter_idx: 3, start_offset: 10, end_offset: 40, excerpt: '……天色渐暗，孟浩独自走在回家的路上……', chars: 30 },
  ],
  dropped_lines: [
    { quote_id: 'q1', reason: '与主线无关的寒暄', text: '今天天气不错啊。' },
    { quote_id: 'q2', reason: '与主线无关的寒暄', text: '是啊，挺好的。' },
  ],
}

describe('StoryboardAdaptationPanel：有删减', () => {
  it('标题带计数，展开后显示档位、时长对比、删减原文与弃置台词', async () => {
    vi.mocked(api.getStoryboardAdaptation).mockResolvedValue(WITH_DROPS)
    const view = await mount()
    const summary = view.root.findByType('summary')
    expect(textOf(summary)).toBe('本集删减 · 1 处原文 / 2 句台词')
    const text = textOf(view.root)
    expect(text).toContain('档位：短剧节奏')
    expect(text).toContain('6 段 · 约 90 秒（目标 90 秒）')
    expect(text).toContain('天色渐暗，孟浩独自走在回家的路上')
    expect(text).toContain('次要支线，压缩篇幅')
    expect(text).toContain('今天天气不错啊。')
    expect(text).not.toContain('本集没有删减内容')
    view.unmount()
  })
})

describe('StoryboardAdaptationPanel：无删减', () => {
  it('没有任何删减时显示「本集没有删减内容」', async () => {
    vi.mocked(api.getStoryboardAdaptation).mockResolvedValue({
      recorded: true, adaptation_mode: 'faithful', target_duration_s: null, segment_count: null,
      over_target: false, dropped_source_spans: [], dropped_lines: [],
    })
    const view = await mount()
    expect(textOf(view.root.findByType('summary'))).toBe('本集删减 · 0 处原文 / 0 句台词')
    expect(textOf(view.root)).toContain('本集没有删减内容')
    view.unmount()
  })
})

describe('StoryboardAdaptationPanel：老分集 recorded=false', () => {
  it('如实说明生成时未记录改编档位，不假装是忠实原著', async () => {
    vi.mocked(api.getStoryboardAdaptation).mockResolvedValue({
      recorded: false, adaptation_mode: 'faithful', target_duration_s: null, segment_count: null,
      over_target: false, dropped_source_spans: [], dropped_lines: [],
    })
    const view = await mount()
    expect(textOf(view.root)).toContain('档位：旧分镜，生成时未记录改编档位')
    view.unmount()
  })

  it('recorded=false 但对白台账仍有弃置台词时照常展示（台账独立于改编留档）', async () => {
    vi.mocked(api.getStoryboardAdaptation).mockResolvedValue({
      recorded: false, adaptation_mode: 'faithful', target_duration_s: null, segment_count: null,
      over_target: false, dropped_source_spans: [], dropped_lines: [{ quote_id: 'q1', reason: '重复台词', text: '走吧。' }],
    })
    const view = await mount()
    expect(textOf(view.root.findByType('summary'))).toBe('本集删减 · 0 处原文 / 1 句台词')
    expect(textOf(view.root)).toContain('走吧。')
    view.unmount()
  })
})

describe('StoryboardAdaptationPanel：over_target', () => {
  it('over_target 为真时提示模型多次调整后仍超出短剧上限', async () => {
    vi.mocked(api.getStoryboardAdaptation).mockResolvedValue({
      recorded: true, adaptation_mode: 'short_drama', target_duration_s: 90, segment_count: 9,
      over_target: true, dropped_source_spans: [], dropped_lines: [],
    })
    const view = await mount()
    expect(textOf(view.root)).toContain('模型多次调整后仍超出短剧上限')
    view.unmount()
  })
})

describe('StoryboardAdaptationPanel：加载失败', () => {
  it('接口失败时展示错误而不是空白折叠面板', async () => {
    vi.mocked(api.getStoryboardAdaptation).mockRejectedValue(new Error('网络错误'))
    const view = await mount()
    expect(textOf(view.root)).toContain('本集删减信息加载失败：网络错误')
    view.unmount()
  })
})
