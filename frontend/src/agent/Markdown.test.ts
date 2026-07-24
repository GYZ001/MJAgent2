import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import Markdown from './Markdown'

describe('Markdown', () => {
  it('把 GFM 表格渲染成语义化 table，不暴露原始管道符', () => {
    const html = renderToStaticMarkup(createElement(Markdown, {
      text: '| 字段 | 值 |\n|---|---|\n| 剧本状态 | **已就绪** |',
    }))

    expect(html).toContain('<table class="md-table">')
    expect(html).toContain('<th>字段</th>')
    expect(html).toContain('<td><strong>已就绪</strong></td>')
    expect(html).not.toContain('|---|')
  })
})
