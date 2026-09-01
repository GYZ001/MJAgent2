import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import SegmentResourcePanel from './SegmentResourcePanel'

/**
 * 分镜台/生成台共用素材面板（用户拍板，2026-08-31，「传入素材」展示重做）：原来
 * BoardPage.test.ts::StoryboardPackResourceRoster / WallPage.test.ts::
 * SegmentResourceRoster 两份几乎相同的测试（因为两页曾各写一份同逻辑组件）随
 * 组件合并搬到这里，不再重复维护两份。
 */
describe('SegmentResourcePanel', () => {
  const resources = (character: Record<string, unknown>) => ({
    characters: [character], scenes: [], props: [],
  }) as any

  it('有定妆照时展示大图缩略图，而不是占位文案', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:孟浩', current_portrait_image_url: 'https://x/portrait.png', description: '' }),
      bible: null,
      project: { refs_status: 'idle' },
    }))
    expect(html).toContain('src="https://x/portrait.png"')
    expect(html).toContain('孟浩')
    expect(html).not.toContain('定妆照待生成')
  })

  it('本轮 refs 任务正在为具名角色出图 -> "定妆照生成中"，不是"无定妆照"', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:张三', portrait_id: 'portrait_x', description: '' }),
      bible: null,
      project: { refs_status: 'running', refs_target: null },
    }))
    expect(html).toContain('定妆照生成中')
    expect(html).not.toContain('无定妆照')
  })

  it('出图任务失败且命中这个角色 -> "定妆照生成失败" + 可点击补图入口，缺图时必须显眼', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:张三', portrait_id: 'portrait_x', description: '' }),
      bible: null,
      project: { id: 'proj_9', refs_status: 'failed', refs_target: '张三' } as any,
    }))
    expect(html).toContain('定妆照生成失败')
    expect(html).toMatch(/<a[^>]+href="\/projects\/proj_9\/bible"[^>]*>定妆照生成失败<\/a>/)
  })

  it('既没在跑也没失败 -> "定妆照待生成"', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:张三', portrait_id: 'portrait_x', description: '' }),
      bible: null,
      project: { refs_status: 'ready', refs_target: null },
    }))
    expect(html).toContain('定妆照待生成')
  })

  it('群演/未收录称谓（identity_id 无 bible: 前缀）恒显示"无定妆照"，不误报生成中/失败/待生成', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'entity:abcdef', portrait_id: null, description: '' }),
      bible: null,
      project: { refs_status: 'running', refs_target: null },
    }))
    expect(html).toContain('无定妆照')
    expect(html).not.toContain('定妆照生成中')
  })

  it('场景有参考图时展示缩略图，按 scene_reference_id 在世界书场景库里查', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: {
        characters: [], props: [],
        scenes: [{ scene_id: 'scene:靠山宗山峦', scene_reference_id: 'ref-1', description: '云雾缭绕' }],
      } as any,
      bible: {
        scenes: [{ scene_refs: [{ id: 'ref-1', image_url: 'https://x/scene.png' }] }],
      } as any,
      project: { scene_refs_status: 'idle' },
    }))
    expect(html).toContain('src="https://x/scene.png"')
    expect(html).toContain('云雾缭绕')
  })

  it('道具没有图像素材库，展示统一占位图标 + 文字描述', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: { characters: [], scenes: [], props: [{ label: '腰牌', description: '靠山宗弟子信物' }] } as any,
      bible: null,
      project: null,
    }))
    expect(html).toContain('腰牌')
    expect(html).toContain('靠山宗弟子信物')
  })

  it('人物/场景/道具全空时展示"暂无数据"，不是空白', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: { characters: [], scenes: [], props: [] } as any,
      bible: null,
      project: null,
    }))
    expect(html).toContain('暂无数据')
  })
})
