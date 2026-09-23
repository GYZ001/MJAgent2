import { describe, expect, it } from 'vitest'
import { feedbackSubmitDisabledReason, formatFeedbackTime } from './CustomerFeedbackPanel'

describe('客户反馈提交禁用原因', () => {
  it('忙碌或未填写内容时给出明确原因，就绪时返回空字符串', () => {
    expect(feedbackSubmitDisabledReason(true, '内容', true)).toBe('正在提交上一条反馈')
    expect(feedbackSubmitDisabledReason(false, '', true)).toBe('请先填写反馈内容')
    expect(feedbackSubmitDisabledReason(false, '   ', true)).toBe('请先填写反馈内容')
    expect(feedbackSubmitDisabledReason(false, '节奏可以更紧', true)).toBe('')
  })

  it('本集尚无交付候选时禁用提交，并给出可执行的出路提示', () => {
    expect(feedbackSubmitDisabledReason(false, '节奏可以更紧', false))
      .toBe('先生成交付候选后才能记录反馈')
    // 没有交付候选比"内容未填写"更优先：候选缺失是结构性前提，先解决它。
    expect(feedbackSubmitDisabledReason(false, '', false))
      .toBe('先生成交付候选后才能记录反馈')
  })
})

describe('反馈时间展示', () => {
  it('兼容秒和毫秒时间戳', () => {
    expect(formatFeedbackTime(1_700_000_000)).toBe(formatFeedbackTime(1_700_000_000_000))
  })
})
