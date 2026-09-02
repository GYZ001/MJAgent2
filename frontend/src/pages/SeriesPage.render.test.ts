import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  SeriesExport,
  SeriesTaskDetail as SeriesTaskDetailData,
  SeriesTaskListResponse,
  SeriesTaskSummary,
} from '../api'

// 最小渲染用例：纯函数测试（SeriesPage.test.ts）各自独立验证 seriesBatchAvailability/
// seriesTaskProgressPercent 等逻辑，但测不出组件树本身是否按预期分支渲染（例如
// "勾选后批量条才出现""队列暂停时展示 stop_reason"这类跨组件的可见状态）。这里用
// react-test-renderer 真的挂载 SeriesPage，覆盖列表页与详情页的关键渲染态。
//
// usePoll 按 deps 数组长度区分资源：连播任务列表/详情都传 [projectId, x]（长度 2，
// 用 mockNav.taskId 是否有值再细分是列表还是详情），导出包轮询固定传
// [projectId]（长度 1）——与 useSeriesTaskListState.ts / SeriesTaskDetail.tsx 里
// 实际调用 usePoll 时传的 deps 一一对应，改了那边的 deps 形状这里也要跟着改。

// vi.mock 会被提升到文件顶部；工厂里只能引用以 `mock` 开头（经 vi.hoisted 声明）的变量。
const { mockNav, mockData } = vi.hoisted(() => ({
  mockNav: { projectId: 'proj1' as string | null, episodeId: null as string | null, taskId: null as string | null },
  mockData: {
    list: null as SeriesTaskListResponse | null,
    exports: { exports: [] as SeriesExport[] },
    detail: null as SeriesTaskDetailData | null,
  },
}))

vi.mock('../App', () => ({
  useNav: () => ({
    projectId: mockNav.projectId,
    episodeId: mockNav.episodeId,
    taskId: mockNav.taskId,
    chapterIdx: null,
    view: 'series',
    go: (_v: unknown, _pid: unknown, _eid?: unknown, _cidx?: unknown, _h?: unknown, taskId?: string | null) => {
      mockNav.taskId = taskId ?? null
    },
    requestNavigation: () => {},
    toast: () => {},
    registerNavigationGuard: () => {},
  }),
  usePoll: (_fetcher: unknown, _interval: unknown, deps: unknown[]) => {
    const isExports = deps.length === 1
    const data = isExports ? mockData.exports : (mockNav.taskId ? mockData.detail : mockData.list)
    return { data, error: null, status: null, loading: false, refresh: async () => data }
  },
}))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import SeriesPage from './SeriesPage'

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.filter((c): c is string => typeof c === 'string').join('')
}

async function renderPage() {
  let renderer!: TestRenderer.ReactTestRenderer
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(SeriesPage))
  })
  return renderer
}

const task = (overrides: Partial<SeriesTaskSummary> = {}): SeriesTaskSummary => ({
  task_id: 'st_1',
  index: 1,
  title: '',
  episode_from: 1,
  episode_to: 10,
  episode_count: 10,
  missing_episode_nos: [],
  status: 'idle',
  queue_position: null,
  current_episode_no: null,
  current_stage: null,
  steps_done: 0,
  steps_total: 50,
  error: null,
  film: null,
  film_stale: false,
  updated_at: 0,
  finished_at: null,
  ...overrides,
})

const listResponse = (overrides: Partial<SeriesTaskListResponse> = {}): SeriesTaskListResponse => ({
  queue: { paused: false, running_task_id: null, queued_count: 0, stop_reason: null },
  totals: { all: 3, idle: 3, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 },
  episodes: { total: 30, min_no: 1, max_no: 30 },
  max_span: 10,
  default_group_size: 10,
  offset: 0,
  limit: 50,
  tasks: [
    task({ task_id: 'st_1', index: 1, episode_from: 1, episode_to: 10 }),
    task({ task_id: 'st_2', index: 2, episode_from: 11, episode_to: 20 }),
    task({ task_id: 'st_3', index: 3, episode_from: 21, episode_to: 30 }),
  ],
  ...overrides,
})

