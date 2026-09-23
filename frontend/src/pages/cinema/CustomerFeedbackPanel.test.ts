import { describe, expect, it } from 'vitest'
import { feedbackSubmitDisabledReason, formatFeedbackTime } from './CustomerFeedbackPanel'

describe('客户反馈提交禁用原因', () => {
  it('忙碌或未填写内容时给出明确原因，就绪时返回空字符串', () => {
    expect(feedbackSubmitDisabledReason(true, '内容')).toBe('正在提交上一条反馈')
    expect(feedbackSubmitDisabledReason(false, '')).toBe('请先填写反馈内容')
    expect(feedbackSubmitDisabledReason(false, '   ')).toBe('请先填写反馈内容')
    expect(feedbackSubmitDisabledReason(false, '节奏可以更紧')).toBe('')
  })
})

describe('反馈时间展示', () => {
  it('兼容秒和毫秒时间戳', () => {
    expect(formatFeedbackTime(1_700_000_000)).toBe(formatFeedbackTime(1_700_000_000_000))
  })
})
