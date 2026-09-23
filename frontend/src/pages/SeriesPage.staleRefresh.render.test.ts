import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SeriesTaskDetail as SeriesTaskDetailData, SeriesTaskListResponse } from '../api'

// 连播任务列表/详情都走 usePoll（见 useSeriesTaskListState.ts / SeriesTaskDetail.tsx），
// 队列/任务运行时持续轮询；轮询失败不清空 data，已有数据后再失败的 error 此前
// 只会被早退的 `<QueryState hasData={false}>` 分支处理——那个分支只在 data 从没
// 取到过时才执行，数据到手后再失败就没有任何提示（2026-09-23 补丁，同
// orgs/resources 各面板 2c96b89c 的分工）。这里复用 SeriesPage.render.test.ts 的
// mock 配方（同一份 usePoll deps 长度区分资源的写法），但把 error 做成可变字段，
// 覆盖"先成功、后台再失败"的时序——已有的 render 测试文件没有这条路径，且已经
// 卡在 FILE_CONVENTIONS.toml 的行数棘轮上，装不下，另开新文件。

const { mockNav, mockData } = vi.hoisted(() => ({
  mockNav: { projectId: 'proj1' as string | null, taskId: null as string | null },
  mockData: {
    list: null as SeriesTaskListResponse | null,
    listError: null as string | null,
    detail: null as SeriesTaskDetailData | null,
    detailError: null as string | null,
  },
}))

vi.mock('../App', () => ({
  useNav: () => ({
    projectId: mockNav.projectId, episodeId: null, taskId: mockNav.taskId,
    chapterIdx: null, view: 'series',
    go: (_v: unknown, _pid: unknown, _eid?: unknown, _cidx?: unknown, _h?: unknown, taskId?: string | null) => {
      mockNav.taskId = taskId ?? null
    },
    requestNavigation: () => {}, toast: () => {}, registerNavigationGuard: () => {},
  }),
  usePoll: (_fetcher: unknown, _interval: unknown, deps: unknown[]) => {
    const isExports = deps.length === 1
    if (isExports) return { data: { exports: [] }, error: null, status: null, loading: false, refresh: async () => null }
    const isDetail = Boolean(mockNav.taskId)
    return {
      data: isDetail ? mockData.detail : mockData.list,
      error: isDetail ? mockData.detailError : mockData.listError,
      status: null, loading: false,
      refresh: async (_o?: { force?: boolean }) => (isDetail ? mockData.detail : mockData.list),
    }
  },
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import SeriesPage from './SeriesPage'

async function renderPage() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => { renderer = TestRenderer.create(React.createElement(SeriesPage)) })
  return renderer
}

const listResponse = (): SeriesTaskListResponse => ({
  queue: { paused: false, running_task_id: null, queued_count: 0, stop_reason: null, concurrency: 3 },
  totals: { all: 1, idle: 1, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
  episodes: { total: 10, min_no: 1, max_no: 10 },
  max_span: 10, default_group_size: 10, offset: 0, limit: 50,
  tasks: [{
    task_id: 'st_1', index: 1, title: '', episode_from: 1, episode_to: 10, episode_count: 10,
    missing_episode_nos: [], status: 'idle', queue_position: null, current_episode_no: null,
    current_stage: null, steps_done: 0, steps_total: 50, error: null, film: null, film_stale: false,
    updated_at: 0, finished_at: null,
  }],
})

const detailResponse = (): SeriesTaskDetailData => ({
  task_id: 'st_1', index: 1, title: '', episode_from: 1, episode_to: 2, episode_count: 2,
  missing_episode_nos: [], status: 'running', queue_position: null, current_episode_no: 1,
  current_stage: 'storyboard', steps_done: 3, steps_total: 10, error: null, updated_at: 0,
  finished_at: null, film_stale: false, film: null,
  episodes: [{
    episode_id: 'ep-1', episode_no: 1, title: '',
    stages: { screenplay: 'done', storyboard: 'running', confirm: 'pending', video: 'pending', final: 'pending' },
    error: null,
  }],
})

describe('连播台——已有数据后台轮询刷新失败不得被吞', () => {
  beforeEach(() => {
    mockNav.projectId = 'proj1'; mockNav.taskId = null
    mockData.list = listResponse(); mockData.listError = null
    mockData.detail = null; mockData.detailError = null
  })
  afterEach(() => vi.unstubAllGlobals())

  it('列表页：已有任务列表时轮询失败，显示「连播任务列表刷新失败」，旧列表不清空', async () => {
    mockData.listError = '轮询失败测试'
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('连播任务列表刷新失败')
    expect(serialized).toContain('轮询失败测试')
    expect(serialized).toContain('第 1-10 集') // 反向断言：旧数据仍在
    await act(async () => { renderer.unmount() })
  })

  it('列表页：error 清空后横幅消失', async () => {
    mockData.listError = null
    const renderer = await renderPage()
    expect(JSON.stringify(renderer.toJSON())).not.toContain('刷新失败')
    await act(async () => { renderer.unmount() })
  })

  it('详情页：已有任务详情时轮询失败，显示「连播任务详情刷新失败」，旧详情不清空', async () => {
    mockNav.taskId = 'st_1'
    mockData.detail = detailResponse()
    mockData.detailError = '轮询失败测试'
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('连播任务详情刷新失败')
    expect(serialized).toContain('轮询失败测试')
    expect(serialized).toContain('第 1-2 集')
    await act(async () => { renderer.unmount() })
  })
})
