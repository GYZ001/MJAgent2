import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ReferenceImage } from '../api'

import GenerationReferenceGallery from './GenerationReferenceGallery'

/**
 * 生成台专用（用户拍板，2026-08-31，「传入素材」展示重做）：这一次生成实际发给
 * 供应商的参考图，与 SegmentResourcePanel.test.ts 覆盖的「本段声明涉及哪些实体」
 * 是两件不同的事——这里只测「这次真的用了什么」以及「该有却没有」的显眼提示。
 */
describe('GenerationReferenceGallery', () => {
  const ref = (overrides: Partial<ReferenceImage> = {}): ReferenceImage => ({
    id: 'r1', type: 'character', source: 'asset_library', entity_name: '孟浩', image_url: 'https://x/ref.png',
    ...overrides,
  })

  it('有参考图正常展示：大图 + 带身份的标签', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [ref()], loading: false, hasAttempt: true, hasDeclaredResources: true,
    }))
    expect(html).toContain('src="https://x/ref.png"')
    expect(html).toContain('人物 · 孟浩')
  })

  it('参考图详情正在加载时提示加载中，不是空白也不是缺失告警', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: true, hasAttempt: true, hasDeclaredResources: true,
    }))
    expect(html).toContain('正在加载参考图')
    expect(html).not.toContain('参考图缺失')
  })

  it('该有却没有必须显眼：已提交过生成、本段声明了素材，但这次一张参考图都没带 -> 红色告警', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: false, hasAttempt: true, hasDeclaredResources: true,
    }))
    expect(html).toContain('参考图缺失')
    expect(html).toMatch(/role="alert"/)
  })

  it('尚未提交过生成时不误报缺失（还没到该有参考图的时候）', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: false, hasAttempt: false, hasDeclaredResources: true,
    }))
    expect(html).not.toContain('参考图缺失')
  })

  it('本段本来就没有声明人物/场景素材时不误报缺失', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: false, hasAttempt: true, hasDeclaredResources: false,
    }))
    expect(html).not.toContain('参考图缺失')
    expect(html).toContain('本次生成未使用参考图')
  })

  it('参考图记录没有图片地址时展示"无图"占位，不是破图', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [ref({ image_url: null })], loading: false, hasAttempt: true, hasDeclaredResources: true,
    }))
    expect(html).toContain('无图')
    expect(html).not.toContain('<img')
  })
})
