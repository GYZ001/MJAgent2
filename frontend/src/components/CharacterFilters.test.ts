import { describe, expect, it } from 'vitest'
import {
  characterFilterActiveCount,
  EMPTY_CHARACTER_FILTERS,
  matchCharacterFilters,
} from './CharacterFilters'

describe('人物谱筛选', () => {
  it('只统计实际生效的筛选和非默认排序', () => {
    expect(characterFilterActiveCount(EMPTY_CHARACTER_FILTERS)).toBe(0)
    expect(characterFilterActiveCount({
      ...EMPTY_CHARACTER_FILTERS,
      portrait: 'no',
      sort: 'qa',
    })).toBe(2)
  })

  it('未出图筛选不会误命中已有定妆角色', () => {
    expect(matchCharacterFilters(
      { name: '萧炎', role: '主角' },
      '',
      { ...EMPTY_CHARACTER_FILTERS, portrait: 'no' },
      { availability: 'passed', hasPortrait: true },
    )).toBe(false)
  })
})
