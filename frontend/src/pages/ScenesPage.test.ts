import { describe, expect, it } from 'vitest'
import { sceneAvailability, sceneUsability } from '../lib/sceneUsability'
import { handoffGapSelectionToPayment } from './ScenesPage'

describe('场景缺口扫描弹窗交接', () => {
  it('先关闭扫描结果，再打开费用确认，避免确认窗被挡在背后', async () => {
    const events: string[] = []

    await handoffGapSelectionToPayment(
      ['萧家广场'],
      () => { events.push('close-gap') },
      async scenes => { events.push(`open-payment:${scenes.join(',')}`) },
    )

    expect(events).toEqual(['close-gap', 'open-payment:萧家广场'])
  })
})

describe('场景主图与附加视角状态', () => {
  it('反打包失败不再把可展示主图标成不可用', () => {
    const scene = {
      name: '后山小树林',
      scene_canonical: '树林',
      scene_refs: [{
        ep_start: 1,
        ep_end: null,
        image_url: '/media/forest.jpg',
        pack_status: 'failed',
        qa: { overall: 0.95, status: 'warning', hard_gate_passed: true, hard_failures: [] },
        group_qa: { status: 'failed', hard_failures: ['缺少必需视角：reverse_angle'] },
      }],
    }

    expect(sceneAvailability(scene, false)).toBe('warning')
    expect(sceneUsability(scene, false)).toBe('available')
  })

  it('主图自身硬失败时仍不可用', () => {
    const scene = {
      name: '错误场景',
      scene_canonical: '室内',
      scene_refs: [{
        ep_start: 1,
        ep_end: null,
        image_url: '/media/bad.jpg',
        pack_status: 'failed',
        qa: { overall: 0.9, status: 'failed', hard_failures: ['场景空间类型不符'] },
      }],
    }

    expect(sceneAvailability(scene, false)).toBe('failed')
    expect(sceneUsability(scene, false)).toBe('unavailable')
  })
})
