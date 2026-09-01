import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import PortraitPlaceholder from './PortraitPlaceholder'

/**
 * CLAUDE.md「拦住用户时必须给出路」：定妆照生成失败不能只是文字提示，必须真的
 * 有一条能走的路——这里验证 failed 态渲染出一个指向人物谱的可点击链接，而不是
 * 一段只能看不能点的静态文案。用 renderToStaticMarkup 而不挂 NavCtx（同
 * PortraitPlaceholder.tsx 顶部注释：useNav() fallback 会碰 window，这里没有 DOM）。
 */
describe('PortraitPlaceholder', () => {
  it('群演/一次性人物恒为无定妆照，不是可点击链接', () => {
    const html = renderToStaticMarkup(createElement(PortraitPlaceholder, {
      identityId: 'entity:abc123',
      project: { refs_status: 'running', id: 'p1' },
      className: 'thumb-empty',
    }))
    expect(html).toContain('无定妆照')
    expect(html).not.toContain('<a')
  })

  it('生成中/待生成是纯展示占位，不是链接', () => {
    const generating = renderToStaticMarkup(createElement(PortraitPlaceholder, {
      identityId: 'bible:张三', project: { refs_status: 'running', id: 'p1' }, className: 'thumb-empty',
    }))
    expect(generating).toContain('定妆照生成中')
    expect(generating).not.toContain('<a')

    const pending = renderToStaticMarkup(createElement(PortraitPlaceholder, {
      identityId: 'bible:张三', project: { refs_status: 'idle', id: 'p1' }, className: 'thumb-empty',
    }))
    expect(pending).toContain('定妆照待生成')
    expect(pending).not.toContain('<a')
  })

  it('生成失败态渲染一条真的指向人物谱的可点击链接，不把用户晾在原地', () => {
    const html = renderToStaticMarkup(createElement(PortraitPlaceholder, {
      identityId: 'bible:张三',
      project: { refs_status: 'failed', refs_target: '张三', id: 'proj-1' },
      className: 'thumb-empty',
    }))
    expect(html).toContain('定妆照生成失败')
    expect(html).toContain('<a')
    expect(html).toContain('href="/projects/proj-1/bible"')
  })
})
