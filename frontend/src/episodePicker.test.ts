import { describe, expect, it } from 'vitest'
import { filterEpisodeOptions, resolveEpisodeId, type EpisodeOption } from './episodePicker'

const episodes: EpisodeOption[] = [
  { id: 'e1', episode_no: 1, title: '开端' },
  { id: 'e2', episode_no: 2, title: '风波' },
  { id: 'e3', episode_no: 3, title: '终局' },
]

describe('episode picker', () => {
  it('keeps a valid selection and recovers a stale or empty selection', () => {
    expect(resolveEpisodeId(episodes, 'e2')).toBe('e2')
    expect(resolveEpisodeId(episodes, 'missing')).toBe('e1')
    expect(resolveEpisodeId(episodes, null)).toBe('e1')
    expect(resolveEpisodeId([], 'e2')).toBeNull()
  })

  it('searches episode numbers and titles before applying the result limit', () => {
    expect(filterEpisodeOptions(episodes, '2')).toEqual([episodes[1]])
    expect(filterEpisodeOptions(episodes, '终局')).toEqual([episodes[2]])
    expect(filterEpisodeOptions(episodes, '', 2)).toEqual(episodes.slice(0, 2))
  })
})
