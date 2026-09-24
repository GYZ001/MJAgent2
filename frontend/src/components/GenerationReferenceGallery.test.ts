import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ReferenceAudioInput, ReferenceImage } from '../api'

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
      refs: [ref()], loading: false, hasAttempt: true, declaredResources: { characters: [{ identity_id: '孟浩' }], scenes: [], props: [] },
    }))
    expect(html).toContain('src="https://x/ref.png"')
    expect(html).toContain('人物 · 孟浩')
  })

  it('参考图详情正在加载时提示加载中，不是空白也不是缺失告警', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: true, hasAttempt: true, declaredResources: { characters: [{ identity_id: '孟浩' }], scenes: [], props: [] },
    }))
    expect(html).toContain('正在加载参考图')
    expect(html).not.toContain('参考图缺失')
  })

  it('该有却没有必须显眼：已提交过生成、本段声明了素材，但这次一张参考图都没带 -> 红色告警', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: false, hasAttempt: true, declaredResources: { characters: [{ identity_id: '孟浩' }], scenes: [], props: [] },
    }))
    expect(html).toContain('参考图缺失')
    expect(html).toMatch(/role="alert"/)
  })

  it('尚未提交过生成时不误报缺失（还没到该有参考图的时候）', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: false, hasAttempt: false, declaredResources: { characters: [{ identity_id: '孟浩' }], scenes: [], props: [] },
    }))
    expect(html).not.toContain('参考图缺失')
  })

  it('本段本来就没有声明人物/场景素材时不误报缺失', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [], loading: false, hasAttempt: true, declaredResources: { characters: [], scenes: [], props: [] },
    }))
    expect(html).not.toContain('参考图缺失')
    expect(html).toContain('本次生成未使用参考图')
  })

  it('参考图记录没有图片地址时展示"无图"占位，不是破图', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      refs: [ref({ image_url: null })], loading: false, hasAttempt: true, declaredResources: { characters: [{ identity_id: '孟浩' }], scenes: [], props: [] },
    }))
    expect(html).toContain('无图')
    expect(html).not.toContain('<img')
  })
})


it('纯声音与独立群演不被误报缺少参考图', () => {
  const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
    refs: [], loading: false, hasAttempt: true,
    declaredResources: { characters: [
      { identity_id: '孟浩', visibility: 'voice_only', subject_kind: 'character' },
      { identity_id: '同门', visibility: 'visible', subject_kind: 'extra' },
    ], scenes: [], props: [] },
  }))
  expect(html).not.toContain('参考图缺失')
  expect(html).toContain('本次生成未使用参考图')
})

/**
 * 参考音频（U4b，2026-09-24）：另一个代理正在做「生成视频时把本段说话角色的
 * 参考音频一起传给视频模型」，这里只测展示层——数据形状按派单里冻结的后端
 * 约定手写 mock（image_inputs.reference_audios/reference_audio_skips），不等
 * 后端真的接上。四种情况：有音频/有跳过/两者都没有/字段整体缺失（旧数据）。
 */
describe('GenerationReferenceGallery 参考音频', () => {
  const audio = (overrides: Partial<ReferenceAudioInput> = {}): ReferenceAudioInput => ({
    index: 1, character_name: '孟浩', voice_id: 'voice_1', clip_url: 'https://x/clip.mp3', clip_duration_s: 3.2,
    ...overrides,
  })
  const baseProps = { refs: [], loading: false, hasAttempt: true, declaredResources: { characters: [], scenes: [], props: [] } } as const

  it('有参考音频：展示"音频N · 角色名"与可播放按钮，时长取自 clip_duration_s', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      ...baseProps, audios: [audio()], versionId: 'v1',
    }))
    expect(html).toContain('参考音频')
    expect(html).toContain('音频1 · 孟浩')
    expect(html).toContain('播放孟浩的参考声音')
    expect(html).toContain('3.2 秒')
  })

  it('有跳过声音的角色：展示"未传声音"一行及中文原因', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      ...baseProps, audioSkips: [{ character_name: '小李', reason: '未配置声音' }],
    }))
    expect(html).toContain('未传声音：小李（未配置声音）')
  })

  it('音频与跳过都没有（都是空数组）：不渲染"参考音频"标题，不冒出空标题', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      ...baseProps, audios: [], audioSkips: [],
    }))
    expect(html).not.toContain('参考音频')
  })

  it('audios/audioSkips 整体缺失（旧数据/老版本）：不报错也不渲染"参考音频"', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, baseProps))
    expect(html).not.toContain('参考音频')
  })

  it('部分角色有声音、部分被跳过：两块同时出现，互不吞没', () => {
    const html = renderToStaticMarkup(createElement(GenerationReferenceGallery, {
      ...baseProps,
      audios: [audio({ index: 1, character_name: '孟浩' })],
      audioSkips: [{ character_name: '同门', reason: '超出每段 3 个上限' }],
    }))
    expect(html).toContain('音频1 · 孟浩')
    expect(html).toContain('未传声音：同门（超出每段 3 个上限）')
  })
})
