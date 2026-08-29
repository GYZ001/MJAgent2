import { describe, expect, it } from 'vitest'
import type { Shot, ShotVersion } from './api'
import { compactShotStage } from './shotStatus'

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

describe('compactShotStage', () => {
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
    expect(compactShotStage(retrying)).toBe('校验失败，自动重试')
  })

  it('依赖镜预算暂停优先展示恢复后的等待关系', () => {
    const paused = shot({
      video_status: 'generating',
      mode_plan: {
        mode: 'FIRST_LAST_FRAME_MODE',
        confidence: 1,
        depends_on_shot_id: 'upstream-shot',
      },
      pipeline: {
        task_accepted: true,
        pipeline_status: 'paused_budget',
        pipeline_stage: 'job_queued',
        candidate_count: 0,
        retake_count: 0,
      },
    })

    expect(compactShotStage(paused))
      .toBe('预算暂停，恢复后等待上一镜素材')
  })

  it('静态尾帧资产等待不显示成等待采用视频', () => {
    const waiting = shot({
      video_status: 'generating',
      pipeline: {
        task_accepted: true,
        pipeline_status: 'waiting',
        pipeline_stage: 'waiting_dependency',
        reason_code: 'WAITING_STATIC_BOUNDARY_ASSET',
        candidate_count: 0,
        retake_count: 0,
      },
    })

    expect(compactShotStage(waiting)).toBe('等待上一镜静态尾帧')
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
})
