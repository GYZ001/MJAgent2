import { describe, expect, it, vi } from 'vitest'
import {
  canScanPortraitGaps,
  generateFirstThreeEpisodeVideos,
  resolveEpisodePage,
} from './EpisodesPage'

describe('人物定妆缺口扫描门禁', () => {
  it('人物谱未就绪时不请求缺口接口', () => {
    expect(canScanPortraitGaps(undefined)).toBe(false)
    expect(canScanPortraitGaps({ bible_status: 'running', bible: null })).toBe(false)
    expect(canScanPortraitGaps({ bible_status: 'ready', bible: null })).toBe(false)
  })

  it('人物谱已就绪且有实体时才允许扫描', () => {
    expect(canScanPortraitGaps({
      bible_status: 'ready',
      bible: {} as never,
    })).toBe(true)
  })
})

describe('分集页码跳转', () => {
  it('空值和当前页给出明确反馈', () => {
    expect(resolveEpisodePage('', 128, 1)).toEqual({
      page: 1,
      message: '请输入 1 到 128 之间的页码',
    })
    expect(resolveEpisodePage('1', 128, 1).message).toContain('当前已是')
  })

  it('越界页码钳制到首尾并说明原因', () => {
    expect(resolveEpisodePage('0', 128, 20)).toEqual({
      page: 1,
      message: '页码不能小于 1，已跳到第一页',
    })
    expect(resolveEpisodePage('999', 128, 20)).toEqual({
      page: 128,
      message: '页码不能超过 128，已跳到最后一页',
    })
  })

  it('合法页码直接跳转', () => {
    expect(resolveEpisodePage('42', 128, 1)).toEqual({
      page: 42,
      message: '已跳到第 42 页',
    })
  })
})

describe('生成前三集视频', () => {
  it('按 episode_no 提交前三个 ID，并显式携带时限和授权参数', async () => {
    const get = vi.fn(async () => ({
      episodes: [
        { id: 'episode-3', episode_no: 3 },
        { id: 'episode-1', episode_no: 1 },
        { id: 'episode-4', episode_no: 4 },
        { id: 'episode-2', episode_no: 2 },
      ],
    }))
    const projectVideoCompletion = vi.fn(async () => ({ status: 'accepted' }))

    await expect(generateFirstThreeEpisodeVideos('project-1', 'first-three-1', {
      get,
      projectVideoCompletion,
    })).resolves.toBe(3)

    expect(get).toHaveBeenCalledWith(
      '/projects/project-1?view=episodes&page=1&page_size=3&status_filter=all',
    )
    expect(projectVideoCompletion).toHaveBeenCalledWith('project-1', {
      episode_ids: ['episode-1', 'episode-2', 'episode-3'],
      wall_clock_cap_s: 4 * 60 * 60,
      allow_fallback_adopt: true,
      allow_storyboard_edit: false,
      idempotency_key: 'first-three-1',
    })
  })

  it('项目少于三集时只提交实际存在的非空 ID', async () => {
    const projectVideoCompletion = vi.fn(async () => ({ status: 'accepted' }))

    await expect(generateFirstThreeEpisodeVideos('project-2', 'first-three-2', {
      get: vi.fn(async () => ({
        episodes: [
          { id: 'episode-2', episode_no: 2 },
          { id: 'episode-1', episode_no: 1 },
          { id: '   ', episode_no: 3 },
        ],
      })),
      projectVideoCompletion,
    })).resolves.toBe(2)

    expect(projectVideoCompletion.mock.calls[0][1]).toMatchObject({
      episode_ids: ['episode-1', 'episode-2'],
    })
  })

  it('空项目不提交视频补全请求', async () => {
    const projectVideoCompletion = vi.fn(async () => ({ status: 'accepted' }))

    await expect(generateFirstThreeEpisodeVideos('project-empty', 'first-three-empty', {
      get: vi.fn(async () => ({ episodes: [] })),
      projectVideoCompletion,
    })).resolves.toBe(0)

    expect(projectVideoCompletion).not.toHaveBeenCalled()
  })

  it('当前页面筛选结果不影响独立第一页的选择', async () => {
    const currentFilteredEpisodeIds = ['episode-9']
    const get = vi.fn(async () => ({
      episodes: [
        { id: 'episode-2', episode_no: 2 },
        { id: 'episode-1', episode_no: 1 },
      ],
    }))
    const projectVideoCompletion = vi.fn(async () => ({ status: 'accepted' }))

    await generateFirstThreeEpisodeVideos('project-filtered', 'first-three-filtered', {
      get,
      projectVideoCompletion,
    })

    expect(get).toHaveBeenCalledWith(
      '/projects/project-filtered?view=episodes&page=1&page_size=3&status_filter=all',
    )
    expect(projectVideoCompletion.mock.calls[0][1]).toMatchObject({
      episode_ids: ['episode-1', 'episode-2'],
    })
    expect(projectVideoCompletion.mock.calls[0][1]).not.toMatchObject({
      episode_ids: currentFilteredEpisodeIds,
    })
  })
})
