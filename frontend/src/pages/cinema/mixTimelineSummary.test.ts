import { describe, expect, it } from 'vitest'
import { autoAdoptedSummary, finalSkipSummary } from './mixTimelineSummary'

describe('autoAdoptedSummary', () => {
  it.each([
    ['报告为 null', null, null],
    ['报告为 undefined', undefined, null],
    ['报告不是对象', 'not-an-object', null],
    ['没有 timeline 键（旧报告）', { ok: true }, null],
    ['timeline 不是对象', { timeline: 'nope' }, null],
    ['没有自动采纳的镜头', { timeline: { auto_adopted_shot_nos: [] } }, null],
    ['auto_adopted_shot_nos 缺失（旧报告）', { timeline: {} }, null],
    ['auto_adopted_shot_nos 类型不对（畸形）', { timeline: { auto_adopted_shot_nos: 'nope' } }, null],
  ])('%s', (_label, report, expected) => {
    expect(autoAdoptedSummary(report)).toBe(expected)
  })

  it('列出自动采纳的镜号并按升序排列，提示未经人工复核', () => {
    const summary = autoAdoptedSummary({ timeline: { auto_adopted_shot_nos: [5, 3] } })
    expect(summary).toContain('第 3、5 镜')
    expect(summary).toContain('系统自动采纳')
    expect(summary).toContain('未经人工复核')
  })

  it('数组里混入非数字元素时只保留合法镜号，不整体拒绝渲染', () => {
    const summary = autoAdoptedSummary({
      timeline: { auto_adopted_shot_nos: [7, 'bad', 2] },
    })
    expect(summary).toContain('第 2、7 镜')
  })
})

describe('finalSkipSummary（从 CinemaPage.tsx 搬出，行为不变）', () => {
  it('没有跳过任何镜头时返回 null', () => {
    expect(finalSkipSummary({ ok: true, timeline: { partial: false, skipped_shot_nos: [] } }))
      .toBeNull()
  })

  it('列出跳过的镜号与具体原因', () => {
    const summary = finalSkipSummary({
      ok: false,
      timeline: {
        partial: true,
        skipped_shot_nos: [3, 5],
        skip_reasons: { '3': '镜 3 缺少已采纳的有效视频权威', '5': '尚无已采纳且落盘可播放的真实视频' },
      },
    })
    expect(summary).toContain('第 3 镜（镜 3 缺少已采纳的有效视频权威）')
    expect(summary).toContain('第 5 镜（尚无已采纳且落盘可播放的真实视频）')
  })
})
