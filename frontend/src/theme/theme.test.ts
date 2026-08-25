import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_THEME_MODE,
  NIGHT_END_HOUR,
  NIGHT_START_HOUR,
  THEME_STORAGE_KEY,
  isNightHour,
  msUntilNextSwitch,
  nextSwitchAt,
  normalizeMode,
  resolveTheme,
} from './theme'

/** 本地时区的某天某点。跟随时间用的是 getHours()，所以测试也必须走本地时间。 */
const at = (hour: number, minute = 0, day = 24) => new Date(2026, 7, day, hour, minute, 0, 0)

describe('昼夜时段判定', () => {
  it('22:00 起入夜，08:00 整点回到亮色', () => {
    expect(isNightHour(at(21, 59))).toBe(false)
    expect(isNightHour(at(22, 0))).toBe(true)
    expect(isNightHour(at(3, 0))).toBe(true)
    expect(isNightHour(at(7, 59))).toBe(true)
    expect(isNightHour(at(8, 0))).toBe(false)
  })

  it('手动选定的亮/暗不受时间影响', () => {
    expect(resolveTheme('light', at(2, 0))).toBe('light')
    expect(resolveTheme('dark', at(12, 0))).toBe('dark')
    expect(resolveTheme('auto', at(2, 0))).toBe('dark')
    expect(resolveTheme('auto', at(12, 0))).toBe('light')
  })
})

describe('存量取值收敛', () => {
  it('空值与脏值都退回默认模式', () => {
    expect(normalizeMode(null)).toBe(DEFAULT_THEME_MODE)
    expect(normalizeMode('')).toBe(DEFAULT_THEME_MODE)
    expect(normalizeMode('Dark')).toBe(DEFAULT_THEME_MODE)
    expect(normalizeMode({ mode: 'dark' })).toBe(DEFAULT_THEME_MODE)
  })

  it('合法取值原样透传', () => {
    expect(normalizeMode('light')).toBe('light')
    expect(normalizeMode('dark')).toBe('dark')
    expect(normalizeMode('auto')).toBe('auto')
  })
})

describe('下一次昼夜切换时刻', () => {
  it('白天等今晚 22:00，凌晨等今早 08:00', () => {
    expect(nextSwitchAt(at(9, 0)).getHours()).toBe(NIGHT_START_HOUR)
    expect(nextSwitchAt(at(9, 0)).getDate()).toBe(24)
    expect(nextSwitchAt(at(3, 0)).getHours()).toBe(NIGHT_END_HOUR)
    expect(nextSwitchAt(at(3, 0)).getDate()).toBe(24)
  })

  it('22:00 之后等的是明早 08:00，不会算成今天的过去时刻', () => {
    const next = nextSwitchAt(at(23, 30))
    expect(next.getHours()).toBe(NIGHT_END_HOUR)
    expect(next.getDate()).toBe(25)
  })

  it('毫秒数为正且有下限，时钟被回拨也不会退化成忙循环', () => {
    expect(msUntilNextSwitch(at(21, 0))).toBe(60 * 60 * 1000)
    expect(msUntilNextSwitch(at(21, 59, 24))).toBe(60 * 1000)
    expect(msUntilNextSwitch(at(NIGHT_START_HOUR, 0))).toBeGreaterThan(0)
  })
})

describe('index.html 的防白闪脚本与常量同步', () => {
  // 那段脚本是 theme.ts 的最小复制（React 挂载前就要定皮肤），只能靠测试盯住。
  const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf-8')

  it('用同一个 storage key', () => {
    expect(html).toContain(`'${THEME_STORAGE_KEY}'`)
  })

  it('用同一个夜间时段', () => {
    expect(html).toContain(`hour >= ${NIGHT_START_HOUR} || hour < ${NIGHT_END_HOUR}`)
  })

  it('默认模式与 DEFAULT_THEME_MODE 一致', () => {
    expect(html).toContain(`mode = '${DEFAULT_THEME_MODE}'`)
  })
})
