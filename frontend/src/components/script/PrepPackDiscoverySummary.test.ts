import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import PrepPackDiscoverySummary, { hasDiscoveryProvenance } from './PrepPackDiscoverySummary'

// 映射台从「顺带跑的一步」变成用户拿到角色卡的唯一入口后，结果页必须让用户
// 一眼看出"这一集新建了几张卡、又复用了几个已有素材"，不必逐条悬停
// provenance 提示才知道发生了什么。判据见组件文件顶部注释：method==='discovery'
// 是后端对"当场建卡+生成定妆照"的确定性标记，其余取值都是命中已有条目。

describe('hasDiscoveryProvenance', () => {
  it('is true when at least one character or scene carries a provenance.method', () => {
    expect(hasDiscoveryProvenance(
      [{ provenance: { method: 'discovery' } } as any],
      [],
    )).toBe(true)
    expect(hasDiscoveryProvenance(
      [],
      [{ provenance: { method: 'direct' } } as any],
    )).toBe(true)
  })

  it('is false for empty lists and for items whose provenance field is entirely absent (pre-1.6.0 packs)', () => {
    expect(hasDiscoveryProvenance([], [])).toBe(false)
    expect(hasDiscoveryProvenance(
      [{ identity_id: 'bible:x', display_name: '甲' } as any],
      [{ scene_id: 'scene:y', display_name: '乙' } as any],
    )).toBe(false)
  })
})

describe('PrepPackDiscoverySummary', () => {
  it('renders nothing when no item carries provenance (cannot distinguish new from historical)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackDiscoverySummary, {
      characters: [{ identity_id: 'bible:x', display_name: '甲' } as any],
      scenes: [],
    }))
    expect(html).toBe('')
  })

  it('renders nothing when both lists are empty', () => {
    const html = renderToStaticMarkup(createElement(PrepPackDiscoverySummary, {
      characters: [], scenes: [],
    }))
    expect(html).toBe('')
  })

  it('splits characters into 新发现/索引历史 counts by provenance.method === "discovery"', () => {
    const html = renderToStaticMarkup(createElement(PrepPackDiscoverySummary, {
      characters: [
        { identity_id: 'bible:a', display_name: '甲', provenance: { method: 'discovery' } } as any,
        { identity_id: 'bible:b', display_name: '乙', provenance: { method: 'direct' } } as any,
        { identity_id: 'bible:c', display_name: '丙', provenance: { method: 'alias' } } as any,
      ],
      scenes: [],
    }))
    expect(html).toContain('人物：新发现 1 位')
    expect(html).toContain('索引历史 2 位')
    // 群演/道具不进这个摘要（它们不进人物谱身份体系，见组件注释）；这里只需
    // 确认场景分句在没有场景数据时不出现。
    expect(html).not.toContain('场景：')
  })

  it('splits scenes into 新发现/索引历史 counts independently of characters', () => {
    const html = renderToStaticMarkup(createElement(PrepPackDiscoverySummary, {
      characters: [],
      scenes: [
        { scene_id: 'scene:a', display_name: '场景甲', provenance: { method: 'discovery' } } as any,
        { scene_id: 'scene:b', display_name: '场景乙', provenance: { method: 'discovery' } } as any,
        { scene_id: 'scene:c', display_name: '场景丙', provenance: { method: 'resolution' } } as any,
      ],
    }))
    expect(html).toContain('场景：新发现 2 个')
    expect(html).toContain('索引历史 1 个')
    expect(html).not.toContain('人物：')
  })

  it('shows 新发现 0 explicitly when every item this episode was matched to an existing card (all-historical episode)', () => {
    const html = renderToStaticMarkup(createElement(PrepPackDiscoverySummary, {
      characters: [
        { identity_id: 'bible:a', display_name: '甲', provenance: { method: 'direct' } } as any,
      ],
      scenes: [],
    }))
    expect(html).toContain('新发现 0 位')
    expect(html).toContain('索引历史 1 位')
  })
})
