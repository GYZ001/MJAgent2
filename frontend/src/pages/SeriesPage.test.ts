import { describe, expect, it } from 'vitest'
import type { SeriesTaskPlanResponse, SeriesTaskStatus, SeriesTaskSummary } from '../api'
import {
  deselectTasks,
  formatFilmSize,
  formatGB,
  selectTasks,
  seriesBatchAvailability,
  seriesPlanSummaryText,
  seriesQueueStatusText,
  seriesTaskProgressLabel,
  seriesTaskProgressPercent,
  seriesTaskStatusLabel,
  seriesTaskStatusTone,
  seriesTaskTitle,
  toggleTaskSelection,
  validateGroupSize,
} from './series/seriesTaskText'
import {
  seriesRepairView,
  seriesStageMeta,
  seriesStageStampClass,
} from './series/SeriesProgressBoard'
import { formatFilmDuration, seriesChapterSeekTime } from './series/SeriesFilmPlayer'

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
  updated_at: 0,
  finished_at: null,
  ...overrides,
})

describe('切分预览 validateGroupSize/seriesPlanSummaryText', () => {
  it('1–10 之间的整数合法', () => {
    expect(validateGroupSize(1)).toEqual({ ok: true })
    expect(validateGroupSize(10)).toEqual({ ok: true })
  })

  it('超出范围或非整数都拒绝，并说明范围', () => {
    expect(validateGroupSize(0).ok).toBe(false)
    expect(validateGroupSize(11).ok).toBe(false)
    expect(validateGroupSize(3.5).ok).toBe(false)
    expect(validateGroupSize(11).reason).toContain('1–10')
  })

  it('预览结果的组数计算：将新建 X 组、已存在 Y 组，并带上总数', () => {
    const plan: SeriesTaskPlanResponse = {
      group_size: 10,
      total_groups: 160,
      new_groups: 155,
      existing_groups: 5,
      episodes: { total: 1600, min_no: 1, max_no: 1600 },
      groups: [],
      truncated: true,
    }
    expect(seriesPlanSummaryText(plan)).toBe('将新建 155 组、已存在 5 组（共 160 组）')
  })
})

describe('跨页勾选集合的增删', () => {
  it('toggleTaskSelection：未选中则加入，已选中则移除，不影响其余元素', () => {
    const base = new Set(['a', 'b'])
    const added = toggleTaskSelection(base, 'c')
    expect(added).toEqual(new Set(['a', 'b', 'c']))
    expect(base).toEqual(new Set(['a', 'b'])) // 不修改传入的集合本身
    const removed = toggleTaskSelection(added, 'b')
    expect(removed).toEqual(new Set(['a', 'c']))
  })

  it('selectTasks/deselectTasks：翻页后批量加入或移除，跨页已选集合不丢失', () => {
    let selected = new Set(['a'])
    selected = selectTasks(selected, ['b', 'c']) // 第 2 页「全选本页」
    expect(selected).toEqual(new Set(['a', 'b', 'c']))
    selected = deselectTasks(selected, ['a']) // 回到第 1 页取消勾选其中一个
    expect(selected).toEqual(new Set(['b', 'c'])) // 第 2 页的勾选依然保留
  })
})

describe('批量按钮可用性判据 seriesBatchAvailability', () => {
  it('无选中：三个按钮都禁用', () => {
    expect(seriesBatchAvailability([])).toEqual({
      enqueueDisabled: true,
      cancelDisabled: true,
      exportDisabled: true,
    })
  })

  it('选中含运行中/排队中的任务：取消按钮可用', () => {
    const selected = [task({ task_id: 'a', status: 'idle' }), task({ task_id: 'b', status: 'running' })]
    const result = seriesBatchAvailability(selected)
    expect(result.enqueueDisabled).toBe(false)
    expect(result.cancelDisabled).toBe(false)
  })

  it('选中全是空闲任务：取消按钮禁用（没有可取消的对象）', () => {
    const selected = [task({ task_id: 'a', status: 'idle' }), task({ task_id: 'b', status: 'succeeded' })]
    expect(seriesBatchAvailability(selected).cancelDisabled).toBe(true)
  })

  it('选中无成片时导出按钮禁用；有一个已出片就可用', () => {
    const noFilm = [task({ task_id: 'a', status: 'idle', film: null })]
    expect(seriesBatchAvailability(noFilm).exportDisabled).toBe(true)
    const withFilm = [
      task({ task_id: 'a', status: 'idle', film: null }),
      task({ task_id: 'b', status: 'succeeded', film: { url: '/x', duration_s: 60, size_bytes: 1024, created_at: 0 } }),
    ]
    expect(seriesBatchAvailability(withFilm).exportDisabled).toBe(false)
  })
})

describe('状态文案映射 seriesTaskStatusLabel/seriesTaskStatusTone/seriesTaskTitle', () => {
  it('六种任务状态各自映射到中文文案与色调', () => {
    const cases: [SeriesTaskStatus, string, 'grey' | 'gold' | 'green' | 'red'][] = [
      ['idle', '未开始', 'grey'],
      ['queued', '排队中', 'gold'],
      ['running', '执行中', 'gold'],
      ['succeeded', '已完成', 'green'],
      ['failed', '失败', 'red'],
      ['cancelled', '已取消', 'grey'],
    ]
    for (const [status, label, tone] of cases) {
      expect(seriesTaskStatusLabel(status)).toBe(label)
      expect(seriesTaskStatusTone(status)).toBe(tone)
    }
  })

  it('标题为空串时按「第 X-Y 集」兜底，非空则原样展示', () => {
    expect(seriesTaskTitle({ title: '', episode_from: 1, episode_to: 10 })).toBe('第 1-10 集')
    expect(seriesTaskTitle({ title: '自定义标题', episode_from: 1, episode_to: 10 })).toBe('自定义标题')
  })
})

