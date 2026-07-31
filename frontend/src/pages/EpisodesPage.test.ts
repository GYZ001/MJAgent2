import { describe, expect, it } from 'vitest'
import { canScanPortraitGaps, resolveEpisodePage } from './EpisodesPage'

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