describe('连播任务列表页渲染', () => {
  beforeEach(() => {
    mockNav.projectId = 'proj1'
    mockNav.taskId = null
    mockData.list = listResponse()
    mockData.exports = { exports: [] }
    mockData.detail = null
  })
  afterEach(() => vi.unstubAllGlobals())

  it('渲染出与任务数一致的表格行数', async () => {
    const renderer = await renderPage()
    const rows = renderer.root.findAllByType('tr')
    // 表头一行 + 3 条任务
    expect(rows).toHaveLength(4)
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('第 1-10 集')
    expect(serialized).toContain('第 11-20 集')
    await act(async () => { renderer.unmount() })
  })

  it('成片已过期的任务在成片列标出「可重新执行」，不被「已完成」盖住', async () => {
    // 跑成功过、但区间里某一集的成片后来重做了 → 后端 status 仍是 succeeded、
    // film_stale 为真。界面必须把这件事说出来，否则用户看到「已完成」就不会再点。
    const base = listResponse()
    base.tasks[0] = {
      ...base.tasks[0], status: 'succeeded', film_stale: true,
      film: { url: '/media/x.mp4', duration_s: 61, size_bytes: 1024, created_at: 0 },
    }
    mockData.list = base
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('已完成')
    expect(serialized).toContain('成片已过期，可重新执行')
    await act(async () => { renderer.unmount() })
  })

  it('总数超过一页时渲染分页控件', async () => {
    mockData.list = listResponse({ totals: { all: 120, idle: 120, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 } })
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('第 1 / 3 页')
    const prevBtn = renderer.root.findAll(n => n.type === 'button' && textOf(n).includes('上一页'))[0]
    const nextBtn = renderer.root.findAll(n => n.type === 'button' && textOf(n).includes('下一页'))[0]
    expect(prevBtn.props.disabled).toBe(true)
    expect(nextBtn.props.disabled).toBe(false)
    await act(async () => { renderer.unmount() })
  })

  it('未勾选时不显示批量条；勾选一行后批量条出现并显示已选数量', async () => {
    const renderer = await renderPage()
    expect(JSON.stringify(renderer.toJSON())).not.toContain('已选')
    const checkbox = renderer.root.findAll(
      n => n.type === 'input' && n.props['aria-label'] === '勾选 第 1-10 集',
    )[0]
    await act(async () => { checkbox.props.onChange() })
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('已选 1 个')
    expect(serialized).toContain('串行执行选中')
    await act(async () => { renderer.unmount() })
  })

  it('队列因连续失败自动暂停时显示 stop_reason 原文，并给出继续队列按钮', async () => {
    mockData.list = listResponse({
      queue: { paused: true, running_task_id: null, queued_count: 2, stop_reason: '连续 3 个任务失败，已自动暂停' },
    })
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('连续 3 个任务失败，已自动暂停')
    const resumeBtn = renderer.root.findAll(n => n.type === 'button' && textOf(n).includes('继续队列'))
    expect(resumeBtn.length).toBeGreaterThan(0)
    await act(async () => { renderer.unmount() })
  })
})

describe('连播任务详情页渲染', () => {
  beforeEach(() => {
    mockNav.projectId = 'proj1'
    mockNav.taskId = 'st_1'
    mockData.detail = {
      task_id: 'st_1',
      index: 1,
      title: '',
      episode_from: 1,
      episode_to: 2,
      episode_count: 2,
      missing_episode_nos: [],
      status: 'running',
      queue_position: null,
      current_episode_no: 1,
      current_stage: 'storyboard',
      steps_done: 6,
      steps_total: 10,
      error: null,
      updated_at: 0,
      finished_at: null,
      film_stale: false,
      film: {
        url: '/media/proj1/series/st_1/film.mp4',
        duration_s: 125,
        size_bytes: 1024 * 1024 * 12,
        created_at: 0,
        chapters: [
          { episode_no: 1, start_s: 0, duration_s: 60 },
          { episode_no: 2, start_s: 60, duration_s: 65 },
        ],
      },
      episodes: [
        {
          episode_id: 'ep-1',
          episode_no: 1,
          title: '',
          stages: { screenplay: 'done', storyboard: 'running', confirm: 'pending', video: 'pending', final: 'pending' },
          error: null,
        },
        {
          episode_id: 'ep-2',
          episode_no: 2,
          title: '',
          stages: { screenplay: 'pending', storyboard: 'pending', confirm: 'pending', video: 'pending', final: 'pending' },
          error: null,
        },
      ],
    }
  })
  afterEach(() => vi.unstubAllGlobals())

  it('按 taskId 渲染进度板（每集五列步骤）与播放器（含下载链接）', async () => {
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('第 1-2 集')
    expect(serialized).toContain('分镜台')
    const downloadLink = renderer.root.findAll(
      n => n.type === 'a' && textOf(n) === '下载连播成片',
    )[0]
    expect(downloadLink.props.href).toBe('/media/proj1/series/st_1/film.mp4')
    const backBtn = renderer.root.findAll(n => n.type === 'button' && textOf(n).includes('返回任务列表'))
    expect(backBtn.length).toBe(1)
    await act(async () => { renderer.unmount() })
  })
})
