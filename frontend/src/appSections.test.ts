import { describe, expect, it } from 'vitest'
import { SECTIONS, visibleSectionsFor } from './appSections'

describe('侧栏入口的可见性', () => {
  it('没进项目时不渲染任何工作台', () => {
    expect(visibleSectionsFor(null, true)).toEqual([])
    expect(visibleSectionsFor(null, false)).toEqual([])
  })

  it('观测台只对租户管理员出现，其余入口两种身份完全一致', () => {
    const memberKeys = visibleSectionsFor('p1', false).map((section) => section.key)
    const adminKeys = visibleSectionsFor('p1', true).map((section) => section.key)

    expect(memberKeys).not.toContain('observability')
    expect(adminKeys).toContain('observability')
    // 只多这一个入口：别的工作台不许被 adminOnly 顺手带走。
    expect(adminKeys.filter((key) => key !== 'observability')).toEqual(memberKeys)
  })

  it('adminOnly 只标在观测台上', () => {
    expect(SECTIONS.filter((section) => section.adminOnly).map((s) => s.key))
      .toEqual(['observability'])
  })
})
