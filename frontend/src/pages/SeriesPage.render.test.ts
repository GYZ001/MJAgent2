import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SeriesFilmSnapshot } from '../api'

// 最小渲染用例：纯函数测试（SeriesPage.test.ts）各自独立验证 validateEpisodeRange/
// seriesStageMeta/seriesPrimaryAction 等逻辑，但测不出组件树本身是否按预期分支
// 渲染（例如"运行中应该把两个下拉框都禁用"这类跨组件的可见状态）。这里用
// react-test-renderer 真的挂载 SeriesPage，覆盖四个关键页面态：加载中、无运行、
// 运行中（进度板 + 禁用区间选择）、有连播成片（播放器 + 下载链接）。

const { mockPoll } = vi.hoisted(() => ({
  mockPoll: {
    data: null as SeriesFilmSnapshot | null,
    error: null as string | null,
    status: null as number | null,
    loading: false,
  },
}))

vi.mock('../App', () => ({
  useNav: () => ({
    projectId: 'proj1',
    episodeId: null,
    chapterIdx: null,
    view: 'series',
    go: () => {},
    requestNavigation: () => {},
    toast: () => {},
    registerNavigationGuard: () => {},
  }),
  usePoll: () => ({
    data: mockPoll.data,
    error: mockPoll.error,
    status: mockPoll.status,
    loading: mockPoll.loading,
    refresh: async () => mockPoll.data,
  }),
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

const snapshot = (overrides: Partial<SeriesFilmSnapshot> = {}): SeriesFilmSnapshot => ({
  run: null,
  film: null,
  episodes_available: [
    { episode_id: 'ep-1', episode_no: 1, title: null },
    { episode_id: 'ep-2', episode_no: 2, title: null },
    { episode_id: 'ep-3', episode_no: 3, title: null },
  ],
  ...overrides,
})

describe('连播台页面渲染', () => {
  beforeEach(() => {
    mockPoll.data = null
    mockPoll.error = null
    mockPoll.status = null
    mockPoll.loading = false
  })
  afterEach(() => vi.unstubAllGlobals())

  it('尚未拿到数据时渲染加载态，不渲染区间选择器/进度板', async () => {
    mockPoll.loading = true
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('正在加载')
    expect(serialized).not.toContain('series-range-picker')
    await act(async () => { renderer.unmount() })
  })

  it('无运行记录：状态条显示"尚未开始"，主按钮是"开始制作连播成片"', async () => {
    mockPoll.data = snapshot()
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('尚未开始')
    const buttons = renderer.root.findAll(
      node => node.type === 'button' && textOf(node).includes('开始制作连播成片'),
    )
    expect(buttons).toHaveLength(1)
    await act(async () => { renderer.unmount() })
  })

  it('运行中：进度板渲染每集五列步骤，区间选择器被禁用，主按钮是"暂停"', async () => {
    mockPoll.data = snapshot({
      run: {
        run_id: 'run-1',
        status: 'running',
        episode_from: 1,
        episode_to: 2,
        current_episode_no: 1,
        current_stage: 'storyboard',
        started_at: 0,
        updated_at: 0,
        finished_at: null,
        error: null,
        episodes: [
          {
            episode_id: 'ep-1',
            episode_no: 1,
            stages: { screenplay: 'done', storyboard: 'running', confirm: 'pending', video: 'pending', final: 'pending' },
            error: null,
          },
          {
            episode_id: 'ep-2',
            episode_no: 2,
            stages: { screenplay: 'pending', storyboard: 'pending', confirm: 'pending', video: 'pending', final: 'pending' },
            error: null,
          },
        ],
      },
    })
    const renderer = await renderPage()
    const serialized = JSON.stringify(renderer.toJSON())
    expect(serialized).toContain('连播制作中')
    expect(serialized).toContain('暂停')
    const selects = renderer.root.findAllByType('select')
    expect(selects).toHaveLength(2)
    expect(selects.every(select => select.props.disabled)).toBe(true)
    await act(async () => { renderer.unmount() })
  })

  it('有连播成片：渲染播放器与可下载的成片链接', async () => {
    mockPoll.data = snapshot({
      film: {
        url: '/media/project/series/ep1-ep2/film.mp4',
        path: 'series/ep1-ep2/film.mp4',
        duration_s: 125,
        size_bytes: 1024 * 1024 * 12,
        created_at: 0,
        episode_from: 1,
        episode_to: 2,
        chapters: [
          { episode_no: 1, start_s: 0, duration_s: 60 },
          { episode_no: 2, start_s: 60, duration_s: 65 },
        ],
      },
    })
    const renderer = await renderPage()
    const downloadLink = renderer.root.findAll(
      node => node.type === 'a' && textOf(node) === '下载连播成片',
    )[0]
    expect(downloadLink.props.href).toBe('/media/project/series/ep1-ep2/film.mp4')
    expect(downloadLink.props.download).toBe(true)
    await act(async () => { renderer.unmount() })
  })
})
