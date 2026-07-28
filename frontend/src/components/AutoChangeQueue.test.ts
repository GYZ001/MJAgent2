import { describe, expect, it } from 'vitest'
import {
  autoChangeDecisionCopy,
  filterAutoChangeItems,
  normalizeEvidenceFragments,
} from './AutoChangeQueue'

describe('normalizeEvidenceFragments', () => {
  it('preserves valid array evidence and removes empty or malformed entries', () => {
    expect(normalizeEvidenceFragments(['  第一段  ', '', null, 3, '第二段'])).toEqual(['第一段', '第二段'])
  })

  it('supports historical string evidence', () => {
    expect(normalizeEvidenceFragments('  第五章 聚气  ')).toEqual(['第五章 聚气'])
  })

  it('treats missing or malformed evidence as empty', () => {
    expect(normalizeEvidenceFragments(null)).toEqual([])
    expect(normalizeEvidenceFragments({ text: '证据' })).toEqual([])
  })
})

describe('filterAutoChangeItems', () => {
  it('keeps internal character discovery out of the scene review queue', () => {
    const items = [
      { id: 'character', kind: 'new_character', character: '葛叶', status: 'auto_applied' },
      { id: 'scene', kind: 'scene_discovery', scene: '云岚宗广场', status: 'pending' },
    ]

    expect(filterAutoChangeItems(items, 'scene').map(item => item.id)).toEqual(['scene'])
    expect(filterAutoChangeItems(items, 'all')).toHaveLength(2)
  })
})

describe('更新建议影响确认', () => {
  it('新增场景明确说明不自动出图和收费', () => {
    const copy = autoChangeDecisionCopy(
      { kind: 'scene_discovery', scene: '云岚宗广场' },
      'approve',
    )
    expect(copy.message).toContain('加入当前场景库')
    expect(copy.message).toContain('不会自动生成图片或产生费用')
  })

  it('忽略建议说明当前采用版本保持不变', () => {
    const copy = autoChangeDecisionCopy(
      { kind: 'scene_state_change', scene: '萧家广场' },
      'reject',
    )
    expect(copy.details.join('；')).toContain('当前已采用版本')
    expect(copy.danger).toBe(true)
  })
})
