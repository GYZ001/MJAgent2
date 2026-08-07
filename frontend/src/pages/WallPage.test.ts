import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Shot, ShotVersion, ReferenceImage } from '../api'
import {
  EPISODE_COMPLETION_BUDGET_CAP_CNY,
  EPISODE_COMPLETION_WALL_CLOCK_CAP_S,
  InfoSection,
  MaterialGallery,
  REVIEW_TABS,
  boundarySourceLabel,
  currentMaterialVersion,
  currentVersionRefs,
  classifyReferenceBuckets,
  countReferenceImages,
  describeShotUpdate,
  episodeCompletionRequest,
  episodeGenerationAction,
  isVideoModelInputRejection,
  refSourceLabel,
  referenceLibraryLabel,
  reviewWallPositionKey,
  resolvePreviewVersionId,
  resolveStableShotSelection,
  reviewContextRefreshKey,
  shotDetailRefreshKey,
  shotHasActiveGeneration,
  shotHasPausedGeneration,
  shotMaterialLibraryKind,
  shouldCommitShotDetail,
  shouldPersistReviewWallPosition,
  videoCandidateNote,
  videoPlaybackRate,
  videoGenerationConfirmLabel,
  videoModeReasonText,
  visibleVideoVersions,
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

describe('生成台三类分组与视角标签', () => {
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

describe('模式化素材库', () => {
  const renderLibrary = (shot: Shot) => renderToStaticMarkup(createElement(
    MaterialGallery,
    {
      shot,
      productionEligible: true,
      onOpen: () => undefined,
      onRefresh: async () => undefined,
      onToast: () => undefined,
    },
  ))

  const materialShot = (
    mode: NonNullable<Shot['mode_plan']>['mode'],
    imageInputs: NonNullable<ShotVersion['image_inputs']>,
  ) => ({
    id: `shot-${mode}`,
    shot_no: 1,
    mode_plan: { mode, confidence: 1 },
    adopted_version_id: null,
    versions: [{
      ...version('v1', 'succeeded', imageInputs.reference_images ?? [], 1),
      image_inputs: imageInputs,
    }],
  }) as unknown as Shot

  it('按计划模式选择唯一素材类型', () => {
    expect(shotMaterialLibraryKind(materialShot(
      'FIRST_LAST_FRAME_MODE', {},
    ))).toBe('keyframes')
    expect(shotMaterialLibraryKind(materialShot(
      'REFERENCE_IMAGE_MODE', {},
    ))).toBe('references')
    expect(shotMaterialLibraryKind(materialShot(
      'VIDEO_INPUT_MODE', {},
    ))).toBe('video')
  })

  it('首尾帧模式只展示关键帧', () => {
    const shot = materialShot('FIRST_LAST_FRAME_MODE', {
      mode: 'FIRST_LAST_FRAME_MODE',
      first_frame_image_url: '/media/first.jpg',
      first_frame_source: 'PREVIOUS_STATIC_TAIL',
      last_frame_image_url: '/media/last.jpg',
      last_frame_source: 'STATIC_BOUNDARY_ASSET',
    })
    const html = renderLibrary(shot)

    expect(html).toContain('本镜关键帧')
    expect(html).toContain('首帧')
    expect(html).toContain('尾帧')
    expect(html).toContain('上一镜静态尾帧')
    expect(html).not.toContain('实际提交参考图')
    expect(html).not.toContain('<video')
    expect(currentMaterialVersion(shot)?.version.id).toBe('v1')
  })

  it('参考图模式只展示参考图', () => {
    const shot = materialShot('REFERENCE_IMAGE_MODE', {
      mode: 'REFERENCE_IMAGE_MODE',
      reference_images: [{
        id: 'ref',
        type: 'scene',
        source: 'asset_library',
        image_url: '/media/ref.jpg',
        selectedForSeedance: true,
      }],
    })
    const html = renderLibrary(shot)

    expect(html).toContain('本镜参考图')
    expect(html).toContain('实际提交参考图')
    expect(html).toContain('场景参考')
    expect(html).not.toContain('关键帧 ·')
    expect(html).not.toContain('<video')
  })

  it('视频参考模式只展示视频输入', () => {
    const shot = materialShot('VIDEO_INPUT_MODE', {
      mode: 'VIDEO_INPUT_MODE',
      video_input_url: '/media/upstream.mp4',
      video_input_source_revision_id: 'upstream-v1',
    })
    const html = renderLibrary(shot)

    expect(html).toContain('本镜视频输入')
    expect(html).toContain('<video')
    expect(html).toContain('/media/upstream.mp4')
    expect(html).not.toContain('实际提交参考图')
    expect(html).not.toContain('关键帧 ·')
    expect(boundarySourceLabel('PREVIOUS_ADOPTED_TAIL'))
      .toBe('上一镜真实视频尾帧')
    expect(referenceLibraryLabel({
      id: 'plot',
      type: 'plot_key_frame',
      source: 'seedream_generated',
    })).toBe('剧情参考图')
  })
})

describe('生成台对象稳定性', () => {
  it('视频模型拒绝输入时展示换模型提示，不视为自动降级', () => {
    expect(isVideoModelInputRejection(
      '当前模型拒绝（VIDEO_INPUT_PRIVACY_REJECTED · ERR-1）',
      null,
    )).toBe(true)
    expect(isVideoModelInputRejection(
      '普通网络错误',
      'VIDEO_INPUT_PRIVACY_REJECTED',
    )).toBe(true)
    expect(isVideoModelInputRejection('普通网络错误', null)).toBe(false)
  })

  it('历史模式计划缺少 reason_codes 时使用兼容文案', () => {
    expect(videoModeReasonText(undefined)).toBe('由整集关系计划生成')
    expect(videoModeReasonText(['FIRST_SHOT_NO_PREDECESSOR']))
      .toBe('FIRST_SHOT_NO_PREDECESSOR')
  })

  it('历史模式计划可完整渲染文字内容，不因缺少新字段崩溃', () => {
    const shot = {
      id: 's1',
      shot_no: 1,
      mode_plan: {
        mode: 'REFERENCE_IMAGE_MODE',
        confidence: 1,
      },
      dialogues: [],
      characters: [],
      versions: [],
      duration_s: 5,
      shot_size: '远景',
      camera_move: '固定',
      scene_setting: '广场',
      transition: '硬切',
      continuity_from_prev: 0,
      action_desc: '角色走入画面。',
      source_excerpt: '',
    } as unknown as Shot

    const html = renderToStaticMarkup(createElement(InfoSection, { shot }))

    expect(html).toContain('由整集关系计划生成')
    expect(html).toContain('参考图')
  })

  it('上游确认或运行指针收口时会重新加载生成资格', () => {
    const running = reviewContextRefreshKey({
      status: 'scripting',
      storyboard_artifact_id: 'board-1',
      active_storyboard_run_id: 'run-1',
    })
    const confirmed = reviewContextRefreshKey({
      status: 'confirmed',
      storyboard_artifact_id: 'board-2',
      active_storyboard_run_id: null,
    })
    expect(confirmed).not.toBe(running)
  })

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

  it('不同分镜制品使用独立位置键，重生成后不会读取旧镜头身份', () => {
    expect(reviewWallPositionKey('p1', 'e1', 'board-v1'))
      .toBe('manju:review-wall:p1:e1:board-v1')
    expect(reviewWallPositionKey('p1', 'e1', 'board-v2'))
      .toBe('manju:review-wall:p1:e1:board-v2')
    expect(shouldPersistReviewWallPosition(
      'manju:review-wall:p1:e1:board-v1',
      'manju:review-wall:p1:e1:board-v2',
      'old-shot',
    )).toBe(false)
    expect(shouldPersistReviewWallPosition(
      'manju:review-wall:p1:e1:board-v2',
      'manju:review-wall:p1:e1:board-v2',
      'new-shot',
    )).toBe(true)
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
  it('每个候选读取独立的定稿倍速，并对异常历史值回退为 1×', () => {
    expect(videoPlaybackRate({ playback_rate: 1.5 })).toBe(1.5)
    expect(videoPlaybackRate({ playback_rate: null })).toBe(1)
    expect(videoPlaybackRate({ playback_rate: 9 })).toBe(1)
  })

  it('只保留生成与预览相关页签', () => {
    expect(REVIEW_TABS.map(tab => tab.label)).toEqual(['文字内容', '素材库', '视频预览'])
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

  it('参考图承载记录不会进入视频候选列表', () => {
    const versions = [
      version('refs', 'references_ready', [], 3),
      version('failed', 'failed', [], 2),
      version('ready', 'succeeded', [], 1),
    ]
    expect(visibleVideoVersions(versions).map(item => item.id)).toEqual(['failed', 'ready'])
  })

  it('只有图像没有视频时仍能识别可清空资源', () => {
    const versions = [
      version('refs', 'references_ready', [
        { id: 'character', type: 'character', source: 'asset_library' },
        { id: 'keyframe', type: 'plot_key_frame', source: 'seedream_generated' },
      ], 1),
    ]
    expect(visibleVideoVersions(versions)).toEqual([])
    expect(countReferenceImages(versions)).toBe(2)
  })

  it('失败候选只在列表显示可操作摘要，原始错误留给详情区', () => {
    const failed = {
      ...version('failed', 'failed', [], 2),
      error: 'provider_internal_stack_trace',
    }
    expect(videoCandidateNote(failed)).toBe('生成未完成，点击查看错误详情')
    expect(videoCandidateNote({ ...version('ready', 'succeeded', [], 1), video_url: '/v1.mp4' }))
      .toBe('点击卡片预览此候选')
  })

  it('付费提交按钮直接说明动作与预计费用，不暗示还有下一步确认', () => {
    expect(videoGenerationConfirmLabel('reroll', 4)).toBe('确认新建候选 · 预计 ￥4.00')
    expect(videoGenerationConfirmLabel('rewrite', 4)).toBe('确认使用新词生成 · 预计 ￥4.00')
    expect(videoGenerationConfirmLabel('critique', 4)).toBe('确认按质检问题修复 · 预计 ￥4.00')
  })
})

describe('整集生成按钮状态', () => {
  it('新建整集任务走可补齐资产的 Supervisor，并绑定当前资格版本', () => {
    expect(episodeCompletionRequest('qualification-v3')).toEqual({
      mode: 'fresh',
      budget_cap_cny: EPISODE_COMPLETION_BUDGET_CAP_CNY,
      wall_clock_cap_s: EPISODE_COMPLETION_WALL_CLOCK_CAP_S,
      allow_fallback_adopt: true,
      allow_storyboard_edit: false,
      qualification_version: 'qualification-v3',
    })
    expect(EPISODE_COMPLETION_BUDGET_CAP_CNY).toBe(150)
    expect(EPISODE_COMPLETION_WALL_CLOCK_CAP_S).toBe(4 * 60 * 60)
  })

  it('运行时停止，暂停或失败停止后继续，其余情况生成', () => {
    expect(episodeGenerationAction(true, 0, 0)).toBe('stop')
    expect(episodeGenerationAction(false, 2, 0)).toBe('resume')
    expect(episodeGenerationAction(false, 0, 1)).toBe('resume')
    expect(episodeGenerationAction(false, 0, 0)).toBe('generate')
  })

  it('预算暂停镜头不冒充活动任务', () => {
    const paused = {
      video_status: 'generating',
      pipeline: { pipeline_status: 'paused_budget' },
      versions: [],
    } as unknown as Shot
    const running = {
      video_status: 'generating',
      pipeline: { pipeline_status: 'waiting_provider' },
      versions: [],
    } as unknown as Shot

    expect(shotHasActiveGeneration(paused)).toBe(false)
    expect(shotHasPausedGeneration(paused)).toBe(true)
    expect(shotHasActiveGeneration(running)).toBe(true)
    expect(shotHasPausedGeneration(running)).toBe(false)
  })
})
