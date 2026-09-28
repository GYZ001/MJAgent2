import { describe, expect, it } from 'vitest'
import {
  adaptationAttentionCount, adaptationModeLabel, adaptationPanelTitle, causalityWarningText, dropReviewText,
  durationComparisonText, foreshadowingWarningText, hooksWarningText, overTargetText,
} from './storyboardAdaptation'

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
  it('attentionCount 为 0（默认）时不追加"需核查"，不给一切正常的分集编造信号', () => {
    expect(adaptationPanelTitle(3, 5, 0)).toBe('本集删减 · 3 处原文 / 5 句台词')
  })
  it('attentionCount > 0 时追加"需核查 N 处"，情绪因果/伏笔的信号不会被删减标题盖住', () => {
    expect(adaptationPanelTitle(3, 5, 2)).toBe('本集删减 · 3 处原文 / 5 句台词 · 需核查 2 处')
  })
})

describe('adaptationAttentionCount', () => {
  it('两者都 ok 或缺字段（老留档）时为 0', () => {
    expect(adaptationAttentionCount(undefined, undefined)).toBe(0)
    expect(adaptationAttentionCount({ status: 'ok', problem_count: 0 }, { status: 'ok', problem_count: 0 })).toBe(0)
  })
  it('warning 按后端给出的 problem_count 计数', () => {
    expect(adaptationAttentionCount({ status: 'warning', problem_count: 3 }, null)).toBe(3)
  })
  it('no_turns_nominated/no_signals_nominated 各算 1 处（模型完全没提名，代码判不出是原文没有还是漏标）', () => {
    expect(adaptationAttentionCount({ status: 'no_turns_nominated', problem_count: 0 }, { status: 'no_signals_nominated', problem_count: 0 })).toBe(2)
  })
  it('两个信号叠加求和', () => {
    expect(adaptationAttentionCount({ status: 'warning', problem_count: 2 }, { status: 'no_signals_nominated', problem_count: 0 })).toBe(3)
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

describe('dropReviewText', () => {
  it('没有 drop_review 字段（老留档）时返回空串，不渲染任何复核文案', () => {
    expect(dropReviewText(undefined)).toBe('')
    expect(dropReviewText(null)).toBe('')
  })

  it('复核调用失败时如实说明删减未经复核', () => {
    const text = dropReviewText({ status: 'failed', reviewed_count: 2, must_keep: [], second_pass: false })
    expect(text).toBe('复核调用失败，删减未经复核')
  })

  it('复核成功且救回内容时列出条数与摘录', () => {
    const text = dropReviewText({
      status: 'ok', reviewed_count: 3, second_pass: true,
      must_keep: [
        { item_id: 'span:1:3-4', kind: 'span', text: '一周后你若到了凝气一层', evidence_quote: '一周后你若到了凝气一层' },
      ],
    })
    expect(text).toBe('删减经复核：恢复了 1 处关键内容（一周后你若到了凝气一层）')
  })

  it('复核成功但没有必保项时不渲染（没有可说的内容）', () => {
    const text = dropReviewText({ status: 'ok', reviewed_count: 2, must_keep: [], second_pass: false })
    expect(text).toBe('')
  })

  it('复核跳过（无删减/忠实档）时不渲染', () => {
    const text = dropReviewText({ status: 'skipped', reviewed_count: 0, must_keep: [], second_pass: false })
    expect(text).toBe('')
  })
})

describe('hooksWarningText', () => {
  it('没有 hooks 字段（老留档）时返回空串，不渲染任何钩子文案', () => {
    expect(hooksWarningText(undefined)).toBe('')
    expect(hooksWarningText(null)).toBe('')
  })

  it('status=ok 时不渲染（钩子核验通过，没有可说的问题）', () => {
    const text = hooksWarningText({
      status: 'ok',
      opening: { beat_id: 'b1', evidence_quote: '他冲进火场', problems: [] },
      ending: { beat_id: 'b9', evidence_quote: '门缓缓合上', problems: [] },
    })
    expect(text).toBe('')
  })

  it('status=warning 时列出开篇/结尾两侧的具体问题', () => {
    const text = hooksWarningText({
      status: 'warning',
      opening: { beat_id: 'b1', evidence_quote: '他冲进火场', problems: ['引用的节拍 b1 不是 importance=key'] },
      ending: { beat_id: 'b9', evidence_quote: '门缓缓合上', problems: [] },
    })
    expect(text).toBe('开篇/结尾钩子模型多次调整后仍未通过核验：引用的节拍 b1 不是 importance=key')
  })
})

describe('causalityWarningText：原文缺诱因', () => {
  it('status=ok 但有缺诱因提名时给出剧本层提示与出路，并计入需核查处数', () => {
    const c = { status: 'ok' as const, problem_count: 0, missing_stimulus_count: 2 }
    const text = causalityWarningText(c)
    expect(text).toContain('2 处情绪转折没写出诱因')
    expect(text).toContain('补写原文后重跑本集分镜')
    expect(adaptationAttentionCount(c, null)).toBe(2)
  })
  it('warning 与缺诱因同时存在时两条都说', () => {
    const text = causalityWarningText({ status: 'warning', problem_count: 1, missing_stimulus_count: 1 })
    expect(text).toContain('仍有 1 处未通过')
    expect(text).toContain('1 处情绪转折没写出诱因')
  })
  it('老留档没有 missing_stimulus_count 时不提缺诱因', () => {
    expect(causalityWarningText({ status: 'ok', problem_count: 0 })).toBe('')
  })
})

describe('causalityWarningText', () => {
  it('没有 causality 字段（老留档）时返回空串', () => {
    expect(causalityWarningText(undefined)).toBe('')
    expect(causalityWarningText(null)).toBe('')
  })
  it('status=ok 时不渲染（核验通过，没有可说的问题）', () => {
    expect(causalityWarningText({ status: 'ok', problem_count: 0 })).toBe('')
  })
  it('status=no_turns_nominated 时提示人工核查是否被遗漏，不断言模型做错了', () => {
    const text = causalityWarningText({ status: 'no_turns_nominated', problem_count: 0 })
    expect(text).toBe('本集没有识别到任何情绪转折/决定性动作节拍——如果原文确实有，请人工核查是否被遗漏')
  })
  it('status=warning 时给出问题条数并指向对应分镜段的能力降级提示', () => {
    const text = causalityWarningText({ status: 'warning', problem_count: 2 })
    expect(text).toBe('情绪因果核验模型多次调整后仍有 2 处未通过，请人工核查（具体见对应分镜段的能力降级提示）')
  })
})

describe('foreshadowingWarningText', () => {
  it('没有 foreshadowing 字段（老留档）时返回空串', () => {
    expect(foreshadowingWarningText(undefined)).toBe('')
    expect(foreshadowingWarningText(null)).toBe('')
  })
  it('status=ok 时不渲染', () => {
    expect(foreshadowingWarningText({ status: 'ok', problem_count: 0 })).toBe('')
  })
  it('status=no_signals_nominated 时提示人工核查是否被遗漏', () => {
    const text = foreshadowingWarningText({ status: 'no_signals_nominated', problem_count: 0 })
    expect(text).toBe('本集没有识别到任何伏笔/类型信号节拍——如果原文确实有，请人工核查是否被遗漏')
  })
  it('status=warning 时给出问题条数并指向对应分镜段的能力降级提示', () => {
    const text = foreshadowingWarningText({ status: 'warning', problem_count: 1 })
    expect(text).toBe('伏笔核验模型多次调整后仍有 1 处未通过，请人工核查（具体见对应分镜段的能力降级提示）')
  })
})
