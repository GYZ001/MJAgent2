import { describe, expect, it } from 'vitest'
import {
  canConcatenateMix,
  deliveryCheckLabel,
  deliveryReviewDisabledReason,
  deliveryStatusLabel,
  deliveryWarningLabel,
  finalEditStatusLabel,
  finalSkipSummary,
  formatDeliveryTime,
  nextCinemaTab,
  reconcileMixStatus,
} from './CinemaPage'
import type { MixStatus } from '../api'

describe('成片可合成条件', () => {
  it('任意一个真实视频已完成就允许合成当前片段', () => {
    expect(canConcatenateMix({ ready: true, shots_ready: 1 })).toBe(true)
    expect(canConcatenateMix({ ready: false, shots_ready: 0 })).toBe(false)
    expect(canConcatenateMix(null)).toBe(false)
  })
})

describe('成片状态刷新', () => {
  const mix = (overrides: Partial<MixStatus> = {}): MixStatus => ({
    episode_id: 'episode-1',
    title: '第一集',
    episode_no: 1,
    shots_total: 2,
    shots_ready: 1,
    ready: true,
    final_video_url: '/media/project/episodes/1/final/episode.mp4',
    final_video_stale: false,
    shots: [],
    ...overrides,
  })

  it('轮询返回相同内容时复用旧对象，避免整页无意义重渲染', () => {
    const previous = mix()
    expect(reconcileMixStatus(previous, mix())).toBe(previous)
  })

  it('刷新中的空成品地址不会移除已经展示的合成成品', () => {
    const previous = mix({ final_edit_report: { ok: true } })
    const next = reconcileMixStatus(previous, mix({
      shots_ready: 2,
      final_video_url: null,
    }))

    expect(next.final_video_url).toBe(previous.final_video_url)
    expect(next.final_video_stale).toBe(true)
    expect(next.final_edit_report).toEqual({ ok: true })
    expect(next.shots_ready).toBe(2)
  })

  it('重新合成返回新版本地址时立即切换到覆盖后的成品', () => {
    const previous = mix({
      final_video_url: '/media/project/episodes/1/final/episode.mp4?v=old',
    })
    const next = reconcileMixStatus(previous, mix({
      final_video_url: '/media/project/episodes/1/final/episode.mp4?v=new',
    }))

    expect(next).not.toBe(previous)
    expect(next.final_video_url).toContain('v=new')
  })
})

describe('成片台交付文案', () => {
  it('隐藏内部交付状态并翻译常见风险', () => {
    expect(deliveryStatusLabel('approved')).toBe('已批准')
    expect(deliveryStatusLabel('unexpected_internal_status')).toBe('处理中')
    expect(deliveryWarningLabel('Duplicate frames (frame 1 and frame 2)'))
      .toBe('存在重复画面帧')
    expect(deliveryWarningLabel('Missing start state of Someone'))
      .toBe('画面状态或人物一致性与预期不符，请结合对应镜头人工复验')
    expect(deliveryWarningLabel('The expected core action is not fully completed'))
      .toBe('预期核心动作未完整完成')
    expect(deliveryWarningLabel('Unknown internal warning from detector'))
      .not.toMatch(/[A-Za-z]{3}/)
    expect(deliveryCheckLabel('每镜都有已采用且通过技术校验的视频'))
      .toBe('每镜都有已采用且可正常播放的视频')
  })

  it('区分快速阶段拼接和终剪失败降级', () => {
    expect(finalEditStatusLabel({ ok: true }))
      .toBe('当前成片已执行确定性文字、镜间转场与音轨衔接')
    expect(finalEditStatusLabel({
      ok: false,
      mode: 'draft_concat',
      skipped_final_edit: true,
      decision_reason: 'partial_timeline_fast_preview',
    })).toContain('快速阶段拼接')
    expect(finalEditStatusLabel({ ok: false, fallback: 'draft_concat', error: 'ffmpeg failed' }))
      .toContain('基础合成降级')
  })

  it('部分合成必须把跳过的镜号和原因明确展示给用户，不能静默少几镜', () => {
    expect(finalSkipSummary(null)).toBeNull()
    expect(finalSkipSummary(undefined)).toBeNull()
    expect(finalSkipSummary({ ok: true, timeline: { partial: false, skipped_shot_nos: [] } }))
      .toBeNull()
    const summary = finalSkipSummary({
      ok: false,
      timeline: {
        partial: true,
        skipped_shot_nos: [3, 5],
        skip_reasons: { '3': '镜 3 缺少已采纳的有效视频权威', '5': '尚无已采纳且落盘可播放的真实视频' },
      },
    })
    expect(summary).toContain('第 3 镜')
    expect(summary).toContain('镜 3 缺少已采纳的有效视频权威')
    expect(summary).toContain('第 5 镜')
    expect(summary).toContain('尚无已采纳且落盘可播放的真实视频')
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
