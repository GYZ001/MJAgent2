import { describe, expect, it } from 'vitest'
import type { Shot, ShotVersion, ReferenceImage } from '../api'
import {
  currentVersionRefs,
  classifyReferenceBuckets,
  refSourceLabel,
} from './WallPage'

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

describe('评审墙三类分组与视角标签', () => {
  it('按用途分为视频实际输入 / QA 依据 / 废弃候选', () => {
    const refs: ReferenceImage[] = [
      {
        id: 'kf',
        type: 'plot_key_frame',
        source: 'seedream_generated',
        selectedForSeedance: true,
        slot_key: 'narrative_keyframe',
        purposes: ['video_input', 'qa_anchor'],
      },
      {
        id: 'char-profile',
        type: 'character',
        source: 'asset_library',
        selectedForSeedance: false,
        entity_type: 'character',
        view_role: 'profile',
        purposes: ['keyframe_seed', 'qa_anchor'],
      },
      {
        id: 'scene-rev',
        type: 'scene',
        source: 'asset_library',
        selectedForSeedance: false,
        entity_type: 'scene',
        view_role: 'reverse_angle',
        purposes: ['qa_anchor'],
      },
      {
        id: 'bad',
        type: 'plot_key_frame',
        source: 'seedream_generated',
        selectedForSeedance: false,
        rejectReason: 'quality_below_threshold',
        purposes: ['video_input'],
      },
    ]

    const buckets = classifyReferenceBuckets(refs)
    expect(buckets.video.map(r => r.id)).toEqual(['kf'])
    expect(buckets.evidence.map(r => r.id)).toEqual(['char-profile', 'scene-rev'])
    expect(buckets.discarded.map(r => r.id)).toEqual(['bad'])
  })

  it('关键帧与多视角显示专用标签', () => {
    expect(refSourceLabel({
      id: 'kf',
      type: 'plot_key_frame',
      source: 'seedream_generated',
      slot_key: 'narrative_keyframe',
    })).toBe('关键帧')
    expect(refSourceLabel({
      id: 'c',
      type: 'character',
      source: 'asset_library',
      entity_type: 'character',
      view_role: 'profile',
    })).toBe('人物参考 · 侧面')
    expect(refSourceLabel({
      id: 's',
      type: 'scene',
      source: 'asset_library',
      entity_type: 'scene',
      view_role: 'reverse_angle',
    })).toBe('场景参考 · 反打')
  })
})
