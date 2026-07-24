import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import ThinkingBlock from './ThinkingBlock'

describe('ThinkingBlock', () => {
  it('流式时使用轻量的正在思考折叠行', () => {
    const html = renderToStaticMarkup(createElement(ThinkingBlock, {
      text: '正在检查分镜证据', streaming: true,
    }))

    expect(html).toContain('正在思考')
    expect(html).toContain('agent-thinking-dots')
    expect(html).toContain('正在检查分镜证据')
    expect(html).not.toContain('💭')
  })

  it('结束后显示已思考并默认折叠', () => {
    const html = renderToStaticMarkup(createElement(ThinkingBlock, {
      text: '已完成检查', streaming: false,
    }))

    expect(html).toContain('已思考')
    expect(html).toContain('aria-expanded="false"')
    expect(html).not.toContain('已完成检查')
  })
})
