import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '../api'
import SegmentIdentityReview from './SegmentIdentityReview'

vi.mock('../api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
afterEach(() => vi.resetAllMocks())

const segment = {
  identity_contract_version: 'segment_identity/1.0',
  speech_template: '镜头1：山路。{{speech:U01}}', prompt_text: '原提示词',
  dialogue: [{ utterance_id: 'U01', line: '回来吧。', speaker_identity_id: '孟浩', source_segment_index: 1, delivery_kind: 'inner_monologue' }],
  resources: { characters: [{ identity_id: '孟浩', display_name: '孟浩', visibility: 'voice_only', subject_kind: 'character' }], scenes: [], props: [] },
}
async function mount() {
  const notify = vi.fn(), saved = vi.fn()
  vi.mocked(api.get).mockResolvedValue({ baseline: 'baseline-1', segment, issues: [], versions: [] })
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => { view = TestRenderer.create(React.createElement(SegmentIdentityReview, { shotId: 'shot-1', notify, onSaved: saved })) })
  await click(view, '复核说话人和群演')
  return { view, notify, saved }
}
async function click(view: TestRenderer.ReactTestRenderer, label: string) {
  const button = view.root.findAllByType('button').find(b => b.children.join('') === label)
  expect(button).toBeDefined()
  await act(async () => { button!.props.onClick(); await Promise.resolve() })
}

it('必须预览后才能保存，修改发声方式会撤回旧预览', async () => {
  const { view, saved } = await mount()
  expect(view.root.findAllByType('button').some(b => b.children.includes('保存本段修订'))).toBe(false)
  vi.mocked(api.post).mockResolvedValue({ candidate: { ...segment, prompt_text: '实际展开的提示词' } })
  await click(view, '校验并预览修订')
  expect(view.root.findAllByType('pre')[0].children).toContain('实际展开的提示词')
  await act(async () => { view.root.findAllByType('select')[1].props.onChange({ target: { value: 'offscreen_dialogue' } }) })
  expect(view.root.findAllByType('button').some(b => b.children.includes('保存本段修订'))).toBe(false)
  await click(view, '校验并预览修订')
  await click(view, '保存本段修订')
  expect(api.post).toHaveBeenLastCalledWith('/shots/shot-1/identity-review/apply', expect.objectContaining({ baseline: 'baseline-1' }))
  expect(saved).toHaveBeenCalledOnce()
  view.unmount()
})

it('并发版本冲突时保留候选并显示服务端原因', async () => {
  const { view, saved, notify } = await mount()
  vi.mocked(api.post).mockRejectedValue(new Error('片段已更新，请重新打开复核'))
  await click(view, '校验并预览修订')
  expect(notify).toHaveBeenCalledWith('片段已更新，请重新打开复核', true)
  expect(saved).not.toHaveBeenCalled()
  expect(view.root.findAllByType('select').length).toBe(3)
  view.unmount()
})

it('旧片段只请求当前片段的候选，不直接保存', async () => {
  vi.mocked(api.get).mockResolvedValue({ baseline: 'old', segment: { ...segment, identity_contract_version: '' }, issues: [], versions: [] })
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => { view = TestRenderer.create(React.createElement(SegmentIdentityReview, { shotId: 'old-shot', notify: vi.fn(), onSaved: vi.fn() })) })
  await click(view, '复核说话人和群演')
  expect(view.root.findAllByType('select')).toHaveLength(0)
  vi.mocked(api.post).mockResolvedValue({ candidate: segment })
  await click(view, '仅重新编写本段（调用文本模型）')
  expect(api.post).toHaveBeenCalledOnce()
  expect(api.post).toHaveBeenCalledWith('/shots/old-shot/identity-review/regenerate', { baseline: 'old' })
  view.unmount()
})
