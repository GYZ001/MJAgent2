// CharacterVoicePanel：生成/重新生成先过 DecisionDialog 确认（不用
// window.confirm——本仓 vitest 跑在 node 环境没有 window）、采用候选、错误就地
// 中文显示、请求中禁用按钮。

const { mockApi } = vi.hoisted(() => ({
  mockApi: {
    getProjectVoices: vi.fn(),
    suggestVoiceDescription: vi.fn(),
    generateCharacterVoice: vi.fn(),
    adoptCharacterVoice: vi.fn(),
  },
}))
vi.mock('../../api/voices', () => mockApi)

import React from 'react'
import TestRenderer, { act } from 'react-test-renderer'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectVoices, VoiceVersion } from '../../api/voices'
// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import CharacterVoicePanel from './CharacterVoicePanel'

/** 同 ProjectSettingsPanel.test.ts 的先例：本组件经 GenerationParamsDialog 使用
 *  的 DecisionDialog 依赖 useFocusTrap，其 useEffect 摸 document/window，node
 *  环境没有这两个全局，补最小存根。 */
function installHostStubs() {
  const passthrough = { addEventListener: () => {}, removeEventListener: () => {} }
  ;(globalThis as { window?: unknown }).window = { ...passthrough }
  ;(globalThis as { document?: unknown }).document = { ...passthrough, activeElement: null, body: { style: {} } }
}
function uninstallHostStubs() {
  delete (globalThis as { window?: unknown }).window
  delete (globalThis as { document?: unknown }).document
}
beforeEach(installHostStubs)
afterEach(() => { vi.resetAllMocks(); uninstallHostStubs() })

function voice(overrides: Partial<VoiceVersion> = {}): VoiceVersion {
  return {
    id: 'v1', status: 'candidate', source: 'design', voice_prompt: '低沉、克制', preview_text: '天要下雨了。',
    audio_url: 'https://x/a.mp3', clip_url: 'https://x/a-clip.mp3', clip_duration_s: 4.2,
    check_status: 'passed', check_reason: null, asr_text: '天要下雨了。', error: null,
    created_at: 1, adopted_at: null, ...overrides,
  }
}

function payload(overrides: Partial<ProjectVoices['items'][number]> = {}): ProjectVoices {
  return {
    voice_model_configured: true, auto_generate: true,
    items: [{ character_name: '张三', anchor_key: '', current: null, candidates: [], generating: false, ...overrides }],
  }
}

async function mount(projectId = 'proj-1', characterName = '张三') {
  let view!: TestRenderer.ReactTestRenderer
  await act(async () => {
    view = TestRenderer.create(React.createElement(CharacterVoicePanel, { projectId, characterName }))
  })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  return view
}

function clickByText(view: TestRenderer.ReactTestRenderer, label: string) {
  const button = view.root.findAllByType('button').find(b => b.children.join('') === label)
  expect(button, `找不到按钮：${label}`).toBeDefined()
  return act(async () => { button!.props.onClick(); await Promise.resolve(); await Promise.resolve() })
}

function treeText(view: TestRenderer.ReactTestRenderer): string {
  return JSON.stringify(view.toJSON())
}

