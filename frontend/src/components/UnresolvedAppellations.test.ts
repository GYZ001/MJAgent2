import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import UnresolvedAppellations from './UnresolvedAppellations'

/**
 * 映射台「未确定指代的称谓」次要列表：有数据渲染标题+每条称谓/段号，没有数据
 * （字段缺失或空数组，两种都可能出现——旧产物没有这个字段、新产物本集确实没有
 * 这类称谓）整块不渲染，不让用户误以为"系统认定这是某个独立出镜的人"。
 */
describe('UnresolvedAppellations', () => {
  it('有数据时渲染标题与每条称谓/段号', () => {
    const html = renderToStaticMarkup(createElement(UnresolvedAppellations, {
      items: [
        { label: '温老师', segment_indexes: [5] },
        { label: '有人', segment_indexes: [7] },
      ],
    }))
    expect(html).toContain('未确定指代的称谓（不会作为独立人物进入分镜） · 2')
    expect(html).toContain('温老师')
    expect(html).toContain('有人')
  })

  it('字段缺失（undefined）时整块不渲染', () => {
    const html = renderToStaticMarkup(createElement(UnresolvedAppellations, {}))
    expect(html).toBe('')
  })

  it('空数组时整块不渲染', () => {
    const html = renderToStaticMarkup(createElement(UnresolvedAppellations, { items: [] }))
    expect(html).toBe('')
  })
})
