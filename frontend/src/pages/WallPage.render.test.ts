import { createElement } from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { describe, expect, it, vi } from 'vitest'
import type { Shot, ShotVersion, StoryboardPackSegment } from '../api'
import { GenerationPanel } from './WallPage'

// WallPage.test.ts 已经卡在 FILE_CONVENTIONS.toml 的 line_count 棘轮基线（291 行，
// 精确实测值、零缓冲），装不下这条需要 react-test-renderer 才能验证「点击」的交互
// 测试。照抄本仓库既有先例（ScriptPage.test.ts 只测纯函数，交互/渲染测试单独开
// ScriptPage.render.test.ts）：纯函数留在原文件，真实渲染 + 点击验证放这个新文件，
// 从零起按「新增测试文件 ≤500 行」严格执行，不占用旧文件的棘轮余量。
//
// GenerationPanel 不自己调用 useNav()——它接收 goToBoard 回调，由 WallPage 顶层用
// useNav().go('board', projectId, episodeId) 绑定后传入，所以这里既不需要 mock
// '../App' 或伪造 NavCtx，也不需要 ScriptPage.render.test.ts 那样的 window/document
// 宿主存根（GenerationPanel 及其子树渲染期都不摸宿主 API）。

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
    id: 's1', episode_id: 'e1', shot_no: 1, duration_s: 15, shot_size: '', camera_move: '',
    scene_time: '', scene_name: '', scene_setting: '', characters: [], action_desc: '',
    first_frame_desc: '', last_frame_desc: '', source_excerpt: '', narration: '',
    dialogues: [], transition: '', continuity_from_prev: 0, adopted_version_id: null,
    versions: [version], video_stale: false, storyboard_pack_segment: segment,
  }
}

function renderPanel(status: string, goToBoard: () => void) {
  let renderer!: TestRenderer.ReactTestRenderer
  act(() => {
    renderer = TestRenderer.create(createElement(GenerationPanel, {
      shot: shotWithStatus(status), context: null, referenceImages: {},
      detailLoading: false, detailError: null,
      onRefresh: async () => undefined, onToast: () => undefined, goToBoard,
    }))
  })
  return renderer
}

function findGoToBoardButton(renderer: TestRenderer.ReactTestRenderer) {
  return renderer.root.findAll(
    node => node.type === 'button' && node.props.className === 'btn small',
  )[0]
}

describe('生成台失败态给出分镜台出路，不是 prompt_override 这类接口参数', () => {
  it.each(['failed', 'waiting_human', 'quarantined'])('%s 态渲染按钮且点击后调用 goToBoard', status => {
    const goToBoard = vi.fn()
    const renderer = renderPanel(status, goToBoard)
    const button = findGoToBoardButton(renderer)
    expect(button, `${status} 态应渲染出分镜台出路按钮`).toBeTruthy()
    expect([button.props.children].flat().join('')).toContain('到分镜台修订第 5 段')
    act(() => { button.props.onClick() })
    expect(goToBoard).toHaveBeenCalledTimes(1)
    act(() => { renderer.unmount() })
  })

  it('succeeded 态不渲染这个按钮——它只在失败态才有意义', () => {
    const renderer = renderPanel('succeeded', vi.fn())
    expect(findGoToBoardButton(renderer)).toBeUndefined()
    act(() => { renderer.unmount() })
  })
})
