import { describe, expect, it } from 'vitest'
import { episodePartialFilmText, partialFilmDetail, seriesTaskPartialFilmSummary } from './seriesPartialFilm'

const STALE_NOTICE = '成片信息可能已过期（合成后又发生变化），请重新合成后确认是否仍缺段'

describe('partialFilmDetail / episodePartialFilmText', () => {
  it('带原因时如实报出段号与原因，不杜撰', () => {
    const ep = { skipped_shot_nos: [3, 7], skip_reasons: { '3': '供应商拒收', '7': '供应商拒收' }, final_video_stale: false }
    expect(partialFilmDetail(ep)).toBe('缺第 3、7 段：供应商拒收')
    expect(episodePartialFilmText(ep)).toBe('成片已合成（缺第 3、7 段：供应商拒收）')
  })

  it('原因不同时逐一列出，且去重', () => {
    const ep = { skipped_shot_nos: [2, 5], skip_reasons: { '2': '供应商拒收', '5': '供应商拒收' } }
    expect(partialFilmDetail(ep)).toBe('缺第 2、5 段：供应商拒收')
  })

  it('没有具体原因时只报段号，不编造理由', () => {
    const ep = { skipped_shot_nos: [4], skip_reasons: {}, final_video_stale: false }
    expect(partialFilmDetail(ep)).toBe('缺第 4 段')
    expect(episodePartialFilmText(ep)).toBe('成片已合成（缺第 4 段）')
  })

  it('全齐（skipped_shot_nos 为空）返回空串/null，不展示缺段提示', () => {
    const ep = { skipped_shot_nos: [] as number[], skip_reasons: {}, final_video_stale: false }
    expect(partialFilmDetail(ep)).toBe('')
    expect(episodePartialFilmText(ep)).toBeNull()
  })

  it('final_video_stale 为真时改报「可能已过期」，不复述可能不准的旧缺段清单', () => {
    const ep = { skipped_shot_nos: [3, 7], skip_reasons: { '3': '供应商拒收', '7': '供应商拒收' }, final_video_stale: true }
    expect(episodePartialFilmText(ep)).toBe(STALE_NOTICE)
  })
})

describe('seriesTaskPartialFilmSummary', () => {
  it('跨集汇总并给出路（提示去查看详情重拍/采纳）', () => {
    const summary = seriesTaskPartialFilmSummary([
      { episode_no: 2, final_is_partial: true, skipped_shot_nos: [3, 7], skip_reasons: { '3': '供应商拒收', '7': '供应商拒收' }, final_video_stale: false },
      { episode_no: 5, final_is_partial: true, skipped_shot_nos: [1], skip_reasons: {}, final_video_stale: false },
    ])
    expect(summary).toBe('第 2 集缺第 3、7 段：供应商拒收；第 5 集缺第 1 段，点击"查看"到对应集的成片台重拍或采纳')
  })

  it('某一集 final_video_stale 为真时该集改报过期提示，其余集不受影响', () => {
    const summary = seriesTaskPartialFilmSummary([
      { episode_no: 2, final_is_partial: true, skipped_shot_nos: [3, 7], skip_reasons: { '3': '供应商拒收', '7': '供应商拒收' }, final_video_stale: true },
      { episode_no: 5, final_is_partial: true, skipped_shot_nos: [1], skip_reasons: {}, final_video_stale: false },
    ])
    expect(summary).toBe(`第 2 集${STALE_NOTICE}；第 5 集缺第 1 段，点击"查看"到对应集的成片台重拍或采纳`)
  })

  it('空数组（全齐）返回 null', () => {
    expect(seriesTaskPartialFilmSummary([])).toBeNull()
  })
})
