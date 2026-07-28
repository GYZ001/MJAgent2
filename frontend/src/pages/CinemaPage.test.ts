import { describe, expect, it } from 'vitest'
import {
  canConcatenateMix,
  deliveryCheckLabel,
  deliveryReviewDisabledReason,
  deliveryStatusLabel,
  deliveryWarningLabel,
  formatDeliveryTime,
  nextCinemaTab,
} from './CinemaPage'

describe('成片可合成条件', () => {
  it('只需一个已采纳可用片段，不要求全部分镜完成', () => {
    expect(canConcatenateMix({ shots_ready: 1 })).toBe(true)
    expect(canConcatenateMix({ shots_ready: 0 })).toBe(false)
    expect(canConcatenateMix(null)).toBe(false)
  })
})

describe('成片台交付文案', () => {
  it('隐藏内部交付状态并翻译常见风险', () => {
    expect(deliveryStatusLabel('approved')).toBe('已批准')
    expect(deliveryStatusLabel('unexpected_internal_status')).toBe('处理中')
    expect(deliveryWarningLabel('Duplicate frames (frame 1 and frame 2)'))
      .toBe('存在重复画面帧')
    expect(deliveryWarningLabel('Missing start state of Xiao Yan'))
      .toBe('未呈现预期起始状态：萧炎')
    expect(deliveryWarningLabel('The expected core action is not fully completed'))
      .toBe('预期核心动作未完整完成')
    expect(deliveryWarningLabel('Unknown internal warning from detector'))
      .not.toMatch(/[A-Za-z]{3}/)
    expect(deliveryCheckLabel('每镜都有已采用且通过技术校验的视频'))
      .toBe('每镜都有已采用且可正常播放的视频')
  })
})

describe('成片台页签与记录时间', () => {
  it('支持方向键循环以及 Home 和 End', () => {
    expect(nextCinemaTab('preview', 'ArrowLeft')).toBe('records')
    expect(nextCinemaTab('preview', 'ArrowRight')).toBe('readiness')
    expect(nextCinemaTab('readiness', 'End')).toBe('records')
    expect(nextCinemaTab('records', 'Home')).toBe('preview')
    expect(nextCinemaTab('records', 'Enter')).toBeNull()
  })

  it('兼容秒和毫秒时间戳', () => {
    expect(formatDeliveryTime(1_700_000_000))
      .toBe(formatDeliveryTime(1_700_000_000_000))
  })
})

describe('成片台审核按钮禁用原因', () => {
  it('按当前恢复路径返回首个缺失条件', () => {
    expect(deliveryReviewDisabledReason('approve', true, null, '', '', ''))
      .toBe('正在处理上一项交付操作')
    expect(deliveryReviewDisabledReason('approve', false, null, '', '', ''))
      .toBe('请先生成并选择一个待复验交付候选')
    expect(deliveryReviewDisabledReason('approve', false, 'approved', '', '', ''))
      .toBe('当前候选已批准，不能重复审核')
    expect(deliveryReviewDisabledReason('approve', false, 'waiting_human', '', '', ''))
      .toBe('请先填写复验人')
    expect(deliveryReviewDisabledReason('approve', false, 'waiting_human', '审核人', '', ''))
      .toBe('请先填写审核意见')
  })

  it('只有带风险批准额外要求接受风险说明', () => {
    expect(deliveryReviewDisabledReason(
      'approve_with_risk',
      false,
      'waiting_human',
      '审核人',
      '画面和声音已复验',
      '',
    )).toBe('请先填写接受风险说明')
    expect(deliveryReviewDisabledReason(
      'approve',
      false,
      'waiting_human',
      '审核人',
      '画面和声音已复验',
      '',
    )).toBe('')
  })
})
