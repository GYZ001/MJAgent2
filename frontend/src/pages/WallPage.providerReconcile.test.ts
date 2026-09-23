import { createElement } from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'
import type { ProviderTaskReconcileResult, Shot, ShotVersion, StoryboardPackSegment } from '../api'

// 生成台此前对"现有任务卡在需要人工处理"只在文案里承诺"请核对供应商任务
// 状态"，界面上没有任何入口能做这件事（CLAUDE.md「拦住用户时必须给出
// 路」）。这里验证：按钮只在 waiting_human 态出现，点击后真的调用了
// POST /episodes/{id}/provider-tasks/reconcile（frontend/src/lib/
// providerTaskRecovery.ts::reconcileProviderTasksAndReport），不是又一句
// 空文案。照抄 WallPage.render.test.ts 的渲染方式与新文件棘轮约定
// （新增测试文件 ≤500 行严格执行，不占旧文件棘轮余量）。
//
// vi.mock 工厂里要用到 mockReconcile，必须 vi.hoisted 提到文件顶部，否则撞
// TDZ（AccountAdminPage.render.test.ts 同款说明）；只桩 WallPage.tsx 与
// lib/providerTaskRecovery.ts 实际用到的 api.reconcileProviderTasks 一个方法。
const { mockReconcile } = vi.hoisted(() => ({ mockReconcile: vi.fn() }))
vi.mock('../api', () => ({ api: { reconcileProviderTasks: mockReconcile } }))

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import { GenerationPanel } from './WallPage'

const segment: StoryboardPackSegment = {
  segment_no: 5, duration_s: 15, synopsis: '', source_segment_indexes: [1],
  prompt_text: '提示词', shot_count: 1, dialogue: [],
  resources: { characters: [], scenes: [], props: [] },
  degraded_capabilities: [], beats: [], beat_ids: [], target_model: 'seedance_2',
  storyboard_version: '2.0.1',
}

function shotWithStatus(status: string): Shot {
  const version: ShotVersion = { id: 'v1', version_no: 1, prompt_text: '', status, latency_s: 5.2 }
  return {
    id: 's1', episode_id: 'ep-42', shot_no: 1, duration_s: 15, shot_size: '', camera_move: '',
    scene_time: '', scene_name: '', scene_setting: '', characters: [], action_desc: '',
    first_frame_desc: '', last_frame_desc: '', source_excerpt: '', narration: '',
    dialogues: [], transition: '', continuity_from_prev: 0, adopted_version_id: null,
    versions: [version], video_stale: false, storyboard_pack_segment: segment,
  }
}

function renderPanel(status: string, onToast: (m: string, isErr?: boolean) => void, onRefresh: () => Promise<void>) {
  let renderer!: TestRenderer.ReactTestRenderer
  act(() => {
    renderer = TestRenderer.create(createElement(GenerationPanel, {
      shot: shotWithStatus(status), context: null, referenceImages: {},
      detailLoading: false, detailError: null,
      onRefresh, onToast, goToBoard: () => undefined,
    }))
  })
  return renderer
}

function findReconcileButton(renderer: TestRenderer.ReactTestRenderer) {
  return renderer.root.findAll(
    node => node.type === 'button' && [node.props.children].flat().join('') === '核对供应商任务状态',
  )[0]
}

describe('waiting_human 态给出「核对供应商任务状态」的真实入口', () => {
  it.each(['failed', 'quarantined', 'succeeded'])('%s 态不渲染核对按钮', status => {
    const renderer = renderPanel(status, vi.fn(), async () => undefined)
    expect(findReconcileButton(renderer)).toBeUndefined()
    act(() => { renderer.unmount() })
  })

  it('waiting_human 态点击后调用真实接口、如实转述结果并刷新', async () => {
    mockReconcile.mockResolvedValue({
      episode_id: 'ep-42', blockers_before: 1,
      provider_confirmed_terminal_job_ids: ['j1'], superseded_jobs_closed_job_ids: [],
      clearance: { safe_to_clear: true, resume_supported: true, blockers: [] },
    } satisfies ProviderTaskReconcileResult)
    const onToast = vi.fn()
    const onRefresh = vi.fn(async () => undefined)
    const renderer = renderPanel('waiting_human', onToast, onRefresh)

    const button = findReconcileButton(renderer)
    expect(button, 'waiting_human 态应渲染核对供应商任务状态按钮').toBeTruthy()
    await act(async () => { button.props.onClick() })

    expect(mockReconcile).toHaveBeenCalledWith('ep-42')
    expect(onToast).toHaveBeenCalledWith('已核对：1 个任务确认供应商终态')
    expect(onRefresh).toHaveBeenCalledTimes(1)
    act(() => { renderer.unmount() })
  })
})
