import { describe, expect, it } from 'vitest'
import type { Shot, ShotVersion } from './api'
import { compactShotStage, countAdoptedVideos, formatPipelineSummary, shotVideoState } from './shotStatus'

function version(partial: Partial<ShotVersion> & Pick<ShotVersion, 'id' | 'status'>): ShotVersion {
  return {
    version_no: 1,
    prompt_text: '',
    cost_cny: 0,
    latency_s: 0,
    ...partial,
  }
}

function shot(partial: Partial<Shot> & { versions?: ShotVersion[] }): Shot {
  return {
    id: 's1',
    episode_id: 'e1',
    shot_no: 1,
    duration_s: 5,
    shot_size: '中景',
    camera_move: '固定',
    scene_setting: '室内',
    characters: [],
    action_desc: '',
    first_frame_desc: '',
    last_frame_desc: '',
    source_excerpt: '',
    narration: null,
    dialogues: [],
    transition: '硬切',
    continuity_from_prev: 0,
    adopted_version_id: null,
    est_cost_cny: 0,
    versions: [],
    scene_status: 'idle',
    approved_scene_id: null,
    scenes: [],
    video_stale: false,
    ...partial,
  }
}

describe('shotVideoState', () => {
  it('只输出后端约定的五种主状态', () => {
    const fixtures: Array<[Partial<Shot>, string, string]> = [
      [{ video_status: 'pending_generation' }, 'pending_generation', '待生成'],
      [{ video_status: 'generating' }, 'generating', '生成中'],
      [{ video_status: 'pending_adoption' }, 'pending_adoption', '待采纳'],
      [{
        video_status: 'adopted',
        adopted_version_id: 'v1',
        versions: [version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4' })],
      }, 'adopted', '已采纳'],
      [{
        video_status: 'generation_failed',
        versions: [version({ id: 'v1', status: 'failed' })],
      }, 'generation_failed', '生成失败'],
    ]

    for (const [input, phase, label] of fixtures) {
      const state = shotVideoState(shot(input))
      expect(state.phase).toBe(phase)
      expect(state.label).toBe(label)
    }
  })

  it('可播放采纳版优先于重生成任务、video_stale 和后端竞争态', () => {
    const state = shotVideoState(shot({
      video_status: 'generating',
      adopted_version_id: 'v1',
      video_stale: true,
      pipeline: {
        video_status: 'generating',
        pipeline_status: 'running',
        candidate_count: 1,
        retake_count: 1,
      },
      versions: [
        version({ id: 'v2', status: 'running', version_no: 2 }),
        version({ id: 'v1', status: 'succeeded', video_url: '/adopted.mp4', version_no: 1 }),
      ],
    }))

    expect(state.phase).toBe('adopted')
    expect(state.label).toBe('已采纳')
    expect(state.playing?.id).toBe('v1')
  })

  it('后端状态优先于前端对版本列表的二次猜测', () => {
    const state = shotVideoState(shot({
      video_status: 'generating',
      versions: [version({ id: 'legacy', status: 'failed' })],
    }))

    expect(state.phase).toBe('generating')
    expect(state.label).toBe('生成中')
  })

  it('入队校验阻塞显示失败，自动重试显示明确阶段', () => {
    const blocked = shot({
      pipeline: {
        task_accepted: true,
        pipeline_status: 'waiting_human',
        pipeline_stage: 'preflight_blocked',
        reason_text: '视频输入校验未通过',
        candidate_count: 0,
        retake_count: 0,
      },
    })
    expect(shotVideoState(blocked).phase).toBe('generation_failed')
    expect(compactShotStage(blocked)).toBe('输入校验未通过')

    const retrying = shot({
      video_status: 'generating',
      pipeline: {
        task_accepted: true,
        pipeline_status: 'waiting',
        pipeline_stage: 'preflight_retry',
        candidate_count: 0,
        retake_count: 0,
      },
    })
    expect(shotVideoState(retrying).phase).toBe('generating')
    expect(compactShotStage(retrying)).toBe('校验失败，自动重试')
  })

  it('首帧依赖未满足前明确展示本镜尾帧预生成', () => {
    const prefetching = shot({
      video_status: 'generating',
      pipeline: {
        task_accepted: true,
        pipeline_status: 'running',
        pipeline_stage: 'reference_generate',
        reason_code: 'PREFETCHING_STATIC_TAIL',
        candidate_count: 0,
        retake_count: 0,
      },
    })

    expect(compactShotStage(prefetching)).toBe('预生成本镜静态尾帧')
  })

  it('兼容旧响应时仍按同一五态规则归并', () => {
    expect(shotVideoState(shot({})).phase).toBe('pending_generation')
    expect(shotVideoState(shot({
      versions: [version({ id: 'v1', status: 'running' })],
    })).phase).toBe('generating')
    expect(shotVideoState(shot({
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/candidate.mp4' })],
    })).phase).toBe('pending_adoption')
    expect(shotVideoState(shot({
      versions: [version({ id: 'v1', status: 'failed' })],
    })).phase).toBe('generation_failed')
  })

  it('B 级采纳版保留风险信息但主状态仍是已采纳', () => {
    const state = shotVideoState(shot({
      adopted_version_id: 'v1',
      video_grade: 'B',
      fallback_reason: 'attempt budget 用尽，技术合格兜底',
      continuity_degraded: true,
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/b.mp4' })],
    }))

    expect(state.phase).toBe('adopted')
    expect(state.label).toBe('已采纳')
    expect(state.railClass).toBe('fallback')
    expect(state.fallbackReason).toContain('兜底')
    expect(state.continuityDegraded).toBe(true)
  })
})

describe('review wall summary', () => {
  it('采纳统计包含 stale 的可播放采纳版', () => {
    const shots = [
      shot({
        id: 'a',
        adopted_version_id: 'v1',
        versions: [version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4' })],
      }),
      shot({
        id: 'b',
        adopted_version_id: 'v2',
        video_stale: true,
        versions: [version({ id: 'v2', status: 'succeeded', video_url: '/b.mp4' })],
      }),
    ]

    expect(countAdoptedVideos(shots)).toBe(2)
  })

  it('顶部汇总只展示五态', () => {
    const summary = formatPipelineSummary({
      shots_total: 5,
      adopted: 1,
      with_candidate: 2,
      upstream_generating: 1,
      preparing_references: 0,
      queued: 0,
      waiting_human: 1,
      failed: 1,
      video_status_counts: {
        pending_generation: 1,
        generating: 1,
        pending_adoption: 1,
        adopted: 1,
        generation_failed: 1,
      },
    }, 5)

    expect(summary).toBe('5 镜 · 待生成 1 · 生成中 1 · 待采纳 1 · 已采纳 1 · 生成失败 1')
  })
})
