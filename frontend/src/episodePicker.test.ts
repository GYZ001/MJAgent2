import { describe, expect, it } from 'vitest'
import {
  episodeProductionStatus,
  pickerWindowParams,
  resolveWindowedEpisodeId,
  resolveEpisodeId,
  type EpisodeOption,
} from './episodePicker'

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

  it('presents failed screenplay work as needing attention', () => {
    expect(episodeProductionStatus({ screenplay_status: 'failed', status: 'pending' })).toBe('需处理')
    expect(episodeProductionStatus({ screenplay_status: 'ready', status: 'script_failed' })).toBe('需处理')
    expect(episodeProductionStatus({ screenplay_status: 'running', status: 'pending' })).toBe('剧本中')
    expect(episodeProductionStatus({ screenplay_status: 'ready', status: 'scripted' })).toBe('待确认')
  })
})

describe('窗口化分集解析', () => {
  it('地址栏显式目标优先于服务端判定', () => {
    expect(resolveWindowedEpisodeId({ episode_current: { id: 'e5' } }, 'e5', 'e9')).toBe('e9')
  })

  it('服务端确认当前集仍有效时保持不动', () => {
    expect(resolveWindowedEpisodeId({ episode_current: { id: 'e5' } }, 'e5')).toBe('e5')
  })

  it('当前集已失效时退回服务端给出的光标或窗口首条', () => {
    // 切换项目后旧 id 不属于新项目：服务端 episode_current 为空，窗口落在第一集
    expect(
      resolveWindowedEpisodeId(
        { episode_current: null, episodes: [{ id: 'e1', episode_no: 1, title: '一' }] },
        'stale-from-other-project',
      ),
    ).toBe('e1')
  })

  it('项目一集都没有时返回 null，不得误报为某一集', () => {
    expect(resolveWindowedEpisodeId({ episode_current: null, episodes: [] }, 'e5')).toBeNull()
  })
})

describe('窗口化查询串', () => {
  it('只写入有值的参数', () => {
    expect(pickerWindowParams(60)).toBe('episode_limit=60')
    expect(pickerWindowParams(60, 'e20')).toBe('episode_limit=60&episode_cursor=e20')
  })

  it('all 筛选不入参，避免同一份数据产生两种 URL', () => {
    expect(pickerWindowParams(60, 'e20', { production: 'all' }))
      .toBe('episode_limit=60&episode_cursor=e20')
  })

  it('搜索词去空白后写入，空搜索不入参', () => {
    expect(pickerWindowParams(60, null, { query: '  孟浩 ' }))
      .toBe('episode_limit=60&episode_query=%E5%AD%9F%E6%B5%A9')
    expect(pickerWindowParams(60, null, { query: '   ' })).toBe('episode_limit=60')
  })
})
