import { describe, expect, it } from 'vitest'
import type { Episode, StoryboardStatus } from './api'
import { episodeBusy } from './App'

function storyboardStatus(state: StoryboardStatus['state']): StoryboardStatus {
  return {
    contract_version: 'storyboard-workspace.v1',
    snapshot_version: 1,
    state_fingerprint: 'fp',
    state,
    screenplay_available: true,
    planned_shots: 8,
    produced_shots: 0,
    validated_shots: 0,
    final_shot_valid: false,
    hard_gates_passed: false,
    confirmed: false,
    editable: false,
    confirmable: false,
    recommended_action: state === 'running' ? 'view_progress' : 'resume_storyboard',
  }
}

function episode(state: StoryboardStatus['state']): Episode {
  return {
    id: 'e1',
    project_id: 'p1',
    episode_no: 1,
    title: '第一集',
    status: 'scripting',
    screenplay_status: 'ready',
    shots: [],
    storyboard_status: storyboardStatus(state),
  } as Episode
}

describe('分集轮询终态同步', () => {
  it('不把残留的 episode.status=scripting 当成失败任务仍存活', () => {
    expect(episodeBusy(episode('failed'))).toBe(false)
    expect(episodeBusy(episode('running'))).toBe(true)
  })

  it('全片任务已 PARTIAL 时不因残留 generating 状态持续轮询', () => {
    const partial = {
      ...episode('failed'),
      status: 'generating',
      video_supervisor: {
        run_id: 'run-partial',
        run_status: 'PARTIAL',
        outcome: 'VIDEO_PLAN_INVALID',
        task_running: false,
        active_media_jobs: 0,
      },
    } as Episode

    expect(episodeBusy(partial)).toBe(false)
  })
})
