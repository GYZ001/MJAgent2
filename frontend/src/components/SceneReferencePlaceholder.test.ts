import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import SceneReferencePlaceholder from './SceneReferencePlaceholder'

/**
 * 场景图占位三态的组件级验证，跟 PortraitPlaceholder.test.ts 同一套要求：failed
 * 态必须给出一条能走的路（指向场景库），不是把用户晾在一段静态文案前。
 */
describe('SceneReferencePlaceholder', () => {
  it('生成中是纯展示占位，不是链接', () => {
    const html = renderToStaticMarkup(createElement(SceneReferencePlaceholder, {
      sceneId: 'scene:老宅', label: '老宅',
      project: { scene_refs_status: 'running', id: 'p1' },
      className: 'thumb-empty',
    }))
    expect(html).toContain('场景图生成中')
    expect(html).not.toContain('<a')
  })

  it('任务既没跑也没失败时是待生成，不许显示"生成中"', () => {
    const html = renderToStaticMarkup(createElement(SceneReferencePlaceholder, {
      sceneId: 'scene:老宅', label: '老宅',
      project: { scene_refs_status: 'ready', id: 'p1' },
      className: 'thumb-empty',
    }))
    expect(html).toContain('场景图待生成')
    expect(html).not.toContain('<a')
  })

  it('生成失败态渲染一条真的指向场景库的可点击链接', () => {
    const html = renderToStaticMarkup(createElement(SceneReferencePlaceholder, {
      sceneId: 'scene:老宅', label: '老宅',
      project: { scene_refs_status: 'failed', scene_refs_target: '老宅', id: 'proj-1' },
      className: 'thumb-empty',
    }))
    expect(html).toContain('场景图生成失败')
    expect(html).toContain('<a')
    expect(html).toContain('href="/projects/proj-1/scenes"')
  })
})
