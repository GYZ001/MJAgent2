import { describe, expect, it } from 'vitest'
import type { Shot, ShotVersion, ReferenceImage } from '../api'
import {
  REVIEW_TABS,
  currentVersionRefs,
  classifyReferenceBuckets,
  describeShotUpdate,
  refSourceLabel,
  resolvePreviewVersionId,
  resolveStableShotSelection,
  shotDetailRefreshKey,
  shouldCommitShotDetail,
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

describe('评审对象稳定性', () => {
  it('5 镜变 4 镜时不会把已删镜头静默换成相邻镜头', () => {
    const result = resolveStableShotSelection(
      [{ id: 's1' }, { id: 's2' }, { id: 's3' }, { id: 's4' }],
      's5',
      true,
    )
    expect(result).toEqual({ selectedShotId: 's5', tombstoneShotId: 's5' })
  })

  it('只有从未保存过镜头身份时才默认首镜', () => {
    expect(resolveStableShotSelection([{ id: 's1' }], null, false))
      .toEqual({ selectedShotId: 's1', tombstoneShotId: null })
  })

  it('丢弃乱序返回的旧镜头详情', () => {
    expect(shouldCommitShotDetail(4, 5, 's1', 's2')).toBe(false)
    expect(shouldCommitShotDetail(5, 5, 's2', 's2')).toBe(true)
  })

  it('同一 shotId 的内容/版本刷新会生成可见差异摘要', () => {
    const before = { id: 's1', source_excerpt: '旧原文', action_desc: '动作', versions: [] } as unknown as Shot
    const after = { id: 's1', source_excerpt: '新原文', action_desc: '动作', versions: [version('v1', 'queued', [], 1)] } as unknown as Shot
    expect(describeShotUpdate(before, after)).toContain('镜头文字/连续性内容已更新')
    expect(describeShotUpdate(before, after)).toContain('视频版本或采用关系已更新')
  })

  it('轮询返回等价的新对象时不重复请求镜头详情', () => {
    const first = {
      id: 's1',
      adopted_version_id: null,
      video_status: 'generating',
      video_stale: false,
      versions: [version('v1', 'running', [], 1)],
    } as Shot
    const sameSnapshot = JSON.parse(JSON.stringify(first)) as Shot

    expect(shotDetailRefreshKey(sameSnapshot)).toBe(shotDetailRefreshKey(first))
  })

  it('视频状态或参考图流出时会刷新镜头详情', () => {
    const running = {
      id: 's1',
      adopted_version_id: null,
      video_status: 'generating',
      video_stale: false,
      versions: [version('v1', 'running', [], 1)],
    } as Shot
    const withReference = {
      ...running,
      versions: [version('v1', 'running', [{
        id: 'ref-1',
        type: 'character',
        source: 'asset_library',
        image_url: '/assets/ref-1.png',
        selectedForSeedance: true,
      }], 1)],
    } as Shot

    expect(shotDetailRefreshKey(withReference)).not.toBe(shotDetailRefreshKey(running))
  })
})

describe('视频预览工作区', () => {
  it('只保留视频预览入口，不再显示 A/B 视频和评审项菜单', () => {
    expect(REVIEW_TABS.map(tab => tab.label)).toEqual(['文字内容', '参考图', '视频预览'])
  })

  it('默认预览最新的可播放候选，并保留用户当前选择', () => {
    const versions = [
      { ...version('v1', 'succeeded', [], 1), video_url: '/v1.mp4' },
      version('v3', 'running', [], 3),
      { ...version('v2', 'succeeded', [], 2), video_url: '/v2.mp4' },
    ]

    expect(resolvePreviewVersionId(versions, null)).toBe('v2')
    expect(resolvePreviewVersionId(versions, 'v1')).toBe('v1')
    expect(resolvePreviewVersionId(versions, 'deleted')).toBe('v2')
  })
})
