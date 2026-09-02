import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { locationFor, resolveNavEpisodeId, routeFromPath, shouldRetryPollError, useNav } from './App'

describe('无分集工作台路由', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('为四个工作台保留稳定的项目级 URL', () => {
    expect(locationFor('script', 'project 1', null, null))
      .toBe('/projects/project%201/script')
    expect(locationFor('board', 'p1', null, null)).toBe('/projects/p1/board')
    expect(locationFor('wall', 'p1', null, null)).toBe('/projects/p1/wall')
    expect(locationFor('cinema', 'p1', null, null)).toBe('/projects/p1/cinema')
  })

  it('刷新项目级工作台 URL 后仍进入对应空状态', () => {
    expect(routeFromPath('/projects/p1/script')).toEqual({
      view: 'script',
      projectId: 'p1',
      episodeId: null,
      chapterIdx: null,
    })
    expect(routeFromPath('/projects/p1/board').view).toBe('board')
    expect(routeFromPath('/projects/p1/wall').view).toBe('wall')
    expect(routeFromPath('/projects/p1/cinema').view).toBe('cinema')
  })

  it('已有分集的工作台 URL 保持原有解析', () => {
    expect(routeFromPath('/projects/p1/episodes/e1/wall')).toEqual({
      view: 'wall',
      projectId: 'p1',
      episodeId: 'e1',
      chapterIdx: null,
    })
  })

  it('连播台不吃从别的工作台带过来的粘性分集 id，显式任务 id 照常进 URL', () => {
    // 分镜台选中 e1 → 点侧栏连播台：eid 未指定（沿用当前），连播台必须得到 null，
    // 否则 e1 会被编进 /series/e1 当成任务 id 去查详情而 404。
    expect(resolveNavEpisodeId('series', undefined, 'e1')).toBeNull()
    expect(locationFor('series', 'p1', resolveNavEpisodeId('series', undefined, 'e1'), null))
      .toBe('/projects/p1/series')
    // 列表 → 详情是显式导航，任务 id 照常进 URL；刷新后仍解析回同一个任务。
    expect(resolveNavEpisodeId('series', 'st_1', 'e1')).toBe('st_1')
    expect(locationFor('series', 'p1', 'st_1', null)).toBe('/projects/p1/series/st_1')
    expect(routeFromPath('/projects/p1/series/st_1').episodeId).toBe('st_1')
    // 其它工作台的「沿用当前」语义不变：映射台/分镜台之间切换要记住分集。
    expect(resolveNavEpisodeId('board', undefined, 'e1')).toBe('e1')
    expect(resolveNavEpisodeId('bible', undefined, 'e1')).toBe('e1')
    expect(resolveNavEpisodeId('studio', undefined, 'e1')).toBeNull()
  })

  it('项目观测台与系统设置使用隔离的稳定路由', () => {
    expect(locationFor('observability', 'project 1', null, null))
      .toBe('/projects/project%201/observability/jobs')
    expect(routeFromPath('/projects/p1/observability/calls')).toEqual({
      view: 'observability', projectId: 'p1', episodeId: null, chapterIdx: null,
    })
    expect(locationFor('system', null, null, null)).toBe('/system/overview')
    expect(routeFromPath('/system/settings')).toEqual({
      view: 'system', projectId: null, episodeId: null, chapterIdx: null,
    })
    expect(routeFromPath('/workspaces')).toEqual({
      view: 'studio', projectId: null, episodeId: null, chapterIdx: null,
    })
    expect(routeFromPath('/workspaces/new').view).toBe('studio')
  })

  it('导航 Provider 在热重载窗口缺失时按当前 URL 安全回退', () => {
    vi.stubGlobal('window', {
      location: {
        pathname: '/projects/p1/bible',
        assign: vi.fn(),
      },
    })
    const Probe = () => {
      const nav = useNav()
      return createElement('span', null, `${nav.view}:${nav.projectId}`)
    }

    expect(renderToStaticMarkup(createElement(Probe))).toContain('bible:p1')
  })

  it('对象不存在时停止自动轮询，瞬时故障仍允许恢复', () => {
    expect(shouldRetryPollError({ status: 404 })).toBe(false)
    expect(shouldRetryPollError({ status: 500 })).toBe(true)
    expect(shouldRetryPollError(new Error('网络中断'))).toBe(true)
  })
})
