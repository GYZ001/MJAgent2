import { describe, expect, it } from 'vitest'
import type { Shot, ShotVersion } from './api'
import { countAdoptedVideos, shotVideoState } from './shotStatus'

function version( partial: Partial<ShotVersion> & Pick<ShotVersion, 'id' | 'status'>): ShotVersion {
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
  it('空镜头为待生成', () => {
    expect(shotVideoState(shot({})).phase).toBe('empty')
    expect(shotVideoState(shot({})).label).toBe('待生成')
  })

  it('queued/running 优先于已采用与失败', () => {
    const s = shot({
      adopted_version_id: 'v1',
      versions: [
        version({ id: 'v2', status: 'running' }),
        version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4', version_no: 1 }),
      ],
    })
    expect(shotVideoState(s).phase).toBe('working')
    expect(shotVideoState(s).railClass).toBe('working')
  })

  it('存在成功版但未采用 → 待采用，而不是已完成', () => {
    const s = shot({
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4' })],
    })
    const state = shotVideoState(s)
    expect(state.phase).toBe('ready')
    expect(state.label).toBe('待采用')
    expect(state.railClass).toBe('ready')
  })

  it('已采用且未过期 → 已采用', () => {
    const s = shot({
      adopted_version_id: 'v1',
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4' })],
    })
    expect(shotVideoState(s).phase).toBe('adopted')
  })

  it('已采用但 video_stale → 需重生', () => {
    const s = shot({
      adopted_version_id: 'v1',
      video_stale: true,
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4' })],
    })
    expect(shotVideoState(s).phase).toBe('stale')
    expect(shotVideoState(s).railClass).toBe('failed')
  })

  it('最新失败且无成功版 → 生成失败', () => {
    const s = shot({
      versions: [version({ id: 'v1', status: 'failed', error: 'timeout' })],
    })
    expect(shotVideoState(s).phase).toBe('failed')
  })

  it('仅有 succeeded 但无 video_url 不算 ready', () => {
    const s = shot({
      versions: [version({ id: 'v1', status: 'succeeded' })],
    })
    expect(shotVideoState(s).phase).toBe('empty')
  })

  it('采用版失败时不算 adopted，回退到其它成功版', () => {
    const s = shot({
      adopted_version_id: 'v-bad',
      versions: [
        version({ id: 'v2', status: 'succeeded', video_url: '/ok.mp4', version_no: 2 }),
        version({ id: 'v-bad', status: 'failed', version_no: 1 }),
      ],
    })
    expect(shotVideoState(s).phase).toBe('ready')
    expect(shotVideoState(s).playing?.id).toBe('v2')
  })

  it('B 级采用 → fallback rail 与兜底原因', () => {
    const s = shot({
      adopted_version_id: 'v1',
      video_grade: 'B',
      fallback_reason: 'attempt budget 用尽，技术合格兜底',
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/b.mp4' })],
    })
    const state = shotVideoState(s)
    expect(state.grade).toBe('B')
    expect(state.railClass).toBe('fallback')
    expect(state.fallbackReason).toContain('兜底')
  })

  it('衔接降级标记透出', () => {
    const s = shot({
      adopted_version_id: 'v1',
      video_grade: 'B',
      continuity_degraded: true,
      versions: [version({ id: 'v1', status: 'succeeded', video_url: '/d.mp4' })],
    })
    expect(shotVideoState(s).continuityDegraded).toBe(true)
  })
})

describe('countAdoptedVideos', () => {
  it('只统计已采用且未过期', () => {
    const shots = [
      shot({
        id: 'a',
        adopted_version_id: 'v1',
        versions: [version({ id: 'v1', status: 'succeeded', video_url: '/a.mp4' })],
      }),
      shot({
        id: 'b',
        versions: [version({ id: 'v2', status: 'succeeded', video_url: '/b.mp4' })],
      }),
      shot({
        id: 'c',
        adopted_version_id: 'v3',
        video_stale: true,
        versions: [version({ id: 'v3', status: 'succeeded', video_url: '/c.mp4' })],
      }),
    ]
    expect(countAdoptedVideos(shots)).toBe(1)
  })
})
