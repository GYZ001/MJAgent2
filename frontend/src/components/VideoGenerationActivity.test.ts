import { describe, expect, it } from 'vitest'
import type { Shot } from '../api'
import { compactShotStage, summarizeVideoActivity } from './VideoGenerationActivity'

function shot(no: number, pipeline: Shot['pipeline']): Shot {
  return {
    id: `s${no}`,
    episode_id: 'e1',
    shot_no: no,
    duration_s: 5,
    shot_size: '全景',
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
    est_cost_cny: 1,
    versions: [],
    video_stale: false,
    pipeline,
  }
}

describe('视频任务活动摘要', () => {
  it('区分系统已受理和供应商已接单', () => {
    const queued = shot(1, {
      task_accepted: true,
      task_id: 'job-1',
      task_created_at: 100,
      provider_submitted: false,
      pipeline_status: 'queued',
      pipeline_stage: 'reference_generate',
      stage_progress: { current: 2, total: 4 },
      candidate_count: 0,
      retake_count: 0,
    })
    const provider = shot(2, {
      task_accepted: true,
      task_id: 'job-2',
      task_created_at: 110,
      provider_submitted: true,
      pipeline_status: 'waiting_provider',
      pipeline_stage: 'video_generating',
      candidate_count: 0,
      retake_count: 0,
    })
    const downloading = shot(3, {
      task_accepted: true,
      task_id: 'job-3',
      task_created_at: 120,
      provider_submitted: true,
      pipeline_status: 'running',
      pipeline_stage: 'video_downloading',
      candidate_count: 0,
      retake_count: 0,
    })

    const result = summarizeVideoActivity([queued, provider, downloading])

    expect(result.activeCount).toBe(3)
    expect(result.taskAcceptedCount).toBe(3)
    expect(result.preparingCount).toBe(1)
    expect(result.providerSubmittedCount).toBe(2)
    expect(result.providerCount).toBe(1)
    expect(result.postCount).toBe(1)
    expect(result.startedAt).toBe(100)
    expect(compactShotStage(queued)).toBe('候选图 2/4')
    expect(compactShotStage(provider)).toBe('供应商生成中')
  })

  it('不把没有活动任务的候选版算作生成中', () => {
    const idle = shot(1, {
      task_accepted: false,
      pipeline_status: 'waiting_human',
      pipeline_stage: 'candidate_ready',
      candidate_count: 1,
      retake_count: 0,
    })

    expect(summarizeVideoActivity([idle]).activeCount).toBe(0)
  })
})
