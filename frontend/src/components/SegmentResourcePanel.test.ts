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

  it('有定妆照时展示大图缩略图，而不是占位文案（快照 portrait_id 为空也照样出图）', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:孟浩', current_portrait_image_url: 'https://x/portrait.png', description: '' }),
      project: { refs_status: 'idle' },
    }))
    expect(html).toContain('src="https://x/portrait.png"')
    expect(html).toContain('孟浩')
    expect(html).not.toContain('定妆照待生成')
  })

  it('本轮 refs 任务正在为具名角色出图 -> "定妆照生成中"，不是"无定妆照"', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:张三', portrait_id: 'portrait_x', description: '' }),
      project: { refs_status: 'running', refs_target: null },
    }))
    expect(html).toContain('定妆照生成中')
    expect(html).not.toContain('无定妆照')
  })

  it('出图任务失败且命中这个角色 -> "定妆照生成失败" + 可点击补图入口，缺图时必须显眼', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:张三', portrait_id: 'portrait_x', description: '' }),
      project: { id: 'proj_9', refs_status: 'failed', refs_target: '张三' } as any,
    }))
    expect(html).toContain('定妆照生成失败')
    expect(html).toMatch(/<a[^>]+href="\/projects\/proj_9\/bible"[^>]*>定妆照生成失败<\/a>/)
  })

  it('既没在跑也没失败 -> "定妆照待生成"', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:张三', portrait_id: 'portrait_x', description: '' }),
      project: { refs_status: 'ready', refs_target: null },
    }))
    expect(html).toContain('定妆照待生成')
  })

  it('群演/未收录称谓（identity_id 无 bible: 前缀）恒显示"无定妆照"，不误报生成中/失败/待生成', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'entity:abcdef', portrait_id: null, description: '' }),
      project: { refs_status: 'running', refs_target: null },
    }))
    expect(html).toContain('无定妆照')
    expect(html).not.toContain('定妆照生成中')
  })

  it('群演展示后端现算的可读名，哈希 id 只留在 title 里', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({
        identity_id: 'entity:ee1fb41c79e4e33d', display_name: '虎头虎脑的少年', description: '八九岁少年',
      }),
      project: { refs_status: 'idle' },
    }))
    expect(html).toContain('虎头虎脑的少年')
    expect(html).toContain('title="entity:ee1fb41c79e4e33d"')
    expect(html).not.toMatch(/>entity:ee1fb41c79e4e33d</)
  })

  it('查不到可读名时显示中性占位，绝不把哈希当名字摆出来', () => {
    // 真实事故 2026-09-01：整排群演显示成 entity:<sha256 前16位>，对人零信息。
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'entity:ee1fb41c79e4e33d', description: '' }),
      project: { refs_status: 'idle' },
    }))
    expect(html).toContain('未具名群演')
    expect(html).not.toMatch(/>entity:ee1fb41c79e4e33d</)
  })

  it('具名角色没有 display_name 时退回冒号后的名字，不显示 bible: 前缀', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: resources({ identity_id: 'bible:孟浩', description: '' }),
      project: { refs_status: 'idle' },
    }))
    expect(html).toMatch(/>孟浩</)
    expect(html).not.toMatch(/>bible:孟浩</)
  })

  it('场景有当前场景图时展示缩略图（取后端现算字段，不查快照 id）', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: {
        characters: [], props: [],
        scenes: [{
          scene_id: 'scene:靠山宗山峦', scene_reference_id: null,
          current_scene_image_url: 'https://x/scene.png', description: '云雾缭绕',
        }],
      } as any,
      project: { scene_refs_status: 'idle' },
    }))
    expect(html).toContain('src="https://x/scene.png"')
    expect(html).toContain('云雾缭绕')
    expect(html).not.toContain('场景图待生成')
  })

  it('场景快照 id 非空但当前解析不到图 -> 占位，不得拿快照回退查图', () => {
    // 真实事故 2026-09-01 EP1 的反向守卫：快照 scene_reference_id 是溯源信息，
    // 不代表"现在有图"；当前解析不到就必须是占位，而不是显示一张旧图。
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: {
        characters: [], props: [],
        scenes: [{ scene_id: 'scene:靠山宗山峦', scene_reference_id: 'ref-1', description: '云雾缭绕' }],
      } as any,
      project: { scene_refs_status: 'ready' },
    }))
    expect(html).toContain('场景图待生成')
    expect(html).not.toContain('<img')
  })

  it('道具没有图像素材库，展示统一占位图标 + 文字描述', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: { characters: [], scenes: [], props: [{ label: '腰牌', description: '靠山宗弟子信物' }] } as any,
      project: null,
    }))
    expect(html).toContain('腰牌')
    expect(html).toContain('靠山宗弟子信物')
  })

  it('人物/场景/道具全空时展示"暂无数据"，不是空白', () => {
    const html = renderToStaticMarkup(createElement(SegmentResourcePanel, {
      resources: { characters: [], scenes: [], props: [] } as any,
      project: null,
    }))
    expect(html).toContain('暂无数据')
  })
})
