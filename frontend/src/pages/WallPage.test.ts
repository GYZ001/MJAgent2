import { describe, expect, it } from 'vitest'
import type { Shot, ShotVersion } from '../api'
import { currentVersionRefs } from './WallPage'

function version(
  id: string,
  status: string,
  refs: NonNullable<ShotVersion['image_inputs']>['reference_images'],
  versionNo: number,
): ShotVersion {
  return {
    id,
    version_no: versionNo,
    prompt_text: '',
    status,
    cost_cny: 0,
    latency_s: 0,
    image_inputs: { reference_images: refs },
  }
}

describe('currentVersionRefs', () => {
  it('优先展示运行中新版本已流出的参考图，而不是旧采用版', () => {
    const liveRef = {
      id: 'live-ref',
      type: 'plot_key_frame',
      source: 'seedream_generated',
      selectedForSeedance: true,
    }
    const adoptedRef = {
      id: 'old-ref',
      type: 'plot_key_frame',
      source: 'seedream_generated',
      selectedForSeedance: true,
    }
    const shot = {
      adopted_version_id: 'v1',
      versions: [
        version('v2', 'running', [liveRef], 2),
        version('v1', 'succeeded', [adoptedRef], 1),
      ],
    } as Shot

    const result = currentVersionRefs(shot)

    expect(result?.versionId).toBe('v2')
    expect(result?.refs.map(ref => ref.id)).toEqual(['live-ref'])
    expect(result?.isFallback).toBe(false)
  })
})
