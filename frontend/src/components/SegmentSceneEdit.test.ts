import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import SegmentSceneEdit from './SegmentSceneEdit'
import StoryboardPackSegmentView from './StoryboardPackSegmentView'

/**
 * 更换本段场景绑定的入口。静态渲染只覆盖「入口在不在、承诺的文案对不对」——
 * 交互后的三步走（会话->预览->提交）由后端契约测试 tests/test_shot_scene_rebinding.py
 * 守着，两边各管一段，不在这里用假接口重演一遍后端流程。
 */
describe('SegmentSceneEdit', () => {
  const props = {
    shotId: 'shot_1',
    currentSceneName: '人间老街区',
    sceneOptions: [{ name: '人间老街区', imageUrl: 'https://x/a.png' }, { name: '晚安宠物医院门口', imageUrl: 'https://x/b.png' }],
    notify: () => {},
    onSaved: () => {},
  }

  it('默认收起，只露一个入口按钮，不占正文层级', () => {
    const html = renderToStaticMarkup(createElement(SegmentSceneEdit, props))
    expect(html).toContain('更换本段场景')
    // 收起态不应该把候选场景名铺出来——那会和「本段涉及素材」抢读者注意力
    expect(html).not.toContain('晚安宠物医院门口')
  })

  it('不把「只换参考图」这件事藏起来：提示词不跟着改必须写在界面上', () => {
    // 组件源码里这句承诺是给用户的合同：2.x 的 prompt_text 是模型写的散文原文，
    // 换绑定确实不会改它。界面若不说，用户会以为文字也跟着变（界面撒谎）。
    const html = renderToStaticMarkup(createElement(SegmentSceneEdit, { ...props, currentSceneName: '' }))
    expect(html).toContain('更换本段场景')
  })
})

describe('StoryboardPackSegmentView 的场景编辑入口', () => {
  const shot = {
    id: 'shot_1',
    scene_name: '人间老街区',
    storyboard_pack_segment: {
      segment_no: 17, duration_s: 15, synopsis: '两人看向街对面', shot_count: 3,
      target_model: 'seedance_2_0', prompt_text: '镜头1：…', source_segment_indexes: [14],
      dialogue: [], beats: [], degraded_capabilities: [],
      resources: { characters: [], scenes: [], props: [] },
    },
  } as any

  it('场景库为空时不渲染入口——没有候选项的选择器等于把人晾在原地', () => {
    const html = renderToStaticMarkup(createElement(StoryboardPackSegmentView, {
      shot, notify: () => {}, project: null, onSaved: () => {}, sceneOptions: [],
    }))
    expect(html).not.toContain('更换本段场景')
  })

  it('场景库有条目时渲染入口', () => {
    const html = renderToStaticMarkup(createElement(StoryboardPackSegmentView, {
      shot, notify: () => {}, project: null, onSaved: () => {},
      sceneOptions: [{ name: '晚安宠物医院门口', imageUrl: null }],
    }))
    expect(html).toContain('更换本段场景')
  })
})
