import { describe, expect, it } from 'vitest'
import { aspectRatioLabel, buildAspectRatioImpactDialog } from './aspectRatioImpact'
import type { AspectRatioImpact } from '../api'

const BASE_IMPACT: AspectRatioImpact = {
  project_id: 'p1',
  current_aspect_ratio: '9:16',
  target_aspect_ratio: '16:9',
  adopted_videos_total: 12,
  adopted_videos_mismatched: 5,
  scene_images_total: 8,
  scene_images_mismatched: 3,
}

describe('aspectRatioLabel', () => {
  it('已知画幅翻成中文标签，未知值原样返回', () => {
    expect(aspectRatioLabel('9:16')).toBe('9:16（竖屏）')
    expect(aspectRatioLabel('16:9')).toBe('16:9（横屏）')
    expect(aspectRatioLabel('4:3')).toBe('4:3')
  })
})

describe('buildAspectRatioImpactDialog', () => {
  it('有统计数据时如实列出受影响的视频与场景图数量，且不掩盖会被裁切的风险', () => {
    const content = buildAspectRatioImpactDialog('16:9', BASE_IMPACT, null)
    expect(content.title).toBe('切换画幅为 16:9（横屏）？')
    expect(content.summary).toContain('5 个视频仍是旧画幅')
    expect(content.summary).toContain('等比放大裁切')
    expect(content.details[0]).toBe('场景图 3 张为旧画幅（共 8 张）')
    expect(content.details).toContain('之后新生成的按新画幅')
    expect(content.details).toContain('需要的话到生成台重新生成')
    expect(content.danger).toBe(true)
  })

  it('scene_images_mismatched 为 null 时如实写「画幅未记录」，不编造具体数字', () => {
    const content = buildAspectRatioImpactDialog('16:9', { ...BASE_IMPACT, scene_images_mismatched: null }, null)
    expect(content.details[0]).toBe('场景图 8 张，画幅未记录')
  })

  it('没有视频受影响时不升级为 danger', () => {
    const content = buildAspectRatioImpactDialog('16:9', { ...BASE_IMPACT, adopted_videos_mismatched: 0 }, null)
    expect(content.danger).toBe(false)
  })

  it('impact 接口失败时仍允许切换，如实提示暂时无法统计，而不是拦死整个操作', () => {
    const content = buildAspectRatioImpactDialog('16:9', null, 'HTTP 404')
    expect(content.summary).toBe('暂时无法统计受影响的视频数')
    expect(content.message).toContain('HTTP 404')
    expect(content.details).toContain('之后新生成的视频和场景图按新画幅')
    expect(content.danger).toBe(false)
  })

  it('impact 尚未返回（既非成功也非失败）时给出「正在核对」的中间态文案', () => {
    const content = buildAspectRatioImpactDialog('16:9', null, null)
    expect(content.summary).toContain('正在核对')
  })
})