describe('CharacterVoicePanel', () => {
  it('已有当前声音：标"当前"，候选里 candidate 态才有"采用"按钮', async () => {
    mockApi.getProjectVoices.mockResolvedValue(payload({
      current: voice({ id: 'cur', status: 'current' }),
      candidates: [voice({ id: 'cand-1', status: 'candidate' }), voice({ id: 'cand-2', status: 'generating' })],
    }))
    const view = await mount()
    const text = treeText(view)
    expect(text).toContain('当前')
    const adoptButtons = view.root.findAllByType('button').filter(b => b.children.join('') === '采用')
    expect(adoptButtons).toHaveLength(1) // 只有 cand-1（candidate 态）能采用，cand-2 还在生成中
    view.unmount()
  })

  it('生成声音：先弹确认框写明付费与数量措辞，确认后才真的调用接口', async () => {
    mockApi.getProjectVoices.mockResolvedValueOnce(payload())
    mockApi.generateCharacterVoice.mockResolvedValue({ voice: voice() })
    mockApi.getProjectVoices.mockResolvedValueOnce(payload({ current: voice({ status: 'current' }) }))
    const view = await mount()

    await clickByText(view, '生成声音')
    expect(treeText(view)).toContain('将调用一次付费的声音生成接口')
    expect(mockApi.generateCharacterVoice).not.toHaveBeenCalled() // 还没点确认，不能已经发出请求

    await clickByText(view, '确认生成')
    expect(mockApi.generateCharacterVoice).toHaveBeenCalledTimes(1)
    const [projectId, characterName, body] = mockApi.generateCharacterVoice.mock.calls[0]
    expect(projectId).toBe('proj-1')
    expect(characterName).toBe('张三')
    expect(body.idempotency_key).toBeTruthy()
    expect(mockApi.getProjectVoices).toHaveBeenCalledTimes(2) // 成功后 refresh 了一次
    view.unmount()
  })

  it('已有当前声音时重新生成：说明新声音进候选、需要试听后采用', async () => {
    mockApi.getProjectVoices.mockResolvedValue(payload({ current: voice({ status: 'current' }) }))
    const view = await mount()
    await clickByText(view, '重新生成')
    const text = treeText(view)
    expect(text).toContain('将调用一次付费的声音生成接口')
    expect(text).toContain('候选')
    expect(text).toContain('采用')
    view.unmount()
  })

  it('生成失败：就地用中文显示错误，不静默吞掉', async () => {
    mockApi.getProjectVoices.mockResolvedValue(payload())
    mockApi.generateCharacterVoice.mockRejectedValue(new Error('未配置声音生成模型，请先在模型中心绑定'))
    const view = await mount()
    await clickByText(view, '生成声音')
    await clickByText(view, '确认生成')
    expect(treeText(view)).toContain('未配置声音生成模型，请先在模型中心绑定')
    view.unmount()
  })

  it('让模型写描述：免费调用，不弹确认框，直接把结果填进草稿框', async () => {
    mockApi.getProjectVoices.mockResolvedValue(payload())
    mockApi.suggestVoiceDescription.mockResolvedValue({ voice_prompt: '沙哑、克制的中年男声', preview_text: '风雪夜归人。' })
    const view = await mount()
    await clickByText(view, '让模型写描述')
    expect(mockApi.suggestVoiceDescription).toHaveBeenCalledWith('proj-1', '张三')
    const textareas = view.root.findAllByType('textarea')
    expect(textareas[0].props.value).toBe('沙哑、克制的中年男声')
    expect(textareas[1].props.value).toBe('风雪夜归人。')
    view.unmount()
  })

  it('采用候选：调用 adopt 接口并带上正确的 voiceId', async () => {
    mockApi.getProjectVoices.mockResolvedValue(payload({ candidates: [voice({ id: 'cand-9', status: 'candidate' })] }))
    mockApi.adoptCharacterVoice.mockResolvedValue({ voice: voice({ id: 'cand-9', status: 'current' }) })
    const view = await mount()
    await clickByText(view, '采用')
    expect(mockApi.adoptCharacterVoice).toHaveBeenCalledWith('proj-1', '张三', 'cand-9')
    view.unmount()
  })

  it('请求进行中禁用"生成声音"/"重新生成"按钮，避免重复提交', async () => {
    mockApi.getProjectVoices.mockResolvedValue(payload())
    let resolveGenerate!: (value: { voice: VoiceVersion }) => void
    mockApi.generateCharacterVoice.mockReturnValue(new Promise(resolve => { resolveGenerate = resolve }))
    const view = await mount()
    await clickByText(view, '生成声音')
    await act(async () => {
      view.root.findAllByType('button').find(b => b.children.join('') === '确认生成')!.props.onClick()
      await Promise.resolve()
    })
    const genButton = view.root.findAllByType('button').find(b => b.children.join('') === '生成中…')
    expect(genButton?.props.disabled).toBe(true)
    await act(async () => { resolveGenerate({ voice: voice() }); await Promise.resolve(); await Promise.resolve() })
    view.unmount()
  })
})
