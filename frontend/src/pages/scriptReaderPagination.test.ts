import { describe, expect, it } from 'vitest'
import { paginateItems, paginateManuscript, paginateSpine } from './scriptReaderPagination'

describe('剧本台只读模块分页', () => {
  it('按固定条数分页且不改变条目顺序', () => {
    expect(paginateItems([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]])
    expect(paginateItems([], 2)).toEqual([])
  })

  it('主线骨架按前提、节拍、收束和不拍内容的语义顺序分页', () => {
    const pages = paginateSpine({
      episode_premise: '前提',
      spine_beats: Array.from({ length: 4 }, (_, index) => ({
        beat_id: `S${index + 1}`,
        who: '主角',
        does: `行动${index + 1}`,
        turn: `变化${index + 1}`,
      })),
      must_keep_ending: '收束',
      drop_list: ['支线一', '支线二'],
    }, 3)

    expect(pages).toHaveLength(3)
    expect(pages.flat().map(item => item.kind)).toEqual([
      'premise',
      'beat',
      'beat',
      'beat',
      'beat',
      'ending',
      'drop',
      'drop',
    ])
  })

  it('正文优先在自然断点分页并保持原文字符完全不丢失', () => {
    const text = `第一段${'甲'.repeat(30)}。\n第二段${'乙'.repeat(45)}！\n第三段${'丙'.repeat(20)}。`
    const pages = paginateManuscript(text, 50)

    expect(pages.length).toBeGreaterThan(1)
    expect(pages.join('')).toBe(text)
  })

  it('没有自然断点的超长单行仍可分页且不会死循环', () => {
    const text = '长'.repeat(205)
    const pages = paginateManuscript(text, 60)

    expect(pages).toHaveLength(4)
    expect(pages.join('')).toBe(text)
  })
})
