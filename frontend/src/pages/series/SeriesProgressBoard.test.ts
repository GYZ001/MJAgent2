import { describe, expect, it } from 'vitest'
import type { EpisodeEntry } from '../../api'
import { seriesRepairTarget } from './SeriesProgressBoard'

function ep(no: number, stages: Partial<EpisodeEntry['stages']>): EpisodeEntry {
  return {
    episode_id: `e${no}`,
    episode_no: no,
    title: '',
    stages: { screenplay: 'skipped', storyboard: 'skipped', confirm: 'skipped', video: 'skipped', final: 'skipped', ...stages },
    error: null,
  }
}

describe('seriesRepairTarget', () => {
  it('单集失败被跳过后，修复入口仍指向失败的那一集与那一步，而不是当前正在跑的集', () => {
    const episodes = [ep(6, {}), ep(7, { video: 'failed', final: 'pending' }), ep(8, { video: 'running', final: 'pending' })]
    const target = seriesRepairTarget(episodes, 8, 'video')
    expect(target?.episode.episode_no).toBe(7)
    expect(target?.view).toBe('wall')
    expect(target?.stage).toBe('video')
  })

  it('多集失败时指向最早失败的集', () => {
    const episodes = [ep(3, { confirm: 'failed' }), ep(4, { video: 'failed' })]
    expect(seriesRepairTarget(episodes, null, null)?.episode.episode_no).toBe(3)
    expect(seriesRepairTarget(episodes, null, null)?.view).toBe('board')
  })

  it('没有失败格子时退回当前集/当前步；merge 或无当前步则没有跳转入口', () => {
    const episodes = [ep(1, { video: 'running' })]
    expect(seriesRepairTarget(episodes, 1, 'video')?.view).toBe('wall')
    expect(seriesRepairTarget(episodes, null, 'merge')).toBeNull()
  })
})