describe('进度百分比 seriesTaskProgressPercent', () => {
  it('正常场景按比例四舍五入', () => {
    expect(seriesTaskProgressPercent(25, 50)).toBe(50)
    expect(seriesTaskProgressPercent(1, 3)).toBe(33)
  })

  it('steps_total<=0 时按 0 处理，不产出 NaN/Infinity', () => {
    expect(seriesTaskProgressPercent(0, 0)).toBe(0)
    expect(seriesTaskProgressPercent(5, 0)).toBe(0)
    expect(seriesTaskProgressPercent(5, -1)).toBe(0)
  })

  it('结果夹在 [0,100] 之间', () => {
    expect(seriesTaskProgressPercent(80, 50)).toBe(100)
    expect(seriesTaskProgressPercent(-5, 50)).toBe(0)
  })
})

describe('进度定位文案 seriesTaskProgressLabel', () => {
  it('执行中显示第几集第几步；排队中显示队列位次；终态各自归类', () => {
    expect(seriesTaskProgressLabel(task({ status: 'running', current_episode_no: 3, current_stage: 'storyboard' })))
      .toBe('第 3 集 · 分镜台')
    expect(seriesTaskProgressLabel(task({ status: 'queued', queue_position: 2 }))).toBe('排队中（第 2 位）')
    expect(seriesTaskProgressLabel(task({ status: 'succeeded' }))).toBe('已完成')
    expect(seriesTaskProgressLabel(task({ status: 'idle' }))).toBe('尚未开始')
  })
})

describe('队列状态条文案 seriesQueueStatusText', () => {
  it('连续失败停队优先于其它状态，展示 stop_reason 原文', () => {
    expect(seriesQueueStatusText({
      paused: true,
      running_task_id: null,
      queued_count: 3,
      stop_reason: '连续 3 个任务失败，已自动暂停',
    })).toBe('已连续失败自动暂停：连续 3 个任务失败，已自动暂停')
  })

  it('手动暂停、正在执行、空闲三种状态各自的文案', () => {
    expect(seriesQueueStatusText({ paused: true, running_task_id: null, queued_count: 0, stop_reason: null }))
      .toBe('队列已暂停')
    expect(seriesQueueStatusText({ paused: false, running_task_id: 'st_a', queued_count: 2, stop_reason: null }))
      .toBe('正在执行 st_a，还有 2 个排队')
    expect(seriesQueueStatusText({ paused: false, running_task_id: null, queued_count: 0, stop_reason: null }))
      .toBe('队列空闲')
  })
})

describe('进度板印章映射 seriesStageMeta/seriesStageStampClass', () => {
  it('五种步骤状态各自映射到正确的文案与色调', () => {
    expect(seriesStageMeta('pending')).toEqual({ label: '待办', tone: 'grey' })
    expect(seriesStageMeta('running')).toEqual({ label: '进行中', tone: 'gold' })
    expect(seriesStageMeta('done')).toEqual({ label: '完成', tone: 'green' })
    expect(seriesStageMeta('failed')).toEqual({ label: '失败', tone: 'red' })
    const skipped = seriesStageMeta('skipped')
    expect(skipped.tone).toBe('grey')
    expect(skipped.label).toContain('✓')
  })

  it('未知/缺失状态按 pending 兜底，不当作空值裸渲染', () => {
    expect(seriesStageMeta(undefined)).toEqual({ label: '待办', tone: 'grey' })
  })

  it('stamp 类名拼接色调', () => {
    expect(seriesStageStampClass('done')).toBe('stamp green')
    expect(seriesStageStampClass('failed')).toBe('stamp red')
  })
})

describe('失败步骤到工作台的跳转映射 seriesRepairView', () => {
  it('五个单集步骤各自映射到对应工作台', () => {
    expect(seriesRepairView('screenplay')).toBe('script')
    expect(seriesRepairView('storyboard')).toBe('board')
    expect(seriesRepairView('confirm')).toBe('board')
    expect(seriesRepairView('video')).toBe('wall')
    expect(seriesRepairView('final')).toBe('cinema')
  })

  it('合成连播成片（merge）不属于任何单集工作台，不给出跳转目标', () => {
    expect(seriesRepairView('merge')).toBeNull()
    expect(seriesRepairView(null)).toBeNull()
    expect(seriesRepairView(undefined)).toBeNull()
  })
})

describe('连播成片时长/大小格式化与章节跳转', () => {
  it('不足一小时只显示分:秒，满一小时带上时', () => {
    expect(formatFilmDuration(65)).toBe('1:05')
    expect(formatFilmDuration(3661)).toBe('1:01:01')
    expect(formatFilmDuration(0)).toBe('0:00')
  })

  it('按 episode_no 找到对应章节的起点秒数；找不到返回 null，不伪造跳到 0 秒', () => {
    const chapters = [
      { episode_no: 1, start_s: 0, duration_s: 120 },
      { episode_no: 2, start_s: 120, duration_s: 90 },
    ]
    expect(seriesChapterSeekTime(chapters, 2)).toBe(120)
    expect(seriesChapterSeekTime(chapters, 9)).toBeNull()
  })

  it('formatFilmSize 按量级自适应单位，formatGB 固定用 GB（导出面板的合计口径）', () => {
    expect(formatFilmSize(500)).toBe('500 B')
    expect(formatFilmSize(2048)).toBe('2 KB')
    expect(formatFilmSize(5 * 1024 * 1024)).toBe('5.0 MB')
    expect(formatFilmSize(1.5 * 1024 ** 3)).toBe('1.50 GB')
    expect(formatGB(2 * 1024 ** 3)).toBe('2.00 GB')
  })
})
