import { describe, expect, it } from 'vitest'
import { seriesPrimaryAction, seriesRunStatusLabel, seriesRunStatusTone } from './SeriesPage'
import { SERIES_MAX_SPAN, validateEpisodeRange } from './series/EpisodeRangePicker'
import {
  seriesRepairView,
  seriesStageMeta,
  seriesStageStampClass,
} from './series/SeriesProgressBoard'
import { formatFilmDuration, seriesChapterSeekTime } from './series/SeriesFilmPlayer'
import type { SeriesEpisodeAvailable, SeriesRun } from '../api'

const ep = (episode_no: number, episode_id = `ep-${episode_no}`): SeriesEpisodeAvailable => ({
  episode_id,
  episode_no,
  title: null,
})

describe('区间校验 validateEpisodeRange', () => {
  const available = [ep(1), ep(2), ep(3), ep(4), ep(5)]

  it('起止未选时提示先选择', () => {
    expect(validateEpisodeRange(available, null, null)).toEqual({
      ok: false,
      reason: '请选择起始集与结束集',
    })
  })

  it('结束集早于起始集时拒绝', () => {
    const result = validateEpisodeRange(available, 3, 2)
    expect(result.ok).toBe(false)
    expect(result.reason).toBe('结束集不能早于起始集')
  })

  it('单集（from === to）合法', () => {
    const result = validateEpisodeRange(available, 2, 2)
    expect(result).toEqual({ ok: true, count: 1 })
  })

  it('跨度超过上限时拒绝并报出跨度数', () => {
    const wide = Array.from({ length: SERIES_MAX_SPAN + 5 }, (_, i) => ep(i + 1))
    const result = validateEpisodeRange(wide, 1, SERIES_MAX_SPAN + 1)
    expect(result.ok).toBe(false)
    expect(result.reason).toContain(`${SERIES_MAX_SPAN}`)
    expect(result.count).toBe(SERIES_MAX_SPAN + 1)
  })

  it('区间内缺集时列出缺的集号，不允许通过', () => {
    const withGap = [ep(1), ep(2), ep(4), ep(5)]
    const result = validateEpisodeRange(withGap, 1, 5)
    expect(result.ok).toBe(false)
    expect(result.missingEpisodeNos).toEqual([3])
    expect(result.reason).toContain('第 3 集')
  })

  it('区间内每一集都存在且跨度合规时通过，给出集数', () => {
    const result = validateEpisodeRange(available, 2, 5)
    expect(result).toEqual({ ok: true, count: 4 })
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

  it('合成长片（merge）不属于任何单集工作台，不给出跳转目标', () => {
    expect(seriesRepairView('merge')).toBeNull()
    expect(seriesRepairView(null)).toBeNull()
    expect(seriesRepairView(undefined)).toBeNull()
  })
})

describe('连播成片时长格式化与章节跳转 formatFilmDuration/seriesChapterSeekTime', () => {
  it('不足一小时只显示分:秒，满一小时带上时', () => {
    expect(formatFilmDuration(65)).toBe('1:05')
    expect(formatFilmDuration(3661)).toBe('1:01:01')
    expect(formatFilmDuration(0)).toBe('0:00')
  })

  it('按 episode_no 找到对应章节的起点秒数', () => {
    const chapters = [
      { episode_no: 1, start_s: 0, duration_s: 120 },
      { episode_no: 2, start_s: 120, duration_s: 90 },
    ]
    expect(seriesChapterSeekTime(chapters, 2)).toBe(120)
  })

  it('章节数据里没有这一集时返回 null，不伪造跳到 0 秒', () => {
    const chapters = [{ episode_no: 1, start_s: 0, duration_s: 120 }]
    expect(seriesChapterSeekTime(chapters, 9)).toBeNull()
  })
})

describe('主操作按钮判定 seriesPrimaryAction', () => {
  const run = (overrides: Partial<SeriesRun> = {}): SeriesRun => ({
    run_id: 'run-1',
    status: 'running',
    episode_from: 1,
    episode_to: 3,
    current_episode_no: 1,
    current_stage: 'screenplay',
    started_at: 0,
    updated_at: 0,
    finished_at: null,
    error: null,
    episodes: [],
    ...overrides,
  })

  it('没有 run 时是"开始"', () => {
    expect(seriesPrimaryAction(null)).toEqual({ kind: 'start', label: '开始制作连播成片' })
  })

  it('运行中是"暂停"', () => {
    expect(seriesPrimaryAction(run({ status: 'running' }))).toEqual({ kind: 'pause', label: '暂停' })
  })

  it('已暂停或失败都是"继续"', () => {
    expect(seriesPrimaryAction(run({ status: 'paused' }))).toEqual({ kind: 'resume', label: '继续' })
    expect(seriesPrimaryAction(run({ status: 'failed' }))).toEqual({ kind: 'resume', label: '继续' })
  })

  it('已完成或已取消都回到"开始"（可以另起一段）', () => {
    expect(seriesPrimaryAction(run({ status: 'succeeded' })).kind).toBe('start')
    expect(seriesPrimaryAction(run({ status: 'cancelled' })).kind).toBe('start')
  })
})

describe('状态条文案 seriesRunStatusLabel/seriesRunStatusTone', () => {
  it('五种终态/运行态各自翻译成中文状态条文案', () => {
    expect(seriesRunStatusLabel('running')).toBe('连播制作中')
    expect(seriesRunStatusLabel('paused')).toBe('已暂停')
    expect(seriesRunStatusLabel('failed')).toBe('失败')
    expect(seriesRunStatusLabel('succeeded')).toBe('已完成')
    expect(seriesRunStatusLabel('cancelled')).toBe('已取消')
  })

  it('没有 run 时显示"尚未开始"，不是空白或未知状态', () => {
    expect(seriesRunStatusLabel(null)).toBe('尚未开始')
    expect(seriesRunStatusLabel(undefined)).toBe('尚未开始')
  })

  it('色调映射：运行中金、完成绿、失败红、其余灰', () => {
    expect(seriesRunStatusTone('running')).toBe('gold')
    expect(seriesRunStatusTone('succeeded')).toBe('green')
    expect(seriesRunStatusTone('failed')).toBe('red')
    expect(seriesRunStatusTone('paused')).toBe('grey')
    expect(seriesRunStatusTone(null)).toBe('grey')
  })
})
