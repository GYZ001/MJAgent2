import { describe, expect, it } from 'vitest'
import type { ReferenceImage, ShotVersion } from '../api'
import { extractReferenceImagesByVersion, shotVersionSignature } from './wallReferences'

function version(overrides: Partial<ShotVersion> = {}): ShotVersion {
  return {
    id: 'v1', version_no: 1, prompt_text: '', status: 'succeeded', latency_s: 0,
    ...overrides,
  }
}

const refs: ReferenceImage[] = [{ id: 'r1', type: 'character', source: 'asset_library' }]

describe('参考图按版本摊平', () => {
  it('带参考图的版本原样摊平', () => {
    const map = extractReferenceImagesByVersion({
      versions: [version({ id: 'v1', image_inputs: { reference_images: refs } })],
    })
    expect(map).toEqual({ v1: refs })
  })

  it('详情说了「这条一张都没有」时留空数组，与「没覆盖到」区分开', () => {
    const map = extractReferenceImagesByVersion({
      versions: [
        version({ id: 'v1', image_inputs: { reference_images: [] } }),
        version({ id: 'v2', image_inputs: {} }),
      ],
    })
    expect(map).toEqual({ v1: [], v2: [] })
    expect('v1' in map).toBe(true)
  })

  it('没有 image_inputs 或被体积裁掉的版本不出现在结果里（未知，不是没有）', () => {
    const map = extractReferenceImagesByVersion({
      versions: [
        version({ id: 'v1' }),
        version({ id: 'v2', image_inputs: { reference_images: [], omitted_for_size: true } }),
      ],
    })
    expect(map).toEqual({})
  })

  it('整段还没有任何尝试时返回空表', () => {
    expect(extractReferenceImagesByVersion({ versions: [] })).toEqual({})
    expect(extractReferenceImagesByVersion({})).toEqual({})
  })
})

describe('版本指纹触发详情重取', () => {
  it('新增尝试、状态推进、供应商任务号落定都会改变指纹', () => {
    const before = shotVersionSignature({ versions: [] })
    const queued = shotVersionSignature({
      versions: [version({ id: 'v1', status: 'queued', provider_task_id: null })],
    })
    const running = shotVersionSignature({
      versions: [version({ id: 'v1', status: 'running', provider_task_id: null })],
    })
    const accepted = shotVersionSignature({
      versions: [version({ id: 'v1', status: 'running', provider_task_id: 'task_1' })],
    })
    expect(new Set([before, queued, running, accepted]).size).toBe(4)
  })

  it('只有计时字段在动时指纹不变，不把轮询放大成详情请求', () => {
    const a = shotVersionSignature({
      versions: [version({ id: 'v1', status: 'running', running_since: 100, latency_s: 1 })],
    })
    const b = shotVersionSignature({
      versions: [version({ id: 'v1', status: 'running', running_since: 100, latency_s: 42 })],
    })
    expect(a).toBe(b)
  })

  it('段不存在时返回空串，不抛错', () => {
    expect(shotVersionSignature(undefined)).toBe('')
  })
})
