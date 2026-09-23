import { describe, expect, it } from 'vitest'
import type { SeriesQueueState } from '../../api'
import {
  seriesBatchEnqueueHint,
  seriesConcurrencyPhrase,
  seriesQueueStatusText,
  seriesSkippedToastMessage,
  seriesTaskProgressLabel,
  seriesTaskStartAvailability,
} from './seriesTaskText'

// 既有的 seriesBatchAvailability/seriesTaskTitle 等纯函数测试历史上留在
// frontend/src/pages/SeriesPage.test.ts（该页面组合了 pages/series/* 下的组件）；
// 本文件只覆盖这次新增/改动的部分，不重复既有覆盖，也不去碰那个文件——
// 它不在本次改动的文件所有权范围内。

describe('seriesTaskProgressLabel — merge 阶段（P1-7）', () => {
  it('running + current_stage=merge：显示"合成连播成片"，不是"尚未开始"', () => {
    const label = seriesTaskProgressLabel({
      status: 'running',
      current_episode_no: null,
      current_stage: 'merge',
      queue_position: null,
    })
    expect(label).toBe('合成连播成片')
    expect(label).not.toBe('尚未开始')
  })

  it('running + 具体某集某步：不受 merge 分支影响，行为不变', () => {
    const label = seriesTaskProgressLabel({
      status: 'running',
      current_episode_no: 3,
      current_stage: 'video',
      queue_position: null,
    })
    expect(label).toBe('第 3 集 · 生成台')
  })

  it('running 但 current_episode_no/current_stage 都缺失（真正没开始的边界）：退回"尚未开始"', () => {
    const label = seriesTaskProgressLabel({
      status: 'running',
      current_episode_no: null,
      current_stage: null,
      queue_position: null,
    })
    expect(label).toBe('尚未开始')
  })
})

describe('seriesQueueStatusText — 并行任务展示（P1-6）', () => {
  const base: SeriesQueueState = {
    paused: false, running_task_id: null, queued_count: 0, stop_reason: null,
  }

  it('running_task_ids 有多个：展示并行任务数，不再只报一个 id', () => {
    const text = seriesQueueStatusText({
      ...base, running_task_id: 'st_a', running_task_ids: ['st_a', 'st_b', 'st_c'], queued_count: 2,
    })
    expect(text).toBe('正在并行执行 3 个任务，还有 2 个排队')
  })

  it('running_task_ids 缺失（老响应）：退回按 running_task_id 单值展示，行为不变', () => {
    const text = seriesQueueStatusText({ ...base, running_task_id: 'st_a', queued_count: 0 })
    expect(text).toBe('正在执行 st_a')
  })

  it('running_task_ids 为空数组、running_task_id 也为空：队列空闲', () => {
    const text = seriesQueueStatusText({ ...base, running_task_ids: [] })
    expect(text).toBe('队列空闲')
  })
})

describe('seriesBatchEnqueueHint — 批量执行提示文案（P1-6）', () => {
  it('concurrency=3（后端默认）：如实展示并行数，不再声称"一次只跑一个任务"', () => {
    const hint = seriesBatchEnqueueHint(3)
    expect(hint).toContain('最多同时执行 3 个任务')
    expect(hint).not.toContain('一次只跑一个任务')
  })

  it('concurrency=1（管理员把并发调到 1）：不用"最多同时执行 1 个"这种别扭说法', () => {
    const hint = seriesBatchEnqueueHint(1)
    expect(hint).not.toContain('最多同时执行 1 个')
    expect(hint).toContain('按勾选顺序排队执行')
  })

  it('concurrency 缺失（老响应）：不编造具体并行数字', () => {
    const hint = seriesBatchEnqueueHint(undefined)
    expect(hint).not.toMatch(/最多同时执行 \d+ 个任务/)
  })
})

describe('seriesTaskStartAvailability — 单任务开始按钮可用性（P2-5）', () => {
  const runnable = { status: 'idle' as const, missing_episode_nos: [] as number[], film_stale: false }

  it('已完成且成片未过期：禁用并给出原因，不再让用户点了却静默 skipped', () => {
    const result = seriesTaskStartAvailability({ ...runnable, status: 'succeeded', film_stale: false })
    expect(result.disabled).toBe(true)
    expect(result.reason).toBe('已完成且成片未过期，无需重新执行')
  })

  it('已完成但成片已过期（film_stale=true）：允许重新执行', () => {
    const result = seriesTaskStartAvailability({ ...runnable, status: 'succeeded', film_stale: true })
    expect(result.disabled).toBe(false)
    expect(result.reason).toBeNull()
  })

  it('运行中/排队中/区间缺集：沿用既有禁用判据不变', () => {
    expect(seriesTaskStartAvailability({ ...runnable, status: 'running' }).disabled).toBe(true)
    expect(seriesTaskStartAvailability({ ...runnable, status: 'queued' }).disabled).toBe(true)
    expect(seriesTaskStartAvailability({ ...runnable, missing_episode_nos: [3, 4] }).disabled).toBe(true)
  })

  it('idle/failed/cancelled 且区间完整：允许开始', () => {
    expect(seriesTaskStartAvailability({ ...runnable, status: 'idle' }).disabled).toBe(false)
    expect(seriesTaskStartAvailability({ ...runnable, status: 'failed' }).disabled).toBe(false)
    expect(seriesTaskStartAvailability({ ...runnable, status: 'cancelled' }).disabled).toBe(false)
  })
})

describe('seriesConcurrencyPhrase — 页头副标题并行短语（P1-6 续）', () => {
  it('concurrency=3：如实展示数字，不再是"串行"', () => {
    expect(seriesConcurrencyPhrase(3)).toBe('最多同时执行 3 个任务')
  })

  it('concurrency 缺失或 ≤1：不编造具体数字', () => {
    expect(seriesConcurrencyPhrase(undefined)).toBe('按顺序执行')
    expect(seriesConcurrencyPhrase(1)).toBe('按顺序执行')
  })
})

describe('seriesSkippedToastMessage — 入队 skipped 提示（P2-5 续）', () => {
  it('空数组：返回 null，调用方不弹 toast', () => {
    expect(seriesSkippedToastMessage([])).toBeNull()
  })

  it('单个 skipped：报出数量与原因', () => {
    const message = seriesSkippedToastMessage([{ task_id: 'st_1', reason: '已完成，成片未过期' }])
    expect(message).toBe('已跳过 1 个任务：已完成，成片未过期')
  })

  it('多个 skipped 且原因不同：都列出，逗号分隔的原因用「；」连接', () => {
    const message = seriesSkippedToastMessage([
      { task_id: 'st_1', reason: '已完成，成片未过期' },
      { task_id: 'st_2', reason: '缺第 3、4 集' },
    ])
    expect(message).toBe('已跳过 2 个任务：已完成，成片未过期；缺第 3、4 集')
  })

  it('多个 skipped 同一原因：不重复念叨同一句话', () => {
    const message = seriesSkippedToastMessage([
      { task_id: 'st_1', reason: '已完成，成片未过期' },
      { task_id: 'st_2', reason: '已完成，成片未过期' },
    ])
    expect(message).toBe('已跳过 2 个任务：已完成，成片未过期')
  })
})
